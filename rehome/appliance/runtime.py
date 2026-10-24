"""What runs inside the appliance: start-up, the full-screen VM, first boot.

    rehome-boot.service      boot()        picks the kiosk or the maintenance console
    rehome-firstboot.service firstboot()   grows the data partition, hardware report
    rehome-kiosk.service     prepare() (starts watch_boot()) → kiosk() → after_kiosk()
        kiosk() runs cage, which runs session(): the VM, the USB and power button agents

The kiosk runs as the unprivileged "rehome" user; prepare/watch_boot/after_kiosk
and the others run as root (see the units in data/appliance/rootfs).
"""

from __future__ import annotations

import json
import logging
import os
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
from pathlib import Path

from rehome.appliance import hardware
from rehome.errors import RehomeError
from rehome.util import toml

log = logging.getLogger(__name__)

CONFIG = Path("/etc/rehome/appliance.toml")
STATE = Path("/var/lib/rehome/appliance")           # on the data partition, owned by "rehome"
SETTINGS = STATE / "settings.toml"
RUN = Path("/run/rehome")
LIMITS = RUN / "limits.json"                        # kiosk → session: memory/CPU for this computer
RESULT = RUN / "session.json"                       # session → kiosk: how the VM ended
INTENT = RUN / "kiosk-exit.json"                    # kiosk → after_kiosk (root)
STARTED = RUN / "vm-started"                        # the VM is on screen
MESSAGE = RUN / "console-message"                   # shown by the maintenance console
GRUBENV = "/boot/grub/grubenv"
KEY_POWER = 116


ZONEINFO = Path("/usr/share/zoneinfo")


def valid_timezone(name: str, zoneinfo: Path = ZONEINFO) -> bool:
    """An IANA zone name tzdata has (and nothing that escapes the zoneinfo folder)."""
    if not name or name.startswith(("/", ".")) or ".." in name.split("/"):
        return False
    return (zoneinfo / name).is_file()


def timezone(etc: Path = Path("/etc")) -> str:
    try:
        return (etc / "timezone").read_text().strip() or "Etc/UTC"
    except OSError:
        return "Etc/UTC"


def set_timezone(name: str, etc: Path = Path("/etc"), zoneinfo: Path = ZONEINFO) -> None:
    """The appliance's time zone; Windows and DOS VMs show its local time (root)."""
    if not valid_timezone(name, zoneinfo):
        raise RehomeError(f"\"{name}\" is not a time zone.",
                          fix="Use a name such as Europe/Berlin or America/New_York.")
    tmp = etc / ".localtime.rehome"
    tmp.unlink(missing_ok=True)
    os.symlink(zoneinfo / name, tmp)
    os.replace(tmp, etc / "localtime")
    (etc / "timezone").write_text(name + "\n")


def find_timezones(text: str, zoneinfo: Path = ZONEINFO) -> list[str]:
    """Zone names containing ``text`` (case and spaces ignored), from tzdata's list."""
    want = text.lower().replace(" ", "_")
    names = set()
    for table in ("zone1970.tab", "zone.tab"):
        try:
            for line in (zoneinfo / table).read_text().splitlines():
                f = line.split("\t")
                if len(f) >= 3 and not line.startswith("#"):
                    names.add(f[2])
        except OSError:
            continue
    return sorted(n for n in names if want in n.lower())


def load_config() -> dict:
    try:
        return toml.load(CONFIG)
    except (OSError, ValueError) as e:
        raise RehomeError("This is not a Rehome appliance (or its configuration is damaged).",
                          details=f"{CONFIG}: {e}") from e


def load_settings() -> dict:
    try:
        return toml.load(SETTINGS)
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    toml.dump_atomic(data, SETTINGS)


def cmdline(text: str | None = None) -> dict[str, str]:
    text = text if text is not None else Path("/proc/cmdline").read_text()
    out = {}
    for word in text.split():
        k, _, v = word.partition("=")
        out[k] = v
    return out


