"""The appliance's maintenance console: a text menu on tty1 (runs as root).

It opens from the boot menu, when M is pressed while "Starting <VM>..." shows
(nothing on the screen says so: the console is for whoever looks after the
computer), and by itself when the VM cannot start. Plain numbered menus and
``input()``, so it works on any screen, including the basic-graphics boot's 80x25
text (the main menu has two columns). VM operations go through the ``rehome``
command, which runs them as the "rehome" user that owns the VM; backups,
restores and copied CD/DVD images are made by maintenance.py.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from rehome.appliance import hardware, maintenance, runtime
from rehome.errors import RehomeError
from rehome.util.units import human

REHOME = "/usr/local/bin/rehome"
USB_MOUNT = "/mnt/rehome-usb"
DATA = runtime.STATE.parent                     # the data partition: the VM, its checkpoints
MIB = 2**20


def _clear() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _pause(text: str = "Press Enter to go back.") -> None:
    try:
        input(f"\n{text}")
    except EOFError:
        time.sleep(2)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        time.sleep(1)
        return ""


def _yes(prompt: str) -> bool:
    return _ask(prompt).lower() == "yes"


def _secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except EOFError:
        return ""


def _pick(items: list, prompt: str = "  Number (Enter: back): "):
    """The item whose number (from 1) is typed, or None."""
    c = _ask(prompt)
    return items[int(c) - 1] if c.isdigit() and 1 <= int(c) <= len(items) else None


def _run(cmd: list[str], **kw) -> int:
    try:
        return subprocess.run(cmd, check=False, **kw).returncode
    except OSError as e:
        print(f"{cmd[0]}: {e}")
        return 127


def _out(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _error(e: RehomeError) -> None:
    _pause(" ".join(x for x in (e.message, e.fix or "") if x) + "\nPress Enter.")


def _percent(label: str):
    def show(frac: float) -> None:
        sys.stdout.write(f"\r{label} {min(100, int(frac * 100)):3d}%")
        sys.stdout.flush()
    return show


def _network_summary() -> str:
    out = _out(["ip", "-brief", "-4", "address"])
    parts = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0] != "lo" and f[1] == "UP":
            parts.append(f"{f[0]} {f[2].split('/')[0]}")
    return ", ".join(parts) or "not connected"


def _storage_summary() -> str:
    try:
        u = shutil.disk_usage(DATA)
    except OSError:
        return "?"
    return f"{human(u.free)} free of {human(u.total)}"


def header(conf: dict, vm_name: str, vm_state: str) -> None:
    a = conf.get("appliance", {})
    _clear()
    print("  Rehome appliance - maintenance console")
    print("  " + "-" * 40)
    print(f"  VM:       {vm_name} ({vm_state})")
    print(f"  Built:    {a.get('built', '?')} with Rehome {a.get('rehome_version', '?')} "
          f"on {a.get('system', '?')}")
    print(f"  Network:  {_network_summary()}")
    print(f"  Clock:    {time.strftime('%Y-%m-%d %H:%M')} ({runtime.timezone()}"
          f"{', internet time' if _time_synced() else ''})")
    print(f"  Storage:  {_storage_summary()}")
    if runtime.MESSAGE.exists():
        msg = runtime.MESSAGE.read_text().strip()
        if msg:
            print("\n  ! " + "\n    ".join(_wrap(msg, 72)))
    print()


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width) or [""]


def menu_lines(items: list[tuple[str, str]]) -> list[str]:
    """Two columns, so the menu fits the 25 lines of a text screen."""
    half = (len(items) + 1) // 2
    lines = []
    for i in range(half):
        key, text = items[i]
        left = f"  {key:>3}  {text}"
        right = ""
        if i + half < len(items):
            key, text = items[i + half]
            right = f"{key:>3}  {text}"
        lines.append(f"{left:<40}{right}".rstrip())
    return lines


def main() -> int:
    while True:
        conf = runtime.load_config()              # again each time: a restore changes the VM
        header(conf, conf["appliance"].get("name", "VM"), _vm_state(conf["appliance"]["vm"]))
        for line in menu_lines([(k, t) for k, t, _f in MENU]):
            print(line)
        choice = _ask("\n  Choose: ").upper()
        for key, text, action in MENU:
            if choice == key:
                try:
                    action(conf)
                except Exception as e:      # a page that fails must not close the console
                    page_failed(text, e)
                break


def page_failed(page: str, e: Exception) -> None:
    """Say what went wrong, and keep the details for "Save diagnostics" (the journal)."""
    import syslog
    import traceback
    syslog.openlog("rehome-console")
    for line in traceback.format_exception(e):
        for part in line.rstrip().splitlines():
            syslog.syslog(syslog.LOG_ERR, part)
    _pause(f"\"{page}\" stopped with an error: {e}\n"
           "The details are in the system log (Save diagnostics includes it). Press Enter.")


def _vm_state(vm_id: str) -> str:
    try:
        vms = json.loads(_out([REHOME, "list", "--json"]) or "[]")
    except ValueError:
        return "unknown"
    return next((v.get("state", "?") for v in vms if v.get("id") == vm_id), "missing")


def _the_vm(conf: dict):
    from rehome.vm import library
    return library.find(conf["appliance"]["vm"])


def start_vm(conf: dict) -> None:
    runtime.MESSAGE.unlink(missing_ok=True)
    _run(["systemctl", "--no-block", "start", "rehome-kiosk.service"])
    time.sleep(3)


# --- checkpoints -----------------------------------------------------------------------

def _checkpoints(vm_id: str) -> tuple[list[dict], str]:
    try:
        data = json.loads(_out([REHOME, "checkpoint", "list", "--json", vm_id]) or "{}")
    except ValueError:
        data = {}
    return data.get("checkpoints", []), data.get("current", "")


def checkpoints(conf: dict) -> None:
    vm_id = conf["appliance"]["vm"]
    while True:
        snaps, current = _checkpoints(vm_id)
        _clear()
        print("Checkpoints: the VM as it was at a moment, to go back to (with the VM off)\n")
        if not snaps:
            print("  None yet.")
        for i, s in enumerate(snaps, start=1):
            when = s.get("created", "")[:16].replace("T", " ")
            here = "  (the VM continues from it)" if s.get("id") == current else ""
            print(f"  {i:2}  {s.get('name', '')[:34]:34}  {when}{here}")
        print("\n   T  Take a checkpoint now")
        if snaps:
            print("   G  Go back to a checkpoint\n   D  Delete a checkpoint")
        print("   Enter  Back")
        c = _ask("\n  Choose: ").upper()
        if c == "T":
            name = _ask("  Name (Enter for the date and time): ")
            _run([REHOME, "checkpoint", "create", vm_id] + ([name] if name else []))
            _pause()
        elif c == "G" and snaps:
            s = _pick(snaps, "  Number of the checkpoint (Enter: back): ")
            if s and _yes(f"  Go back to \"{s['name']}\"? What changed since then is lost unless "
                          "you take a checkpoint first. Type yes: "):
                _run([REHOME, "checkpoint", "revert", vm_id, s["id"]])
                _pause()
        elif c == "D" and snaps:
            s = _pick(snaps, "  Number of the checkpoint (Enter: back): ")
            if s and _yes(f"  Delete \"{s['name']}\"? Type yes: "):
                _run([REHOME, "checkpoint", "delete", vm_id, s["id"]])
                _pause()
        else:
            return


# --- USB drives --------------------------------------------------------------------------

def _usb_partitions() -> list[tuple[str, str, str]]:
    """(device, description, filesystem) of partitions on USB drives (not the appliance's own)."""
    out = []
    try:
        data = json.loads(_out(["lsblk", "-J", "-o",
                                "NAME,PATH,TRAN,TYPE,FSTYPE,SIZE,LABEL,MOUNTPOINT"]))
    except ValueError:
        return out

    def walk(nodes, tran=""):
        for n in nodes or []:
            t = n.get("tran") or tran
            if n.get("type") == "part" and t == "usb" and n.get("fstype") in (
                    "vfat", "exfat", "ntfs", "ext4") and not n.get("mountpoint"):
                out.append((n["path"], f"{n.get('label') or ''} {n.get('size')} {n['fstype']}",
                            n["fstype"]))
            walk(n.get("children"), t)
    walk(data.get("blockdevices"))
    return out


def _pick_drive(what: str) -> tuple[str, str, str] | None:
    parts = _usb_partitions()
    if not parts:
        _pause("Plug in a USB drive (FAT, exFAT, NTFS or ext4) and choose this again. "
               "Press Enter.")
        return None
    print(f"USB drives ({what}):\n")
    for i, (dev, desc, _fs) in enumerate(parts, start=1):
        print(f"  {i}  {dev}  {desc}")
    return _pick(parts)


@contextlib.contextmanager
def _mounted(dev: str, readonly: bool = False):
    os.makedirs(USB_MOUNT, exist_ok=True)
    if _run(["mount"] + (["-o", "ro"] if readonly else []) + [dev, USB_MOUNT]) != 0:
        raise RehomeError("The USB drive could not be opened.",
                          fix="Check it on another computer, or use another drive.")
    try:
        if not readonly and os.statvfs(USB_MOUNT).f_flag & os.ST_RDONLY:
            # NTFS drives Windows did not close (Fast Startup, pulled out) open read-only.
            raise RehomeError("The USB drive can only be read, not written to.",
                              fix="If it was last used in Windows, use \"Safely Remove\" (or "
                                  "turn Fast Startup off) there, or use another drive.")
        yield Path(USB_MOUNT)
    finally:
        _run(["umount", USB_MOUNT])


# --- backups -----------------------------------------------------------------------------

def backups(conf: dict) -> None:
    while True:
        _clear()
        print("Back up or restore the VM\n")
        print("A backup is one .rehome file on a USB drive, in its \"Rehome backups\" folder.\n"
              "FAT32 drives hold backups up to 4 GB; use exFAT or NTFS for bigger VMs.\n"
              "Rehome on a computer can import a backup too.\n")
        print("   1  Back up the VM to a USB drive\n   2  Restore the VM from a backup\n"
              "   Enter  Back")
        c = _ask("\n  Choose: ")
        if c == "1":
            back_up(conf)
        elif c == "2":
            restore(conf)
        else:
            return


def back_up(conf: dict) -> None:
    _clear()
    print("Back up the VM to a USB drive\n")
    picked = _pick_drive("the backup is saved on it")
    if not picked:
        return
    try:
        cfg = _the_vm(conf)
        with _mounted(picked[0]) as drive:
            print(f"\n  Backing up {cfg.name}. This can take a while; leave the drive in.\n")
            path = maintenance.backup(cfg, drive, picked[2], progress=_percent("  Backing up"),
                                      work_dir=runtime.STATE)
            size = path.stat().st_size
    except RehomeError as e:
        _error(e)
        return
    print(f"\n\n  Saved \"{maintenance.BACKUP_DIR}/{path.name}\" ({human(size)}).")
    _pause("You can take the drive out now. Press Enter.")


def _while_growing(total: int, label: str, work) -> object:
    """Run ``work()``, showing how much of ``total`` bytes the data partition has taken."""
    start = shutil.disk_usage(DATA).free
    done = threading.Event()

    def show():
        while not done.wait(1.0):
            used = max(0, start - shutil.disk_usage(DATA).free)
            _percent(label)(used / total if total else 0)
    t = threading.Thread(target=show, daemon=True)
    t.start()
    try:
        return work()
    finally:
        done.set()
        t.join()


def restore(conf: dict) -> None:
    _clear()
    print("Restore the VM from a backup\n")
    picked = _pick_drive("with the backup")
    if not picked:
        return
    try:
        with _mounted(picked[0], readonly=True) as drive:
            found = maintenance.backups_on(drive)[:20]
            if not found:
                _pause(f"No backups on this drive (.rehome files in \"{maintenance.BACKUP_DIR}\" "
                       "or at the top). Press Enter.")
                return
            _clear()
            print("Backups on the drive, newest first:\n")
            for i, p in enumerate(found, start=1):
                print(f"  {i:2}  {p.name[:56]:56}  {human(p.stat().st_size)}")
            path = _pick(found)
            if path is None:
                return
            info = maintenance.backup_info(path)
            when = info["exported"][:16].replace("T", " ") or "an unknown date"
            print(f"\n  \"{info['name']}\", backed up {when}.")
            if not _yes(f"  This replaces \"{conf['appliance'].get('name', 'the VM')}\" and "
                        "deletes its checkpoints. Type yes to restore: "):
                return
            print("\n  Restoring. This can take a while; leave the drive in.\n")
            new = _while_growing(info["bytes"], "  Restoring",
                                 lambda: maintenance.restore(path, conf))
    except RehomeError as e:
        _error(e)
        return
    _pause(f"\n\nRestored \"{new.name}\". Start it with 1 in the menu. Press Enter.")


# --- VM settings -----------------------------------------------------------------------------

def vm_settings(conf: dict) -> None:
    while True:
        try:
            cfg = _the_vm(conf)
        except RehomeError as e:
            _error(e)
            return
        r = hardware.gather()
        _clear()
        print(f"VM settings: {cfg.name}\n")
        print(f"  Memory:      {human(cfg.machine.memory_mib * MIB)} "
              f"(this computer has {human(r.memory_mib * MIB)})")
        print(f"  Processors:  {cfg.machine.cpus} (this computer has {r.cpus})")
        cds = ", ".join(Path(d.file).name if d.file else "empty" for d in cfg.cdroms)
        print(f"  CD/DVD:      {cds or 'no drive'}")
        print("\n   1  Memory\n   2  Processors\n"
              "   3  Put a CD/DVD image (.iso) from a USB drive in\n"
              "   4  Take the CD/DVD out\n   Enter  Back")
        c = _ask("\n  Choose: ")
        if c == "1":
            set_memory(cfg, r)
        elif c == "2":
            n = _ask(f"  Processors, 1 to {r.cpus} (Enter: keep): ")
            if n.isdigit() and 1 <= int(n) <= r.cpus:
                _run([REHOME, "set", cfg.id, "--cpus", n])
                _pause()
        elif c == "3":
            insert_cd(cfg)
        elif c == "4":
            eject_cd(cfg)
        else:
            return


def set_memory(cfg, report) -> None:
    if cfg.os.family == "win9x":
        print("  Windows 95, 98 and ME may not start with more than 512 MB.")
    n = _ask("  Memory in MB, e.g. 512 or 2048 (Enter: keep): ")
    if not n.isdigit() or int(n) < 16:
        return
    if report.memory_mib and int(n) > report.memory_mib:
        _pause(f"This computer has only {report.memory_mib} MB. Press Enter.")
        return
    _run([REHOME, "set", cfg.id, "--memory", n])
    _pause()


def insert_cd(cfg) -> None:
    from rehome.vm import library
    _clear()
    print("Put a CD/DVD image in the VM's drive\n")
    picked = _pick_drive("with the .iso file")
    if not picked:
        return
    try:
        with _mounted(picked[0], readonly=True) as drive:
            images = maintenance.images_on(drive)
            if not images:
                _pause("No .iso files on this drive (in its top two folder levels). Press Enter.")
                return
            _clear()
            print("Disc images on the drive:\n")
            for i, p in enumerate(images, start=1):
                print(f"  {i:2}  {str(p.relative_to(drive))[:56]:56}  {human(p.stat().st_size)}")
            src = _pick(images)
            if src is None:
                return
            print(f"\n  Copying {src.name} to this drive, so the USB drive can be taken out.\n")
            dest = maintenance.copy_image(src, cfg, progress=_percent("  Copying"))
            print()
    except RehomeError as e:
        _error(e)
        return
    if not cfg.cdroms:
        _run([REHOME, "set", cfg.id, "--add-cd", ""])
        cfg = library.find(cfg.id)
    before = maintenance.images_in_use(cfg)
    if _run([REHOME, "cd", cfg.id, str(dest), "--drive", cfg.cdroms[0].id]) == 0:
        _forget_images(library.find(cfg.id), before - {dest})
    _pause()


def eject_cd(cfg) -> None:
    from rehome.vm import library
    before = maintenance.images_in_use(cfg)
    for d in cfg.cdroms:
        if d.file:
            _run([REHOME, "cd", cfg.id, "--drive", d.id])
    _forget_images(library.find(cfg.id), before)
    _pause()


def _forget_images(cfg, paths: set[Path]) -> None:
    """Offer to delete copied images (in the VM's media folder) no drive uses any more."""
    in_use = maintenance.images_in_use(cfg)
    for p in sorted(paths):
        if p.parent == cfg.dir / "media" and p not in in_use and p.is_file():
            if _ask(f"  Delete the copy of {p.name} from this drive ({human(p.stat().st_size)})? "
                    "y/N: ").lower() in ("y", "yes"):
                p.unlink()


# --- USB devices ------------------------------------------------------------------------------

def usb_devices(conf: dict) -> None:
    from rehome.vm import usb
    while True:
        settings = runtime.load_settings()
        prefs = settings.get("usb", {})
        excluded, included = set(prefs.get("exclude", [])), set(prefs.get("include", []))
        devs = usb.list_host_devices()
        system_paths = runtime._system_drive_paths()
        _clear()
        print("USB devices. Plugged-in devices are given to the VM automatically, except:\n"
              "keyboards and mice (they reach the VM through its screen), the drive the\n"
              "appliance runs from, network adapters, devices built into the computer,\n"
              "Bluetooth adapters, and devices you exclude here.\n")
        reasons = []
        for i, d in enumerate(devs, start=1):
            why = runtime.usb_reason_to_skip(d, runtime._sysfs_dir(d), system_paths, excluded,
                                             included=included)
            reasons.append(why)
            print(f"  {i:2}  {d.vendor_id}:{d.product_id}  {d.label[:40]:40}  "
                  f"{'stays here: ' + why if why else 'to the VM'}")
        c = _ask("\n  Number to exclude/include (Enter: back): ")
        if not c.isdigit() or not 1 <= int(c) <= len(devs):
            return
        d, why = devs[int(c) - 1], reasons[int(c) - 1]
        ident = f"{d.vendor_id}:{d.product_id}"
        if ident in included:
            included.discard(ident)
        elif why in (runtime.BUILT_IN, runtime.BLUETOOTH):
            included.add(ident)
        elif why and ident not in excluded:
            _pause(f"{d.label} cannot be given to the VM ({why.split(' (')[0]}). Press Enter.")
            continue
        else:
            excluded ^= {ident}
        settings["usb"] = {"exclude": sorted(excluded), "include": sorted(included)}
        _save_settings(settings)


def _save_settings(settings: dict) -> None:
    runtime.save_settings(settings)
    shutil.chown(runtime.SETTINGS, "rehome", "rehome")    # the kiosk reads them as "rehome"


# --- network -----------------------------------------------------------------------------------

def _wifi_device() -> str:
    for n in sorted(Path("/sys/class/net").glob("*")):
        if (n / "wireless").exists() or (n / "phy80211").exists():
            return n.name
    return ""


def network(conf: dict) -> None:
    while True:
        _clear()
        print("Network and Wi-Fi\n")
        _run(["ip", "-brief", "address"])
        wifi = _wifi_device()
        print("\nWired connections are set up automatically.")
        if wifi:
            print(f"\n  1  Connect to a Wi-Fi network ({wifi})\n  2  Forget a Wi-Fi network")
        print("  3  Test the connection\n  Enter  Back")
        c = _ask("\n  Choose: ")
        if c == "1" and wifi:
            _run(["iwctl", "station", wifi, "scan"])
            time.sleep(3)
            _run(["iwctl", "station", wifi, "get-networks"])
            ssid = _ask("  Network name: ")
            if ssid:
                pw = _ask("  Password (Enter if none): ")
                cmd = ["iwctl"] + (["--passphrase", pw] if pw else []) + \
                      ["station", wifi, "connect", ssid]
                _run(cmd)
                time.sleep(5)
                _pause()
        elif c == "2" and wifi:
            _run(["iwctl", "known-networks", "list"])
            ssid = _ask("  Network to forget: ")
            if ssid:
                _run(["iwctl", "known-networks", ssid, "forget"])
                _pause()
        elif c == "3":
            _run(["ping", "-c", "3", "-W", "3", "archive.ubuntu.com"])
            _pause()
        else:
            return


# --- date, time and time zone ----------------------------------------------------------------

def _time_synced() -> bool:
    return _out(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip() == "yes"


def _internet_time() -> bool:
    return _out(["timedatectl", "show", "-p", "NTP", "--value"]).strip() == "yes"


def clock(conf: dict) -> None:
    while True:
        ntp = _internet_time()
        _clear()
        print("Date, time and time zone\n")
        print(f"  Now:            {time.strftime('%Y-%m-%d %H:%M')} ({runtime.timezone()})")
        print("  Internet time:  " + (("on, set" if _time_synced() else
                                       "on, waiting for a connection") if ntp else "off"))
        print("\n  Windows and DOS VMs take the date, time and time zone from here each time\n"
              "  they start.\n")
        print("   1  Set the date and time by hand" + (" (turns internet time off)" if ntp else ""))
        if not ntp:
            print("   2  Use internet time")
        print(f"   3  Time zone ({runtime.timezone()})\n   Enter  Back")
        c = _ask("\n  Choose: ")
        if c == "1":
            set_time()
        elif c == "2" and not ntp:
            _run(["timedatectl", "set-ntp", "true"])
            _pause("Internet time is on; the clock is set once the appliance is online. "
                   "Press Enter.")
        elif c == "3":
            choose_timezone()
        else:
            return


def set_time() -> None:
    text = _ask("  Date and time, e.g. 2026-10-24 14:05 (Enter: keep): ")
    if not text:
        return
    try:
        when = dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        _pause("That is not a date and time like 2026-10-24 14:05. Press Enter.")
        return
    _run(["timedatectl", "set-ntp", "false"])
    for _ in range(5):                  # until time synchronization has stopped
        if _run(["timedatectl", "set-time", when.strftime("%Y-%m-%d %H:%M:00")],
                stderr=subprocess.DEVNULL) == 0:
            _pause(f"The clock says {when:%Y-%m-%d %H:%M} now. Press Enter.")
            return
        time.sleep(1)
    _pause("The clock could not be set. Press Enter.")


def choose_timezone() -> None:
    print(f"\n  Time zone now: {runtime.timezone()}. Windows and DOS VMs show its local time.")
    text = _ask("  New time zone or part of its name, e.g. Berlin or New_York (Enter: keep): ")
    if not text:
        return
    names = [text] if runtime.valid_timezone(text) else runtime.find_timezones(text)
    if len(names) > 1:
        for i, n in enumerate(names[:30], start=1):
            print(f"  {i:2}  {n}")
        c = _ask("  Number (Enter: keep): ")
        names = [names[int(c) - 1]] if c.isdigit() and 1 <= int(c) <= min(len(names), 30) else []
    if not names:
        _pause("No such time zone. Press Enter.")
        return
    try:
        runtime.set_timezone(names[0])
        _pause(f"The time zone is now {names[0]}. Press Enter.")
    except RehomeError as e:
        _pause(f"{e.message} Press Enter.")


# --- sound, screen -----------------------------------------------------------------------------

def _sound_cards() -> list[str]:
    import re
    try:
        text = Path("/proc/asound/cards").read_text()
    except OSError:
        return []
    return [m.group(1).strip() for m in re.finditer(r"^\s*\d+ \[.*?\]: (.+)$", text, re.M)]


def sound(conf: dict) -> None:
    while True:
        _clear()
        print("Sound\n")
        cards = _sound_cards()
        print("  Sound cards: " + (", ".join(cards) if cards else "none found"))
        print("\n   1  Volume (arrow keys change it, M mutes or unmutes, F6 picks the card,\n"
              "      Esc closes; the volume is kept)\n   2  Play a test sound\n   Enter  Back")
        c = _ask("\n  Choose: ")
        if c == "1":
            _run(["alsamixer"])
            _run(["alsactl", "store"], stderr=subprocess.DEVNULL)
        elif c == "2":
            print("\n  A tone on the left, then on the right...")
            _run(["speaker-test", "-c", "2", "-t", "sine", "-f", "440", "-l", "1"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _pause()
        else:
            return


def _grubenv() -> dict[str, str]:
    out = {}
    for line in _out(["grub-editenv", runtime.GRUBENV, "list"]).splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k] = v
    return out


def screen(conf: dict) -> None:
    while True:
        basic = _grubenv().get("rehome_default") == "vm-basic"
        settings = runtime.load_settings()
        gl = runtime.opengl(settings, args={})
        _clear()
        print("Screen\n")
        print("  Graphics:        " + ("basic (the firmware's display, no acceleration)"
                                       if basic else "normal"))
        print(f"  OpenGL drawing:  {'on' if gl else 'off'}")
        print("\n  If the VM's picture is garbled or flickers, turn OpenGL drawing off.\n"
              "  If the screen stays black when the VM starts, use basic graphics.\n")
        print(f"   1  Always start with {'normal' if basic else 'basic'} graphics")
        print(f"   2  Turn OpenGL drawing {'off' if gl else 'on'}")
        print("   3  Restart now with basic graphics (this time only)\n   Enter  Back")
        c = _ask("\n  Choose: ")
        if c == "1":
            _run(["grub-editenv", runtime.GRUBENV] +
                 (["unset", "rehome_default"] if basic else ["set", "rehome_default=vm-basic"]))
        elif c == "2":
            settings.setdefault("screen", {})["gl"] = not gl
            _save_settings(settings)
        elif c == "3":
            _run(["grub-editenv", runtime.GRUBENV, "set", "next_entry=vm-basic"])
            _run(["systemctl", "reboot"])
            time.sleep(30)
        else:
            return


# --- startup and shutdown -----------------------------------------------------------------------

AFTER_SHUTDOWN = {"poweroff": "turn the computer off", "restart": "start the VM again",
                  "maintenance": "open this console"}


def startup_and_shutdown(conf: dict) -> None:
    while True:
        settings = runtime.load_settings()
        st = runtime.startup(conf, settings)
        name = conf["appliance"].get("name", "VM")
        _clear()
        print("Startup and shutdown\n")
        print("  At power-on:             " + ("start the VM" if st["open"] == "vm"
                                               else "open this console"))
        print(f"  Before the VM starts:    \"Starting {name}...\" shows for {st['delay']} s;\n"
              "                           pressing M then opens this console")
        print(f"  When the VM shuts down:  {AFTER_SHUTDOWN[st['when_vm_shuts_down']]}")
        print("\n  The computer's power button always asks the VM to shut down, then turns\n"
              "  the computer off.\n")
        print("   1  At power-on: " + ("open this console instead" if st["open"] == "vm"
                                       else "start the VM"))
        print("   2  Seconds before the VM starts\n   3  When the VM shuts down\n   Enter  Back")
        c = _ask("\n  Choose: ")
        new = dict(settings.get("startup", {}))
        if c == "1":
            new["open"] = "maintenance" if st["open"] == "vm" else "vm"
        elif c == "2":
            n = _ask(f"  Seconds, 0 to {runtime.MAX_DELAY} (Enter: keep): ")
            if not n.isdigit() or int(n) > runtime.MAX_DELAY:
                continue
            new["delay"] = int(n)
            if int(n) == 0:
                _pause("With 0 there is no time to press M: open this console from the boot "
                       "menu (press Esc while the computer starts). Press Enter.")
        elif c == "3":
            choices = list(AFTER_SHUTDOWN)
            print()
            for i, k in enumerate(choices, start=1):
                print(f"  {i}  {AFTER_SHUTDOWN[k][0].upper() + AFTER_SHUTDOWN[k][1:]}")
            k = _pick(choices)
            if k is None:
                continue
            new["when_vm_shuts_down"] = k
        else:
            return
        settings["startup"] = new
        _save_settings(settings)


# --- maintenance password --------------------------------------------------------------------

def maintenance_password(conf: dict) -> None:
    on = maintenance.has_password()
    _clear()
    print("Maintenance password\n")
    print("  " + ("On: this console asks for it (log in as root) each time it opens."
                  if on else "Off: this console opens without a password."))
    print("\n   1  " + ("Change the password" if on else "Set a password"))
    if on:
        print("   2  Remove the password")
    print("   Enter  Back")
    c = _ask("\n  Choose: ")
    try:
        if c == "1":
            pw = _secret("  New password: ")
            if not pw:
                return
            if _secret("  The same again: ") != pw:
                _pause("The two did not match; nothing changed. Press Enter.")
                return
            maintenance.set_password(pw, conf)
            _pause("Done. From the next time this console opens, log in as \"root\" with this "
                   "password. Press Enter.")
        elif c == "2" and on:
            if _yes("  Anyone could open this console then. Type yes to remove the password: "):
                maintenance.remove_password(conf)
                _pause("The password is removed. Press Enter.")
    except RehomeError as e:
        _error(e)


# --- information -------------------------------------------------------------------------------

def storage(conf: dict) -> None:
    from rehome.vm import disks, library
    _clear()
    print("Storage\n")
    u = shutil.disk_usage(DATA)
    print(f"  For the VM (data partition): {human(u.used)} used, {human(u.free)} free "
          f"of {human(u.total)}")
    try:
        cfg = _the_vm(conf)
        print(f"\n  {cfg.name}:")
        for d in cfg.disks:
            f = cfg.resolve(d.file)
            try:
                size = disks.info_no_backing(f).get("virtual-size", 0)
            except RehomeError:
                size = 0
            print(f"    Disk {d.id}: holds up to {human(size)}, uses {human(disks.actual_size(f))}")
        images = [p for p in (cfg.dir / "media").glob("*") if p.is_file()]
        print(f"    CD/DVD and floppy images: {len(images)}, "
              f"{human(sum(p.stat().st_size for p in images))}")
        print(f"    Checkpoints: {len(cfg.snapshots)} (they keep old data until deleted)")
        print(f"    All together: {human(library.disk_usage(cfg))}")
    except RehomeError as e:
        print(f"\n  {e.message}")
    s = shutil.disk_usage("/")
    print(f"\n  The appliance's own system: {human(s.used)} used of {human(s.total)}")
    _pause()


def hardware_report(conf: dict) -> None:
    _clear()
    print(hardware.gather().to_text())
    _pause()


def logs(conf: dict) -> None:
    while True:
        _clear()
        print("Logs (in a log: arrow keys and PgUp/PgDn move, / searches, q closes)\n")
        print("   1  This start (the appliance's system log)\n   2  The start before\n"
              "   3  The VM's last run (QEMU's log)\n   Enter  Back")
        c = _ask("\n  Choose: ")
        if c in ("1", "2"):
            _run(["journalctl", "-b", "0" if c == "1" else "-1", "-e"])
        elif c == "3":
            try:
                log = _the_vm(conf).dir / "logs" / "qemu.log"
            except RehomeError as e:
                _error(e)
                continue
            if log.exists():
                _run(["less", "+G", str(log)])
            else:
                _pause("The VM has not run yet. Press Enter.")
        else:
            return


# --- diagnostics ---------------------------------------------------------------------------

def collect_diagnostics(vm_id: str, dest_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = dest_dir / f"rehome-appliance-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "hardware.txt").write_text(hardware.gather().to_text() + "\n")
        (t / "journal-this-boot.txt").write_text(_out(["journalctl", "-b", "0", "--no-pager"]))
        (t / "journal-previous-boot.txt").write_text(
            _out(["journalctl", "-b", "-1", "--no-pager"]))
        (t / "lspci.txt").write_text(_out(["lspci", "-nnk"]))
        (t / "lsusb.txt").write_text(_out(["lsusb"]))
        (t / "vm-diagnostics.txt").write_text(_out([REHOME, "diagnostics", vm_id]))
        for f in (Path("/etc/rehome/appliance.toml"), runtime.SETTINGS, runtime.MESSAGE,
                  runtime.RESULT, runtime.INTENT):
            if f.exists():
                shutil.copy(f, t / f.name)     # what the console said, how the VM ended
        logs = Path("/var/lib/rehome/data/vms")
        with tarfile.open(path, "w:gz") as tar:
            for f in sorted(t.iterdir()):
                tar.add(f, arcname=f"rehome-appliance-{stamp}/{f.name}")
            for f in logs.glob("*/logs/*"):
                tar.add(f, arcname=f"rehome-appliance-{stamp}/vm-logs/{f.name}")
    return path


def save_diagnostics(conf: dict) -> None:
    _clear()
    print("Save diagnostics to a USB stick\n")
    picked = _pick_drive("the file is saved on it")
    if not picked:
        return
    try:
        with _mounted(picked[0]) as drive:
            path = collect_diagnostics(conf["appliance"]["vm"], drive)
            print(f"\nSaved {path.name}.")
    except RehomeError as e:
        _error(e)
        return
    _pause("You can take the stick out now. Press Enter.")


# --- the rest -------------------------------------------------------------------------------

def shell(conf: dict) -> None:
    _clear()
    print("Root shell. The VM's files are in /var/lib/rehome; `rehome --help` lists "
          "commands. Type exit to return to the menu.\n")
    _run([os.environ.get("SHELL") or "/bin/bash", "-l"]
         if os.path.exists("/bin/bash") else ["/bin/sh", "-l"])


def restart(conf: dict) -> None:
    _run(["systemctl", "reboot"])
    time.sleep(30)


def power_off(conf: dict) -> None:
    _run(["systemctl", "poweroff"])
    time.sleep(30)


MENU = [
    ("1", "Start the VM", start_vm),
    ("2", "Checkpoints", checkpoints),
    ("3", "Back up or restore the VM", backups),
    ("4", "VM settings (memory, CD/DVD)", vm_settings),
    ("5", "USB devices", usb_devices),
    ("6", "Network and Wi-Fi", network),
    ("7", "Date, time and time zone", clock),
    ("8", "Sound", sound),
    ("9", "Screen", screen),
    ("10", "Startup and shutdown", startup_and_shutdown),
    ("11", "Maintenance password", maintenance_password),
    ("12", "Storage", storage),
    ("13", "Hardware report", hardware_report),
    ("14", "Logs", logs),
    ("15", "Save diagnostics to USB", save_diagnostics),
    ("16", "Shell (root)", shell),
    ("R", "Restart", restart),
    ("P", "Power off", power_off),
]
