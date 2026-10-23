"""The appliance's maintenance console: a text menu on tty1 (runs as root).

Plain numbered menus and ``input()``, so it works on any screen, including
the basic-graphics boot. VM operations go through the ``rehome`` command,
which runs them as the "rehome" user that owns the VM.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from rehome.appliance import hardware, runtime
from rehome.errors import RehomeError

REHOME = "/usr/local/bin/rehome"
USB_MOUNT = "/mnt/rehome-usb"


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


def _network_summary() -> str:
    out = _out(["ip", "-brief", "-4", "address"])
    parts = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0] != "lo" and f[1] == "UP":
            parts.append(f"{f[0]} {f[2].split('/')[0]}")
    return ", ".join(parts) or "not connected"


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
    if runtime.MESSAGE.exists():
        msg = runtime.MESSAGE.read_text().strip()
        if msg:
            print("\n  ! " + "\n    ".join(_wrap(msg, 72)))
    print()


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width) or [""]


MENU = [
    ("1", "Start the VM"),
    ("2", "Checkpoints"),
    ("3", "USB devices"),
    ("4", "Network, Wi-Fi and time zone"),
    ("5", "Hardware report"),
    ("6", "Save diagnostics to a USB stick"),
    ("7", "Shell (root; type exit to come back)"),
    ("8", "Restart with basic graphics"),
    ("9", "Restart"),
    ("0", "Power off"),
]


def main() -> int:
    conf = runtime.load_config()
    vm_id = conf["appliance"]["vm"]
    while True:
        state = _vm_state(vm_id)
        header(conf, conf["appliance"].get("name", "VM"), state)
        for key, text in MENU:
            print(f"   {key}  {text}")
        choice = _ask("\n  Choose: ")
        if choice == "1":
            runtime.MESSAGE.unlink(missing_ok=True)
            _run(["systemctl", "--no-block", "start", "rehome-kiosk.service"])
            time.sleep(3)
        elif choice == "2":
            checkpoints(vm_id)
        elif choice == "3":
            usb_devices()
        elif choice == "4":
            network()
        elif choice == "5":
            _clear()
            print(hardware.gather().to_text())
            _pause()
        elif choice == "6":
            save_diagnostics(vm_id)
        elif choice == "7":
            _clear()
            print("Root shell. The VM's files are in /var/lib/rehome; `rehome --help` lists "
                  "commands. Type exit to return to the menu.\n")
            _run([os.environ.get("SHELL") or "/bin/bash", "-l"]
                 if os.path.exists("/bin/bash") else ["/bin/sh", "-l"])
        elif choice == "8":
            _run(["grub-editenv", runtime.GRUBENV, "set", "next_entry=vm-basic"])
            _run(["systemctl", "reboot"])
            time.sleep(30)
        elif choice == "9":
            _run(["systemctl", "reboot"])
            time.sleep(30)
        elif choice == "0":
            _run(["systemctl", "poweroff"])
            time.sleep(30)


def _vm_state(vm_id: str) -> str:
    import json
    try:
        vms = json.loads(_out([REHOME, "list", "--json"]) or "[]")
    except ValueError:
        return "unknown"
    return next((v.get("state", "?") for v in vms if v.get("id") == vm_id), "missing")


# --- checkpoints -----------------------------------------------------------------------

def checkpoints(vm_id: str) -> None:
    while True:
        _clear()
        print("Checkpoints (the VM must be off to go back to one)\n")
        _run([REHOME, "checkpoint", "list", vm_id])
        print("\n  1  Take a checkpoint now\n  2  Go back to a checkpoint\n"
              "  3  Delete a checkpoint\n  Enter  Back")
        c = _ask("\n  Choose: ")
        if c == "1":
            name = _ask("  Name (Enter for the date and time): ")
            _run([REHOME, "checkpoint", "create", vm_id] + ([name] if name else []))
            _pause()
        elif c == "2":
            name = _ask("  Checkpoint name or ID: ")
            if name and _ask(f"  Go back to \"{name}\"? Changes since then are lost unless "
                             "you take a checkpoint first. Type yes: ").lower() == "yes":
                _run([REHOME, "checkpoint", "revert", vm_id, name])
                _pause()
        elif c == "3":
            name = _ask("  Checkpoint name or ID: ")
            if name:
                _run([REHOME, "checkpoint", "delete", vm_id, name])
                _pause()
        else:
            return


# --- USB ------------------------------------------------------------------------------------

def usb_devices() -> None:
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
        runtime.save_settings(settings)
        shutil.chown(runtime.SETTINGS, "rehome", "rehome")


# --- network ---------------------------------------------------------------------------------

def _time_synced() -> bool:
    return _out(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip() == "yes"


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


def _wifi_device() -> str:
    for n in sorted(Path("/sys/class/net").glob("*")):
        if (n / "wireless").exists() or (n / "phy80211").exists():
            return n.name
    return ""


def network() -> None:
    while True:
        _clear()
        print("Network, Wi-Fi and time zone\n")
        _run(["ip", "-brief", "address"])
        wifi = _wifi_device()
        print("\nWired connections are set up automatically.")
        if wifi:
            print(f"\n  1  Connect to a Wi-Fi network ({wifi})\n  2  Forget a Wi-Fi network")
        print("  3  Test the connection")
        print(f"  4  Time zone ({runtime.timezone()})\n  Enter  Back")
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
        elif c == "4":
            choose_timezone()
        else:
            return


# --- diagnostics ---------------------------------------------------------------------------

def _usb_partitions() -> list[tuple[str, str]]:
    """(device, description) of partitions on USB drives (not the appliance's own)."""
    import json
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
                out.append((n["path"], f"{n.get('label') or ''} {n.get('size')} {n['fstype']}"))
            walk(n.get("children"), t)
    walk(data.get("blockdevices"))
    return out


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


def save_diagnostics(vm_id: str) -> None:
    _clear()
    print("Save diagnostics to a USB stick\n")
    parts = _usb_partitions()
    if not parts:
        print("Plug in a USB stick (FAT, exFAT, NTFS or ext4) and choose this again.")
        _pause()
        return
    for i, (dev, desc) in enumerate(parts, start=1):
        print(f"  {i}  {dev}  {desc}")
    c = _ask("\n  Number (Enter: back): ")
    if not c.isdigit() or not 1 <= int(c) <= len(parts):
        return
    dev = parts[int(c) - 1][0]
    os.makedirs(USB_MOUNT, exist_ok=True)
    if _run(["mount", dev, USB_MOUNT]) != 0:
        _pause("The stick could not be opened. Press Enter.")
        return
    try:
        path = collect_diagnostics(vm_id, Path(USB_MOUNT))
        print(f"\nSaved {path.name}.")
    finally:
        _run(["umount", USB_MOUNT])
    print("You can unplug the stick now.")
    _pause()
