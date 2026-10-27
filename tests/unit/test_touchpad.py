"""Touchpads for VMs with only a PS/2 mouse (rehome/appliance/touchpad.py, D46)."""

import os
import threading

from rehome.appliance import touchpad as tp
from rehome.errors import RehomeError


def _input(tmp_path, name, minor, props, devname="Touchpad"):
    sysclass, udev = tmp_path / "class", tmp_path / "udev"
    dev = tmp_path / "devices" / name
    dev.mkdir(parents=True)
    (dev / "name").write_text(devname + "\n")
    ev = sysclass / name
    ev.mkdir(parents=True)
    (ev / "dev").write_text(f"13:{minor}\n")
    (ev / "device").symlink_to(dev)
    udev.mkdir(exist_ok=True)
    (udev / f"c13:{minor}").write_text("".join(f"E:{p}=1\n" for p in props))
    return sysclass, udev


def test_settings():
    assert tp.options({}) == {"speed": "normal", "tap": True}
    assert tp.options({"touchpad": {"speed": "fast", "tap": False}}) == {"speed": "fast",
                                                                           "tap": False}
    assert tp.options({"touchpad": {"speed": "warp"}})["speed"] == "normal"


def test_touchpads_and_keyboards_are_found_by_udevs_classification(tmp_path):
    _input(tmp_path, "event3", 67, ["ID_INPUT", "ID_INPUT_TOUCHPAD"])
    _input(tmp_path, "event4", 68, ["ID_INPUT", "ID_INPUT_KEYBOARD", "ID_INPUT_KEY"])
    sysclass, udev = _input(tmp_path, "event5", 69, ["ID_INPUT", "ID_INPUT_MOUSE"])
    (sysclass / "event6").mkdir()                      # no udev data: skipped
    pads = tp.devices("ID_INPUT_TOUCHPAD", sysclass, udev)
    assert list(pads) == ["event3"] and pads["event3"].endswith("devices/event3")
    assert list(tp.devices("ID_INPUT_KEYBOARD", sysclass, udev)) == ["event4"]


def test_movement_keeps_the_fractions():
    ps2 = tp.ToPS2()
    assert ps2.motion(0.4, -0.4) == []
    assert ps2.motion(0.7, -0.7) == [[{"type": "rel", "data": {"axis": "x", "value": 1}},
                                       {"type": "rel", "data": {"axis": "y", "value": -1}}]]
    assert ps2.motion(10.0, 0) == [[{"type": "rel", "data": {"axis": "x", "value": 10}}]]
    total = sum(e["data"]["value"] for _ in range(100) for b in ps2.motion(0.3, 0) for e in b)
    assert total in (30, 31)


def test_buttons_and_scrolling():
    ps2 = tp.ToPS2()
    assert ps2.button(0x110, True) == [[{"type": "btn", "data": {"button": "left",
                                                                 "down": True}}]]
    assert ps2.button(0x111, False)[0][0]["data"] == {"button": "right", "down": False}
    assert ps2.button(0x113, True) == []                # a side button the PS/2 mouse lacks
    assert ps2.scroll(10.0) == []
    (click,) = ps2.scroll(10.0)                         # 20 units: one wheel click down
    assert [e["data"] for e in click] == [{"button": "wheel-down", "down": True},
                                          {"button": "wheel-down", "down": False}]
    assert ps2.scroll(0) == []                          # the end of the scroll: rest dropped
    assert ps2.scroll(-14.0) == []
    assert len(ps2.scroll(-31.0)) == 3 and ps2.scroll(-0.1) == []