def the_vm():
    from rehome.vm import library
    return library.find(load_config()["appliance"]["vm"])


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# --- root side -----------------------------------------------------------------------------

def boot() -> int:
    """rehome-boot.service: start the VM, or the maintenance console."""
    mode = cmdline().get("rehome.mode", "vm")
    if mode == "maintenance":
        clear_recordfail()
        unit = "rehome-maintenance.service"
    else:
        unit = "rehome-kiosk.service"
    return subprocess.run(["systemctl", "--no-block", "start", unit], check=False).returncode


def clear_recordfail() -> None:
    if shutil.which("grub-editenv") and os.path.exists(GRUBENV):
        subprocess.run(["grub-editenv", GRUBENV, "unset", "recordfail"], check=False)


def prepare() -> int:
    """Before the kiosk (root): a clean runtime directory owned by the VM user."""
    RUN.mkdir(exist_ok=True)
    shutil.chown(RUN, "rehome", "rehome")
    os.chmod(RUN, 0o770)
    for p in (INTENT, RESULT, STARTED, LIMITS):
        p.unlink(missing_ok=True)
    subprocess.run(["chvt", "1"], check=False)
    return watch_boot()


def watch_boot() -> int:
    """In the background (root): once the VM is on screen, the boot counts as good.

    Until then GRUB's ``recordfail`` stays set, so after a hang the boot menu
    is shown on the next start. The watcher keeps no terminal open: the kiosk
    unit hangs up tty1 when its main process starts.
    """
    if os.fork():
        return 0
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        if STARTED.exists():
            clear_recordfail()
            break
        time.sleep(1)
    os._exit(0)


def after_kiosk() -> int:
    """After the kiosk ends (root): power off, or open the maintenance console."""
    state = subprocess.run(["systemctl", "is-system-running"], capture_output=True,
                           text=True, check=False).stdout.strip()
    if state == "stopping":
        return 0
    intent = _read_json(INTENT) or {
        "outcome": "error", "message": "The VM screen closed unexpectedly. The system log "
                                       "(maintenance console → Save diagnostics) has details."}
    outcome = intent.get("outcome")
    if outcome == "poweroff":
        return subprocess.run(["systemctl", "poweroff"], check=False).returncode
    if outcome == "reboot":
        return subprocess.run(["systemctl", "reboot"], check=False).returncode
    if intent.get("message"):
        MESSAGE.write_text(intent["message"])
    return subprocess.run(["systemctl", "--no-block", "start",
                           "rehome-maintenance.service"], check=False).returncode


def firstboot() -> int:
    """Once per drive: grow the data partition, record the hardware."""
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        grow_data_partition()
    except (OSError, subprocess.SubprocessError, RehomeError) as e:
        log.warning("could not grow the data partition: %s", e)
    hardware.save(hardware.gather(), STATE)
    (STATE / "firstboot.done").write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"))
    subprocess.run(["chown", "-R", "rehome:rehome", str(STATE)], check=False)
    return 0


def grow_data_partition(mountpoint: str = "/var/lib/rehome") -> None:
    src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", mountpoint], capture_output=True,
                         text=True, check=False).stdout.strip()
    if not src.startswith("/dev/"):
        raise RehomeError(f"{mountpoint} is not mounted from a partition.")
    name = os.path.basename(os.path.realpath(src))
    part = Path("/sys/class/block") / name
    num = (part / "partition").read_text().strip()
    disk = "/dev/" + os.path.basename(os.path.realpath(part / ".."))
    r = subprocess.run(["growpart", disk, num], capture_output=True, text=True, check=False)
    if r.returncode not in (0, 1):          # 1: already as large as it can be
        raise RehomeError("growpart failed.", details=r.stdout + r.stderr)
    if r.returncode == 0:
        subprocess.run(["resize2fs", src], check=True, capture_output=True)


# --- the kiosk (user "rehome", tty1) -------------------------------------------------------

