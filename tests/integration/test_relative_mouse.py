"""Relative (PS/2) mouse input through QEMU's D-Bus display, against a guest that moves
its cursor like Windows 9x (tests/fixtures/ps2mouse.asm)."""

import re
import shutil
import subprocess
import time

import pytest

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not shutil.which("nasm"), reason="nasm is not installed")]

gi = pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402


def pump(cond, timeout=30.0):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.0005)
    return False


@pytest.fixture
def mouse_vm(tmp_path):
    from pathlib import Path
    from rehome.vm import create, disks, library, runner
    src = Path(__file__).resolve().parents[1] / "fixtures" / "ps2mouse.asm"
    raw = tmp_path / "mouse.img"
    subprocess.run(["nasm", "-f", "bin", "-o", str(raw), str(src)], check=True)
    with open(raw, "r+b") as f:
        f.truncate(2**20)
    img = tmp_path / "mouse.qcow2"
    disks.convert(str(raw), str(img), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="Mouse test", os="freedos1.3",
                                         existing_disk=str(img), network="none"))
    cfg.machine.accel = "tcg"            # like Windows 9x VMs
    library.save(cfg)
    assert cfg.input.pointer == "ps2"
    runner.start(cfg)          # with the D-Bus display, like the console
    yield cfg
    runner.force_off(cfg)


def position(cfg):
    from rehome.vm import runner
    m = re.search(r"MOUSE X=(\d+) Y=(\d+) N=(\d+)", runner.screen_text(cfg))
    return tuple(int(v) for v in m.groups()) if m else None


def send(client, rel):
    """What the console's view does: BURST events every TICK_MS until the queue is empty."""
    from rehome.ui.console import relmouse
    while rel.pending():
        for kind, a, b in rel.take():
            client.mouse_button(a, b) if kind == "btn" else client.mouse_rel(a, b)
        pump(lambda: False, relmouse.TICK_MS / 1000)
    client.mouse_rel(0, 0)


def settle(cfg):
    """The guest's (x, y) once it has stopped receiving packets."""
    last = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pump(lambda: False, 0.3)
        now = position(cfg)
        if now and now == last:
            return now[:2]
        last = now
    return last[:2]


def test_moves_match_the_host_pointer_and_edges_line_up(mouse_vm):
    """Regression: Windows 98's cursor did not follow the host pointer across the screen."""
    from rehome.ui.console.dbus_display import DisplayClient
    from rehome.ui.console.relmouse import RelMouse
    from rehome.vm import runner
    cfg = mouse_vm
    client = DisplayClient()
    client.connect_qmp(runner.runtime(cfg).qmp_ui)
    assert pump(lambda: position(cfg) is not None, 60), "the test guest did not start"
    # The console used to read QEMU's IsAbsolute = false as "absolute" and sent
    # positions a PS/2 mouse cannot use: the cursor never moved.
    assert not client.absolute
    assert settle(cfg) == (320, 240)
    size = (640, 480)

    # What the console sent before: one move per host motion event. Windows doubles
    # each, so the guest cursor ran ahead of the host pointer.
    for _ in range(4):
        client.mouse_rel(25, 0)
    assert settle(cfg) == (520, 240)                   # 100 host pixels went 200

    # Now: queued in steps Windows does not accelerate, sent a few at a time.
    rel = RelMouse()
    for _ in range(4):
        rel.motion(-25, 0)
    send(client, rel)
    assert settle(cfg) == (420, 240)
    rel.motion(-37.5, 20.25)
    rel.motion(-37.5, 20.25)
    send(client, rel)
    assert settle(cfg) == (345, 280)

    # Capturing the pointer with a click at (100, 50): the guest cursor goes there.
    rel.align(100, 50, size)
    send(client, rel)
    assert settle(cfg) == (100, 50)

    # The host pointer reaches the image's left edge: the guest cursor goes to its left
    # edge too, and later moves line up again.
    layout = (0, 0, 640, 480)
    assert rel.edge(0, 300, layout, size)
    assert not rel.edge(0, 310, layout, size)           # once per arrival
    send(client, rel)
    assert settle(cfg) == (0, 50)
    rel.motion(50, 0)
    send(client, rel)
    assert settle(cfg) == (50, 50)
    # Leaving the view at the bottom pushes the cursor to the bottom edge.
    rel.left(320, 479, layout, size)
    send(client, rel)
    assert settle(cfg) == (50, 479)

    # A fast flick far beyond what can be sent in time is merged into bigger packets
    # (accelerated by the guest) instead of lagging; the right edge lines it up again.
    for _ in range(100):
        rel.motion(6, 0)
    assert len(rel.queue) == 100
    send(client, rel)
    x, _y = settle(cfg)
    assert x > 50 + 600 or x == 639
    assert rel.edge(640, 400, layout, size)
    send(client, rel)
    assert settle(cfg) == (639, 479)
    client.close()


@pytest.mark.skipif(not __import__("os").environ.get("DISPLAY"), reason="needs a display (Xvfb)")
def test_console_view_follows_the_pointer(mouse_vm):
    """The console widget itself: capture by click, motion, edges, leaving the view."""
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    from rehome.ui.console.dbus_display import DisplayClient
    from rehome.ui.console.view import DisplayView
    from rehome.vm import runner
    cfg = mouse_vm
    client = DisplayClient()
    client.connect_qmp(runner.runtime(cfg).qmp_ui)
    assert pump(lambda: position(cfg) is not None and client.width, 60)
    view = DisplayView(client)
    view.resize_guest = False
    win = Gtk.Window(default_width=client.width, default_height=client.height)
    win.set_child(view)
    win.present()
    assert pump(lambda: view.texture is not None and view.get_width() > 0, 20)
    x0, y0, w, h = view._layout()
    f = w / client.width                                 # widget pixels per guest pixel

    def at(gx, gy):
        return x0 + gx * f, y0 + gy * f

    class Click:
        def get_current_button(self):
            return 1
    view._button(Click(), 1, *at(200, 100), True)        # captures, cursor jumps there
    assert view.grabbed
    assert settle(cfg) == (200, 100)
    def move(gx, gy):                # a mouse reports about every 8 ms (125 Hz)
        view._motion(None, *at(gx, gy))
        pump(lambda: False, 0.008)
    for i in range(1, 11):                               # 100 guest pixels to the right
        move(200 + 10 * i, 100)
    x, y = settle(cfg)               # the window scales a little: fractions carry over
    assert abs(x - 300) <= 1 and y == 100
    move(300, 0)                                         # top edge
    x, y = settle(cfg)
    assert abs(x - 300) <= 1 and y == 0
    for i in range(1, 23):                               # down, 20 guest pixels per event
        move(300 + i * 20 / 22, i * 20)
    view._leave()                                        # out of the window at the bottom
    x, y = settle(cfg)
    assert abs(x - 320) <= 2 and y == 479
    view.set_grab(False)
    win.destroy()
    client.close()
