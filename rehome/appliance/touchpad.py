"""Touchpads for VMs that only have a PS/2 mouse (docs/DECISIONS.md D46).

Mice go straight to QEMU (runtime.mouse_agent, D41). A touchpad cannot: it
reports where the finger is, and QEMU's input-linux would pass that on as
positions. Through cage and QEMU's window it hit the same invisible walls mice
did. So the appliance reads touchpads itself with libinput, as Linux desktops
do: tapping, the click areas of clickpads, two-finger scrolling, palm detection
and no movement while the keyboard types. It grabs them, so cage no longer
sees them, and sends libinput's movement, buttons and scrolling to the VM's
PS/2 mouse over its own QMP socket (qmp-input.sock, rehome/vm/qemu_cmd.py).
The guest then accelerates the movement as it would a mouse's.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import logging
import os
import select
import threading
from pathlib import Path

log = logging.getLogger("rehome.appliance")

EVIOCGRAB = 0x40044590                  # _IOW('E', 0x90, int)
# libinput.h
POINTER_MOTION, POINTER_BUTTON, POINTER_SCROLL_FINGER = 400, 402, 405
SCROLL_VERTICAL = 0
BUTTON_PRESSED = 1
TAP_ENABLED, TAP_DISABLED = 1, 0
BUTTONS = {0x110: "left", 0x111: "right", 0x112: "middle"}     # BTN_LEFT, BTN_RIGHT...
# Two-finger scrolling comes in motion units; a wheel click is libinput's 15 degrees.
WHEEL_STEP = 15.0
# The maintenance console's choices -> libinput's pointer speed (-1 to 1).
SPEEDS = {"slow": -0.5, "normal": 0.0, "fast": 0.5}
UDEV_DATA = Path("/run/udev/data")


def options(settings: dict) -> dict:
    """``speed`` (a SPEEDS key) and ``tap`` (tap to click) from the appliance's settings."""
    s = settings.get("touchpad", {})
    return {"speed": s.get("speed") if s.get("speed") in SPEEDS else "normal",
            "tap": bool(s.get("tap", True))}


def devices(kind: str, sysclass: Path = Path("/sys/class/input"),
            udev_data: Path = UDEV_DATA) -> dict[str, str]:
    """Event nodes udev marks as ``kind`` (ID_INPUT_TOUCHPAD...) -> their sysfs device."""
    out = {}
    for ev in sorted(sysclass.glob("event*")):
        try:
            major_minor = (ev / "dev").read_text().strip()
            props = (udev_data / f"c{major_minor}").read_text().splitlines()
        except OSError:
            continue
        if f"E:{kind}=1" in props:
            out[ev.name] = os.path.realpath(ev / "device")
    return out


class ToPS2:
    """libinput's pointer events as QMP input-send-event batches for a PS/2 mouse.

    QEMU makes one PS/2 packet of each batch, so a button change gets a batch of its
    own: a press and a release in one would be a packet without the press."""

    def __init__(self):
        self.rest = [0.0, 0.0]                  # fractions of a count not sent yet
        self.scrolled = 0.0

    def motion(self, dx: float, dy: float) -> list[list[dict]]:
        events = []
        for i, (axis, d) in enumerate((("x", dx), ("y", dy))):
            total = self.rest[i] + d
            whole = int(total)
            self.rest[i] = total - whole
            if whole:
                events.append({"type": "rel", "data": {"axis": axis, "value": whole}})
        return [events] if events else []

    def button(self, code: int, pressed: bool) -> list[list[dict]]:
        name = BUTTONS.get(code)
        return [[{"type": "btn", "data": {"button": name, "down": pressed}}]] if name else []

    def scroll(self, value: float) -> list[list[dict]]:
        if value == 0:                          # libinput ends each scroll with 0
            self.scrolled = 0.0
            return []
        self.scrolled += value
        out = []
        while abs(self.scrolled) >= WHEEL_STEP:
            down = self.scrolled > 0
            self.scrolled -= WHEEL_STEP if down else -WHEEL_STEP
            name = "wheel-down" if down else "wheel-up"
            out.append([{"type": "btn", "data": {"button": name, "down": True}},
                        {"type": "btn", "data": {"button": name, "down": False}}])
        return out


OPEN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
CLOSE = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)


class _Interface(ctypes.Structure):
    _fields_ = [("open_restricted", OPEN), ("close_restricted", CLOSE)]


def load() -> ctypes.CDLL:
    """libinput (libinput10, which cage needs too), with the calls used here declared."""
    lib = ctypes.CDLL(ctypes.util.find_library("input") or "libinput.so.10")
    P, I, D = ctypes.c_void_p, ctypes.c_int, ctypes.c_double
    for name, res, args in (
            ("libinput_path_create_context", P, [P, P]),
            ("libinput_path_add_device", P, [P, ctypes.c_char_p]),
            ("libinput_unref", P, [P]),
            ("libinput_get_fd", I, [P]),
            ("libinput_dispatch", I, [P]),
            ("libinput_get_event", P, [P]),
            ("libinput_event_get_type", I, [P]),
            ("libinput_event_destroy", None, [P]),
            ("libinput_event_get_pointer_event", P, [P]),
            ("libinput_event_pointer_get_dx", D, [P]),
            ("libinput_event_pointer_get_dy", D, [P]),
            ("libinput_event_pointer_get_button", ctypes.c_uint32, [P]),
            ("libinput_event_pointer_get_button_state", I, [P]),
            ("libinput_event_pointer_has_axis", I, [P, I]),
            ("libinput_event_pointer_get_scroll_value", D, [P, I]),
            ("libinput_device_config_tap_get_finger_count", I, [P]),
            ("libinput_device_config_tap_set_enabled", I, [P, I]),
            ("libinput_device_config_accel_set_speed", I, [P, D])):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args
    return lib