def _wait_key(seconds: float) -> str:
    """A key pressed on the terminal within ``seconds`` ("" if none or no terminal)."""
    if not sys.stdin.isatty():
        time.sleep(seconds)
        return ""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
        r, _, _ = select.select([fd], [], [], seconds)
        return os.read(fd, 1).decode(errors="ignore").lower() if r else ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _say(lines: list[str]) -> None:
    sys.stdout.write("\033[2J\033[H\n")
    for line in lines:
        for w in textwrap.wrap(line, 76) or [""]:
            sys.stdout.write("  " + w + "\n")
    sys.stdout.flush()


def _disk_numbers(cfg) -> tuple[int, int]:
    from rehome.vm import disks
    free = shutil.disk_usage(cfg.dir).free
    grow = 0
    for d in cfg.disks:
        try:
            info = disks.info_no_backing(cfg.resolve(d.file))
            grow += max(0, info.get("virtual-size", 0) - disks.actual_size(cfg.resolve(d.file)))
        except (RehomeError, OSError):
            pass
    return free, grow


def kiosk() -> int:
    conf = load_config()
    try:
        cfg = the_vm()
    except RehomeError as e:
        _write_json(INTENT, {"outcome": "error", "message": f"{e.message} {e.fix or ''}"})
        return 1
    basic = cmdline().get("rehome.graphics") == "basic"
    report = hardware.gather()
    free, grow = _disk_numbers(cfg)
    checks = hardware.start_checks(cfg, report, data_free=free, virtual_total=grow)
    gpu = hardware.has_gpu_acceleration() and not basic
    gl = {"on": True, "off": False}.get(cmdline().get("rehome.gl", ""), True)
    _write_json(LIMITS, {"memory_mib": cfg.machine.memory_mib, "cpus": cfg.machine.cpus,
                         "screen": hardware.screen_size(), "gl": gl})
    countdown = int(conf.get("kiosk", {}).get("countdown", 3)) + (8 if checks.notes else 0)
    _say([f"Starting {cfg.name}...", ""] + [f"! {n}" for n in checks.notes]
         + ["", "Press M for the maintenance console."])
    if _wait_key(countdown) == "m":
        _say(["Opening the maintenance console..."])
        _write_json(INTENT, {"outcome": "maintenance"})
        return 0
    _say([f"Starting {cfg.name}..."])

    env = dict(os.environ)
    env.pop("DISPLAY", None)
    # cage also starts Xwayland (DISPLAY=:0); QEMU's window must use Wayland directly.
    # Without libdecor's plugins SDL's fallback waits forever for the window, and a
    # kiosk window needs no decorations anyway.
    env.update(SDL_VIDEODRIVER="wayland", SDL_VIDEO_WAYLAND_ALLOW_LIBDECOR="0",
               GDK_BACKEND="wayland")
    # The VM's NAT: QEMU's own (slirp), not passt. The VM usually starts before the
    # Wi-Fi is connected, and the network can change while it runs; passt fixes the
    # guest's addresses when it starts (link-local ones without a connection), while
    # slirp gives the guest a fixed private network and follows the host's connection.
    env["REHOME_NO_PASST"] = "1"
    if not gpu:
        # No GPU acceleration (basic graphics, or a display without a render node):
        # wlroots refuses software OpenGL, so it uses its CPU renderer directly, and
        # QEMU's window renders with Mesa's software OpenGL into shared memory (Mesa's
        # EGL crashed QEMU when it looked for a GPU that is not there).
        env["WLR_RENDERER"] = "pixman"
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        if not gl:
            env["SDL_RENDER_DRIVER"] = "software"
    result = None
    rc = 0
    restarts = 0
    while True:
        RESULT.unlink(missing_ok=True)
        rc = subprocess.run(["cage", "--", "/usr/local/bin/rehome", "appliance", "session"],
                            env=env, check=False).returncode
        result = _read_json(RESULT)
        if result and result.get("outcome") == "restart-vm" and restarts < 20:
            restarts += 1
            _say([f"Restarting {cfg.name}..."])
            continue
        if result or env.get("WLR_RENDERER") == "pixman":
            break
        # cage could not use the graphics hardware; software rendering always works.
        log.warning("cage exited with %s before the VM started; retrying with pixman", rc)
        env["WLR_RENDERER"] = "pixman"
    if not result:
        result = {"outcome": "error",
                  "message": f"The screen could not be set up (cage exited with code {rc}). "
                             "Try \"basic graphics\" in the boot menu (press Esc while the "
                             "computer starts)."}
    # The text screen still shows "Starting ..." from before the VM's window opened.
    _say([NEXT_STEP.get(result.get("outcome"), "Opening the maintenance console...")])
    _write_json(INTENT, result)
    return 0


