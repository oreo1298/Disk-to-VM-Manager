"""Run a real (tiny) guest through the whole VM lifecycle.

Uses the FreeDOS disk built by tests/fixtures/freedos.py; boots in a few
seconds even without KVM.
"""

import hashlib
import shutil
import time

import pytest

from tests.fixtures import freedos

pytestmark = pytest.mark.slow

if freedos.available() or not shutil.which("qemu-system-x86_64"):
    pytest.skip(f"cannot build/boot FreeDOS: {freedos.available() or 'no QEMU'}",
                allow_module_level=True)


@pytest.fixture(scope="module")
def freedos_image(tmp_path_factory):
    return freedos.build(tmp_path_factory.mktemp("img") / "freedos.img")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


@pytest.fixture
def vm(freedos_image, tmp_path):
    from rehome.vm import create, disks, runner
    img = tmp_path / "disk.qcow2"
    disks.convert(str(freedos_image), str(img), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="FreeDOS", os="freedos1.3",
                                         existing_disk=str(img), network="none"))
    yield cfg
    if runner.is_running(cfg):
        runner.force_off(cfg)


def test_boot_screenshot_and_checkpoints(vm, tmp_path):
    from rehome.vm import library, runner, snapshots
    runner.start(vm, headless=True)
    wait_text(vm, freedos.MARKER)
    shot = tmp_path / "shot.png"
    runner.screenshot(vm, shot)
    assert shot.stat().st_size > 500

    # Online checkpoint includes RAM.
    runner.type_text(vm, "echo first-state\n")
    wait_text(vm, "first-state")
    snap = snapshots.create(vm, "online")
    assert snap.has_state
    runner.type_text(vm, "echo second-state\n")
    wait_text(vm, "second-state")
    snapshots.revert(vm, snap.id)
    text = wait_text(vm, "first-state")
    assert "second-state" not in text
    runner.force_off(vm)

    # Start straight from the checkpoint with memory.
    vm = library.reload(vm)
    snapshots.start_from(vm, snap.id, headless=True)
    wait_text(vm, "first-state")
    runner.force_off(vm)

    # Offline checkpoint tree: child of the online one.
    vm = library.reload(vm)
    off = snapshots.create(vm, "offline")
    assert not off.has_state and off.parent == snap.id
    snapshots.revert(vm, off.id)
    snapshots.delete(vm, snap.id)
    vm = library.reload(vm)
    assert [s.id for s in vm.snapshots] == [off.id]
    assert vm.snapshots[0].parent == ""


def test_save_and_restore(vm):
    from rehome.vm import runner
    runner.start(vm, headless=True)
    wait_text(vm, freedos.MARKER)
    runner.type_text(vm, "echo before-save\n")
    wait_text(vm, "before-save")
    runner.save(vm)
    assert runner.state(vm).status == runner.SAVED
    runner.start(vm, headless=True)
    assert "before-save" in wait_text(vm, "before-save", timeout=30)
    assert not runner.saved_state_path(vm).exists()


def test_pause_resume_reset(vm):
    from rehome.vm import runner
    runner.start(vm, headless=True)
    wait_text(vm, freedos.MARKER)
    runner.pause(vm)
    assert runner.state(vm).status == runner.PAUSED
    runner.resume(vm)
    assert runner.state(vm).status == runner.RUNNING
    runner.reset(vm)
    wait_text(vm, "SeaBIOS")
    wait_text(vm, freedos.MARKER)


def test_clone_export_import(vm, tmp_path):
    from rehome.vm import lifecycle, runner
    clone = lifecycle.clone(vm, "FreeDOS clone")
    assert clone.id != vm.id and clone.nics == [] or clone.nics[0].mac != vm.nics[0].mac
    runner.start(clone, headless=True)
    wait_text(clone, freedos.MARKER)
    runner.force_off(clone)
    bundle = lifecycle.export(vm, str(tmp_path))
    imported = lifecycle.import_bundle(str(bundle), "FreeDOS imported")
    runner.start(imported, headless=True)
    wait_text(imported, freedos.MARKER)
    runner.force_off(imported)


def test_source_image_never_written(freedos_image, tmp_path):
    """Attaching an image as a linked source must not modify it."""
    from rehome.vm import create, disks, library, runner
    from rehome.vm.config import DiskSource
    before = sha256(freedos_image)
    overlay = tmp_path / "overlay.qcow2"
    size = disks.info(str(freedos_image))["virtual-size"]
    disks.create(overlay, size, backing=str(freedos_image), backing_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="FreeDOS linked", os="freedos1.3",
                                         existing_disk=str(overlay), network="none"))
    cfg.disks[0].source = DiskSource(kind="image", path=str(freedos_image), format="raw",
                                     mode="linked")
    library.save(cfg)
    runner.start(cfg, headless=True)
    wait_text(cfg, freedos.MARKER)
    runner.type_text(cfg, "echo hello > C:\\WRITE.TXT\n")
    time.sleep(2)
    runner.force_off(cfg)
    assert sha256(freedos_image) == before