class FakeLib:
    """libinput as touchpad.LibInput calls it, replaying a script of pointer events."""

    def __init__(self, script, readable_fd):
        self.script = list(script)      # (type, values) in order
        self.fd = readable_fd
        self.added, self.config = [], []
        self.unrefs = 0
        self.iface = None

    def libinput_path_create_context(self, iface, _data):
        self.iface = iface._obj
        return 1

    def libinput_path_add_device(self, _li, path):
        self.added.append(path.decode())
        fd = self.iface.open_restricted(path, os.O_RDONLY, None)
        self.iface.close_restricted(fd, None)
        return len(self.added)

    def libinput_device_config_tap_get_finger_count(self, _dev):
        return 3

    def libinput_device_config_tap_set_enabled(self, dev, on):
        self.config.append(("tap", dev, on))

    def libinput_device_config_accel_set_speed(self, dev, speed):
        self.config.append(("speed", dev, speed))

    def libinput_get_fd(self, _li):
        return self.fd

    def libinput_dispatch(self, _li):
        return 0

    def libinput_get_event(self, _li):
        return len(self.script) if self.script else None

    def libinput_event_get_type(self, _ev):
        return self.script[0][0]

    def libinput_event_get_pointer_event(self, ev):
        return ev

    def libinput_event_pointer_get_dx(self, _p):
        return self.script[0][1][0]

    def libinput_event_pointer_get_dy(self, _p):
        return self.script[0][1][1]

    def libinput_event_pointer_get_button(self, _p):
        return self.script[0][1][0]

    def libinput_event_pointer_get_button_state(self, _p):
        return self.script[0][1][1]

    def libinput_event_pointer_has_axis(self, _p, axis):
        return axis == tp.SCROLL_VERTICAL

    def libinput_event_pointer_get_scroll_value(self, _p, _axis):
        return self.script[0][1][0]

    def libinput_event_destroy(self, _ev):
        self.script.pop(0)

    def libinput_unref(self, _li):
        self.unrefs += 1


def test_the_agent_sends_libinputs_events_until_the_vm_is_gone(tmp_path):
    _input(tmp_path, "event3", 67, ["ID_INPUT_TOUCHPAD"], "SynPS/2 Synaptics TouchPad")
    sysclass, udev = _input(tmp_path, "event4", 68, ["ID_INPUT_KEYBOARD"])
    devdir = tmp_path / "dev"
    devdir.mkdir()
    for n in ("event3", "event4"):
        (devdir / n).write_text("")
    r, w = os.pipe()
    os.write(w, b"x")                       # libinput's fd has something to read
    lib = FakeLib([(tp.POINTER_MOTION, (1.5, 0.5)), (tp.POINTER_MOTION, (1.0, 0.75)),
                   (tp.POINTER_BUTTON, (0x110, 1)), (tp.POINTER_BUTTON, (0x110, 0)),
                   (tp.POINTER_SCROLL_FINGER, (-16.0,)), (tp.POINTER_MOTION, (0.5, 0)),
                   (tp.POINTER_BUTTON, (0x111, 1))],
                  r)
    sent = []

    def send(batch):
        if len(sent) == 5:
            raise RehomeError("The VM is gone.")
        sent.append(batch)

    stop = threading.Event()
    tp.agent(send, stop, settings={"touchpad": {"tap": False, "speed": "slow"}},
             interval=0.01, sysclass=sysclass, udev_data=udev, devdir=str(devdir), lib=lib)
    os.close(r)
    os.close(w)
    # The touchpad is configured; the keyboard only joins (for "disable while typing").
    assert lib.added == [f"{devdir}/event3", f"{devdir}/event4"]
    assert lib.config == [("tap", 1, tp.TAP_DISABLED), ("speed", 1, -0.5)]
    assert sent == [
        [{"type": "rel", "data": {"axis": "x", "value": 2}},
         {"type": "rel", "data": {"axis": "y", "value": 1}}],
        [{"type": "btn", "data": {"button": "left", "down": True}}],
        [{"type": "btn", "data": {"button": "left", "down": False}}],
        [{"type": "btn", "data": {"button": "wheel-up", "down": True}},
         {"type": "btn", "data": {"button": "wheel-up", "down": False}}],
        [{"type": "rel", "data": {"axis": "x", "value": 1}}]]
    assert lib.unrefs == 1                  # the context is closed: the grab ends


def test_without_a_touchpad_nothing_is_opened(tmp_path):
    sysclass, udev = _input(tmp_path, "event4", 68, ["ID_INPUT_KEYBOARD"])
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    tp.agent(lambda b: None, stop, interval=0.01, sysclass=sysclass, udev_data=udev,
             lib=object())                  # would fail if libinput were used