NEXT_STEP = {"poweroff": "Turning off...", "reboot": "Restarting..."}


# --- the session (inside cage) --------------------------------------------------------------

def session() -> int:
    from rehome.vm import runner
    from rehome.vm.qmp import QMP
    # What the agents do (USB devices given to the VM...) goes to the journal, for
    # "Save diagnostics".
    logging.getLogger("rehome.appliance").setLevel(logging.INFO)
    try:
        cfg = the_vm()
        limits = _read_json(LIMITS) or {}
        cfg.machine.memory_mib = int(limits.get("memory_mib", cfg.machine.memory_mib))
        cfg.machine.cpus = int(limits.get("cpus", cfg.machine.cpus))
        screen = limits.get("screen")
        runner.start(cfg, allow_overcommit=True, start_timeout=600,
                     display_size=tuple(screen) if screen else None,
                     kiosk_gl=bool(limits.get("gl", True)))
    except RehomeError as e:
        _write_json(RESULT, {"outcome": "error", "message": " ".join(
            x for x in (f"The VM could not be started: {e.message}", e.fix or "") if x),
            "details": e.details or ""})
        return 1
    stop = threading.Event()
    conf = load_config()
    given: dict[str, object] = {}                 # USB devices the VM has now
    agents = [threading.Thread(target=power_button_agent, args=(cfg, stop), daemon=True)]
    if conf.get("kiosk", {}).get("usb_auto", True):
        agents.append(threading.Thread(target=usb_agent, args=(cfg, stop),
                                       kwargs={"attached": given}, daemon=True))
    if relative_mouse_only(cfg):
        agents.append(threading.Thread(target=mouse_agent, args=(cfg, stop), daemon=True))
    for a in agents:
        a.start()
    reason = ""
    try:
        q = QMP(runner.runtime(cfg).qmp_ui).connect(retries=10)
    except RehomeError:
        q = None
    STARTED.touch()
    def read_events(timeout: float) -> None:
        nonlocal q, reason
        try:
            for ev in q.poll_events(timeout=timeout):
                if ev.get("event") == "SHUTDOWN":
                    reason = ev.get("data", {}).get("reason", "") or reason
        except RehomeError:
            q = None

    try:
        while runner.is_running(cfg):
            if q is not None:
                read_events(1.0)
            else:
                time.sleep(1)
        if q is not None and not reason:
            read_events(0.5)            # QEMU may have exited right after its last event
    finally:
        stop.set()
        if q is not None:
            q.close()
    runner.reap()
    runner.after_stop(cfg)
    log.info("the VM stopped: %s", reason or "QEMU quit without saying why")
    result = outcome_for(reason)
    if result["outcome"] == "error" and not result.get("message"):
        log_tail = runner._tail(cfg.dir / "logs" / "qemu.log")
        result.update(message=crash_message(runner.qemu_crash_line(log_tail),
                                            [d.label for d in given.values()]),
                      details=log_tail)
    _write_json(RESULT, result)
    return 0


def crash_message(qemu_line: str, usb_labels: list[str]) -> str:
    """For the maintenance console: QEMU itself stopped while the VM was running."""
    msg = "The VM stopped unexpectedly: QEMU, the program that runs it, quit."
    if qemu_line:
        msg += f" Its last message: \"{qemu_line}\""
    if usb_labels:
        msg += " USB devices the VM had: " + ", ".join(usb_labels) + \
            " (exclude one in USB devices if the problem comes back)."
    return msg + " Save diagnostics to a USB stick to report it."


