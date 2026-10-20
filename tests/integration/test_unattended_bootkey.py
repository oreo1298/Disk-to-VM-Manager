"""The "Press any key to boot from CD or DVD" helper against a real (tiny) guest.

Windows media cannot be downloaded in the test environment, so this checks
the mechanism with FreeDOS: the keys Rehome sends over QMP must arrive in
the guest, and after_start() must press only once per prepared install.
"""

import shutil
import time

import pytest

from rehome.media import unattended
from tests.fixtures import freedos

pytestmark = pytest.mark.slow

if freedos.available() or not shutil.which("qemu-system-x86_64"):
    pytest.skip(f"cannot build/boot FreeDOS: {freedos.available() or 'no QEMU'}",
                allow_module_level=True)


@pytest.fixture(scope="module")
def freedos_image(tmp_path_factory):
    return freedos.build(tmp_path_factory.mktemp("img") / "freedos.img")


@pytest.fixture
def vm(freedos_image, tmp_path):
    from rehome.vm import create, disks, runner
    img = tmp_path / "disk.qcow2"
    disks.convert(str(freedos_image), str(img), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="FreeDOS keys", os="freedos1.3",
                                         existing_disk=str(img), network="none"))
    yield cfg
    if runner.is_running(cfg):
        runner.force_off(cfg)


def wait_text(cfg, needle, timeout=90):
    from rehome.vm import runner
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = runner.screen_text(cfg)
        if needle in text:
            return text
        time.sleep(0.5)
    raise AssertionError(f"{needle!r} not on screen:\n{text}")


def test_keys_reach_the_guest_and_are_pressed_once(vm, monkeypatch):
    from rehome.vm import runner
    runner.start(vm, headless=True)
    wait_text(vm, freedos.MARKER)
    n = unattended.press_boot_key(vm, duration=2.5, interval=0.5, key="x")
    assert n >= 3
    wait_text(vm, "xxx")

    # A VM prepared for a Windows install: after_start presses exactly once.
    unattended._write_state(vm, {"method": "windows", "press_boot_key": True,
                                 "boot_key_pressed": False})
    pressed = []
    real = unattended.press_boot_key

    def short(cfg, **kw):
        pressed.append(real(cfg, duration=1.5, interval=0.5, **kw))
        return pressed[-1]
    monkeypatch.setattr(unattended, "press_boot_key", short)
    notes = unattended.after_start(vm)
    assert pressed and pressed[0] > 0 and "pressed a key" in notes[0]
    assert unattended.after_start(vm) == [] and len(pressed) == 1


def test_pressing_stops_when_the_vm_is_off(vm):
    t0 = time.monotonic()
    assert unattended.press_boot_key(vm, duration=30) == 0     # not running: returns at once
    assert time.monotonic() - t0 < 5
