"""The console's QEMU D-Bus display client against a real guest (no GTK needed)."""

import time

import pytest

pytestmark = pytest.mark.slow

gi = pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402

from tests.fixtures import freedos  # noqa: E402

if freedos.available():
    pytest.skip(f"cannot build FreeDOS: {freedos.available()}", allow_module_level=True)

XT = {c: code for c, code in zip("qwertyuiop", range(0x10, 0x1A))}
XT.update({c: code for c, code in zip("asdfghjkl", range(0x1E, 0x27))})
XT.update({c: code for c, code in zip("zxcvbnm", range(0x2C, 0x33))})
XT.update({" ": 0x39, "\n": 0x1C, "-": 0x0C})


def pump(cond, timeout=30.0):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def running_vm(tmp_path):
    from rehome.vm import create, disks, runner
    img = tmp_path / "fd.qcow2"
    disks.convert(str(freedos.build(tmp_path / "fd.img")), str(img), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="Console test", os="freedos1.3",
                                         existing_disk=str(img), network="none"))
    runner.start(cfg)          # with the D-Bus display (not headless)
    yield cfg
    runner.force_off(cfg)


def test_framebuffer_keyboard_mouse(running_vm):
    from rehome.ui.console.dbus_display import DisplayClient
    from rehome.vm import runner
    cfg = running_vm
    client = DisplayClient()
    frames = []
    client.connect("scanout", lambda c, w, h: frames.append(("scanout", w, h)))
    client.connect("update", lambda c, *r: frames.append(("update",) + r))
    t0 = time.monotonic()
    client.connect_qmp(runner.runtime(cfg).qmp_ui)
    # Regression: a blocking call after RegisterListener used to deadlock
    # against QEMU until the 25 s D-Bus timeout.
    assert time.monotonic() - t0 < 5, "connecting to the display took too long"
    assert client.label and client.console_ids
    assert pump(lambda: any(f[0] == "scanout" for f in frames)), "no scanout received"
    assert client.width >= 640 and len(client.fb) == client.width * client.height * 4
    assert pump(lambda: freedos.MARKER in runner.screen_text(cfg), 90)
    for ch in "echo dbus ok\n":
        client.key_press(XT[ch])
        client.key_release(XT[ch])
        pump(lambda: False, 0.05)
    assert pump(lambda: "dbus ok" in runner.screen_text(cfg).split("echo dbus ok")[-1], 30)
    assert any(f[0] == "update" for f in frames), "no incremental updates"
    # The client follows video mode changes (QEMU 8 only changes the console's
    # Width/Height properties): its size matches QEMU's own screendump.
    shot = cfg.dir / "dump.png"
    runner.screenshot(cfg, shot)
    head = shot.read_bytes()[:24]
    assert (client.width, client.height) == (int.from_bytes(head[16:20], "big"),
                                             int.from_bytes(head[20:24], "big"))
    assert len(client.fb) == client.width * client.height * 4
    # The framebuffer is not blank (text is drawn in light grey on black).
    assert client.fb.count(0) < len(client.fb)
    client.mouse_rel(5, 5)
    client.set_ui_info(800, 600)
    pump(lambda: False, 0.5)
    client.close()