def outcome_for(reason: str) -> dict:
    """What the appliance does after QEMU's SHUTDOWN event ``reason`` ("" = no event: a crash)."""
    if reason in ("guest-shutdown", "host-signal"):
        # The guest turned itself off, or the appliance itself is shutting down.
        return {"outcome": "poweroff", "reason": reason}
    if reason == "guest-reset":
        return {"outcome": "restart-vm", "reason": reason}   # only with -no-reboot (direct boot)
    if reason == "guest-panic":
        return {"outcome": "error", "reason": reason,
                "message": "The VM's operating system crashed."}
    if reason == "host-ui":
        return {"outcome": "error", "reason": reason,
                "message": "The VM was closed from its window (Ctrl+Alt+Q) instead of being "
                           "shut down."}
    if reason.startswith("host-qmp"):
        return {"outcome": "maintenance", "reason": reason,
                "message": "The VM was turned off with Rehome's tools."}
    return {"outcome": "error", "reason": reason or "crash"}


def power_button_agent(cfg, stop: threading.Event) -> None:
    """The computer's power button asks the guest to shut down (ACPI), like on real hardware."""
    from rehome.vm import runner
    fds = []
    for ev in sorted(Path("/sys/class/input").glob("event*")):
        if _has_key(ev / "device" / "capabilities" / "key", KEY_POWER):
            try:
                fds.append(os.open(f"/dev/input/{ev.name}", os.O_RDONLY | os.O_NONBLOCK))
            except OSError as e:
                log.debug("cannot read %s: %s", ev.name, e)
    if not fds:
        return
    try:
        while not stop.is_set():
            r, _, _ = select.select(fds, [], [], 1.0)
            for fd in r:
                try:
                    data = os.read(fd, 24 * 16)
                except OSError:
                    continue
                for i in range(0, len(data) - 23, 24):
                    typ, code, value = (int.from_bytes(data[i + 16:i + 18], "little"),
                                        int.from_bytes(data[i + 18:i + 20], "little"),
                                        int.from_bytes(data[i + 20:i + 24], "little"))
                    if typ == 1 and code == KEY_POWER and value == 1:
                        log.warning("power button pressed: asking %s to shut down", cfg.name)
                        try:
                            runner.shutdown(cfg)
                        except RehomeError as e:
                            log.warning("power button: %s", e)
    finally:
        for fd in fds:
            os.close(fd)


def _has_key(capfile: Path, code: int) -> bool:
    try:
        words = capfile.read_text().split()
    except OSError:
        return False
    idx, bit = divmod(code, 64)
    if idx >= len(words):
        return False
    return bool(int(words[-1 - idx], 16) >> bit & 1)


# --- mice for VMs with a PS/2 mouse only -----------------------------------------------------

# Linux input codes (linux/input-event-codes.h).
REL_X, REL_Y, BTN_LEFT = 0x00, 0x01, 0x110


def relative_mouse_only(cfg) -> bool:
    """The VM's only pointing device is a PS/2 mouse (DOS, Windows 3.x/9x): it moves by
    deltas and cannot follow an absolute pointer."""
    return cfg.input.pointer == "ps2" and cfg.machine.type.startswith(("pc", "q35", "isapc"))


def mice(sysclass: Path = Path("/sys/class/input")) -> dict[str, str]:
    """The computer's mice (relative X and Y, a left button): event node -> sysfs device."""
    out = {}
    for ev in sorted(sysclass.glob("event*")):
        caps = ev / "device" / "capabilities"
        if (_has_key(caps / "rel", REL_X) and _has_key(caps / "rel", REL_Y)
                and _has_key(caps / "key", BTN_LEFT)):
            out[ev.name] = os.path.realpath(ev / "device")
    return out