class LibInput:
    """A libinput context for the given devices; the ones in ``grab`` only reach us."""

    def __init__(self, grab: set[str], lib: ctypes.CDLL | None = None):
        self.lib = lib or load()
        self.grab = grab
        # libinput keeps a pointer to the interface: it lives as long as this object.
        self._iface = _Interface(OPEN(self._open), CLOSE(self._close))
        self.li = self.lib.libinput_path_create_context(ctypes.byref(self._iface), None)
        if not self.li:
            raise OSError("libinput could not create a context")

    def _open(self, path: bytes, flags: int, _data) -> int:
        try:
            fd = os.open(path, flags)
        except OSError as e:
            return -(e.errno or 1)
        if path.decode(errors="replace") in self.grab:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
            except OSError as e:
                log.warning("touchpad %s: cannot take it from the screen (%s)",
                            path.decode(errors="replace"), e)
        return fd

    def _close(self, fd: int, _data) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def add(self, path: str):
        return self.lib.libinput_path_add_device(self.li, path.encode())

    def set_touchpad(self, dev, tap: bool, speed: float) -> None:
        if self.lib.libinput_device_config_tap_get_finger_count(dev) > 0:
            self.lib.libinput_device_config_tap_set_enabled(dev, TAP_ENABLED if tap
                                                            else TAP_DISABLED)
        self.lib.libinput_device_config_accel_set_speed(dev, speed)

    def fileno(self) -> int:
        return self.lib.libinput_get_fd(self.li)

    def read(self, ps2: ToPS2) -> list[list[dict]]:
        """What libinput has now, as batches for the VM (motion in a row combined)."""
        lib = self.lib
        lib.libinput_dispatch(self.li)
        batches: list[list[dict]] = []
        dx = dy = 0.0
        while True:
            ev = lib.libinput_get_event(self.li)
            if not ev:
                break
            kind = lib.libinput_event_get_type(ev)
            if kind in (POINTER_MOTION, POINTER_BUTTON, POINTER_SCROLL_FINGER):
                p = lib.libinput_event_get_pointer_event(ev)
                if kind == POINTER_MOTION:
                    dx += lib.libinput_event_pointer_get_dx(p)
                    dy += lib.libinput_event_pointer_get_dy(p)
                else:
                    batches += ps2.motion(dx, dy)
                    dx = dy = 0.0
                    if kind == POINTER_BUTTON:
                        batches += ps2.button(lib.libinput_event_pointer_get_button(p),
                                              lib.libinput_event_pointer_get_button_state(p)
                                              == BUTTON_PRESSED)
                    elif lib.libinput_event_pointer_has_axis(p, SCROLL_VERTICAL):
                        batches += ps2.scroll(
                            lib.libinput_event_pointer_get_scroll_value(p, SCROLL_VERTICAL))
            lib.libinput_event_destroy(ev)
        return batches + ps2.motion(dx, dy)

    def close(self) -> None:
        if self.li:
            self.lib.libinput_unref(self.li)    # closes the devices, which ends the grabs
            self.li = None


def agent(send, stop: threading.Event, settings: dict | None = None, interval: float = 1.0,
          sysclass: Path = Path("/sys/class/input"), udev_data: Path = UDEV_DATA,
          devdir: str = "/dev/input", lib: ctypes.CDLL | None = None) -> None:
    """Move the VM's PS/2 mouse with the computer's touchpads until ``stop`` is set or
    ``send`` (one batch of QMP input events) fails because the VM is gone.

    Keyboards join the libinput context too, ungrabbed, so that libinput can ignore
    the touchpad while someone types."""
    from rehome.errors import RehomeError
    opts = options(settings or {})
    ps2 = ToPS2()
    li: LibInput | None = None
    have = None
    try:
        while not stop.is_set():
            pads = devices("ID_INPUT_TOUCHPAD", sysclass, udev_data)
            want = (pads, devices("ID_INPUT_KEYBOARD", sysclass, udev_data)) if pads else None
            if want != have:
                if li:
                    li.close()
                    li = None
                have = want
                if want:
                    try:
                        li = LibInput({f"{devdir}/{n}" for n in pads}, lib)
                    except OSError as e:
                        log.warning("touchpads stay with the screen: %s", e)
                        stop.wait(interval)
                        continue
                    for name, dev in pads.items():
                        handle = li.add(f"{devdir}/{name}")
                        if handle:
                            li.set_touchpad(handle, opts["tap"], SPEEDS[opts["speed"]])
                            log.info("touchpad %s (%s) moves the VM's mouse", name,
                                     _read(Path(dev) / "name") or "?")
                        else:
                            log.warning("touchpad %s: libinput cannot open it", name)
                    for name in want[1]:
                        if name not in pads:
                            li.add(f"{devdir}/{name}")
            if li is None:
                stop.wait(interval)
                continue
            ready, _, _ = select.select([li.fileno()], [], [], interval)
            if ready:
                for batch in li.read(ps2):
                    try:
                        send(batch)
                    except RehomeError as e:
                        log.debug("touchpad agent: %s", e)
                        return
    finally:
        if li:
            li.close()


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""