def mouse_agent(cfg, stop: threading.Event, interval: float = 1.0,
                sysclass: Path = Path("/sys/class/input"), devdir: str = "/dev/input") -> None:
    """Hand the computer's mice straight to a VM that only has a PS/2 mouse.

    Such a guest moves its cursor by the deltas QEMU's window sends. cage cannot
    lock the pointer (it has no pointer-constraints protocol), so the hidden pointer
    stopped at the screen's edges while the guest's cursor, accelerated by the guest,
    was elsewhere: "invisible walls". QEMU's input-linux reads a mouse's own movement
    from /dev/input and grabs it for the guest, so the guest gets every movement, like
    a mouse plugged into an old PC. Mice plugged in later are added, unplugged ones
    removed.
    """
    from rehome.vm import runner
    given: dict[str, str] = {}                  # event node -> sysfs device
    failed: set[tuple[str, str]] = set()
    while not stop.is_set():
        present = mice(sysclass)
        gone = [n for n, d in given.items() if present.get(n) != d]
        new = [(n, d) for n, d in present.items()
               if given.get(n) != d and (n, d) not in failed]
        if gone or new:
            try:
                with runner.qmp(cfg) as q:
                    for name in gone:
                        try:
                            q.execute("object-del", {"id": f"mouse-{name}"})
                        except RehomeError:
                            pass
                        given.pop(name, None)
                        log.info("mouse %s removed from the VM", name)
                    for name, dev in new:
                        try:
                            q.execute("object-add", {"qom-type": "input-linux",
                                                     "id": f"mouse-{name}",
                                                     "evdev": f"{devdir}/{name}"})
                            given[name] = dev
                            log.info("mouse %s (%s) given to the VM", name,
                                     _read(Path(dev) / "name") or "?")
                        except RehomeError as e:
                            failed.add((name, dev))
                            log.warning("mouse %s: %s %s", name, e.message, e.details or "")
            except RehomeError as e:
                log.debug("mouse agent: %s", e)
        failed &= set(present.items())
        stop.wait(interval)


# --- USB ------------------------------------------------------------------------------------

def _open_usb(bus: int, devnum: int) -> int:
    return os.open(f"/dev/bus/usb/{bus:03d}/{devnum:03d}", os.O_RDWR)


def _sysfs_dir(dev) -> Path | None:
    for d in Path("/sys/bus/usb/devices").iterdir():
        try:
            if (int((d / "busnum").read_text()) == dev.bus
                    and int((d / "devnum").read_text()) == dev.devnum):
                return d
        except (OSError, ValueError):
            continue
    return None


def _system_drive_paths() -> list[str]:
    """sysfs paths of the block devices holding the appliance itself."""
    out = []
    for mnt in ("/", "/var/lib/rehome", "/boot/efi"):
        try:
            st = os.stat(mnt)
        except OSError:
            continue
        p = Path(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
        if p.exists():
            out.append(os.path.realpath(p))
    return out


UDEV_DATA = Path("/run/udev/data")
# udev's classification of input devices that the appliance keeps: they reach the VM
# through its screen (a touchscreen or drawing tablet acts as the VM's pointer).
POINTER_OR_KEYBOARD = ("ID_INPUT_KEYBOARD", "ID_INPUT_MOUSE", "ID_INPUT_TOUCHPAD",
                       "ID_INPUT_TOUCHSCREEN", "ID_INPUT_TABLET", "ID_INPUT_POINTINGSTICK")


def _input_kinds(sysdir: Path, udev_data: Path = UDEV_DATA) -> set[str]:
    """udev ID_INPUT_* properties of the input devices a USB device provides."""
    kinds: set[str] = set()
    for dev in sysdir.glob("*/*/input/input*/event*/dev"):
        try:
            major_minor = dev.read_text().strip()
            for line in (udev_data / f"c{major_minor}").read_text().splitlines():
                if line.startswith("E:ID_INPUT_") and line.endswith("=1"):
                    kinds.add(line[2:-2])
        except OSError:
            continue
    return kinds


# Devices that stay with the appliance unless included in the maintenance console.
BUILT_IN = "built into the computer (camera, fingerprint reader...): include it to give it to the VM"
BLUETOOTH = "Bluetooth adapter: include it to give it to the VM"


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _built_in(sysdir: Path) -> bool:
    """Wired inside the computer, by the firmware's description of its USB ports."""
    return (_read(sysdir / "removable") == "fixed"
            or _read(sysdir / "port" / "connect_type") == "hardwired")


def usb_reason_to_skip(dev, sysdir: Path | None, system_paths: list[str],
                       excluded: set[str], udev_data: Path = UDEV_DATA,
                       included: set[str] = frozenset()) -> str:
    """Why a USB device stays with the appliance ("" = give it to the VM)."""
    ident = f"{dev.vendor_id}:{dev.product_id}"
    if ident in excluded:
        return "excluded in the maintenance console"
    if sysdir is None:
        return "gone"
    real = os.path.realpath(sysdir)
    if any(p.startswith(real + "/") for p in system_paths):
        return "the drive the appliance runs from"
    bluetooth = False
    for intf in sysdir.glob(f"{sysdir.name}:*"):
        cls = _read(intf / "bInterfaceClass")
        sub = _read(intf / "bInterfaceSubClass")
        proto = _read(intf / "bInterfaceProtocol")
        if cls == "03" and sub == "01" and proto in ("01", "02"):
            return "keyboard or mouse (they reach the VM through its screen)"
        if (intf / "net").exists():
            return "network adapter (the appliance's network connection)"
        bluetooth |= (cls, sub, proto) == ("e0", "01", "01")
    if set(POINTER_OR_KEYBOARD) & _input_kinds(sysdir, udev_data):
        return "keyboard, mouse, touchpad, touchscreen or pen tablet (they reach the VM " \
               "through its screen)"
    if ident in included:
        return ""
    # A laptop's own camera, fingerprint reader and Bluetooth are not "plugged in", and
    # giving them away has crashed VMs: Bluetooth chips re-enumerate when the guest
    # resets them, and QEMU's USB 1.1 controller (Windows XP and older) aborts on that.
    if _built_in(sysdir):
        return BUILT_IN
    if bluetooth:
        return BLUETOOTH
    return ""


def usb_agent(cfg, stop: threading.Event, interval: float = 1.0,
              attached: dict | None = None) -> None:
    """Give USB devices to the VM as they are plugged in (and those present at start)."""
    from rehome.vm import usb
    if cfg.usb.controller == "none":
        log.warning("%s has no USB controller; USB devices stay with the appliance", cfg.name)
        return
    attached = {} if attached is None else attached
    tried: set[str] = set()
    system_paths = _system_drive_paths()
    while not stop.is_set():
        prefs = load_settings().get("usb", {})
        excluded, included = set(prefs.get("exclude", [])), set(prefs.get("include", []))
        present = {d.key: d for d in usb.list_host_devices()}
        for key, dev in present.items():
            if key in attached or key in tried:
                continue
            tried.add(key)
            why = usb_reason_to_skip(dev, _sysfs_dir(dev), system_paths, excluded,
                                     included=included)
            if why:
                log.info("USB %s (%s:%s) stays with the appliance: %s", dev.label,
                         dev.vendor_id, dev.product_id, why)
                continue
            try:
                usb.attach(cfg, dev, fd_provider=_open_usb)
                attached[key] = dev
                log.info("USB %s (%s:%s, %s Mbit/s) given to the VM", dev.label,
                         dev.vendor_id, dev.product_id, dev.speed or "?")
            except RehomeError as e:
                log.warning("USB %s: %s %s", dev.label, e.message, e.details or "")
        for key in list(attached):
            if key not in present:
                dev = attached.pop(key)
                try:
                    usb.detach(cfg, dev)
                except RehomeError:
                    pass
        tried &= set(present)
        stop.wait(interval)
