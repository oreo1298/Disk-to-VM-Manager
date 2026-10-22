"""Where disks are stored, and adding, replacing and removing a VM's disks (rehome/vm/storage.py).

Image files in temporary folders only.
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rehome.errors import RehomeError
from rehome.vm import create, disks, library, lifecycle, runner, snapshots, storage
from rehome.vm.config import Disk, DiskSource

pytestmark = pytest.mark.skipif(not shutil.which("qemu-img"), reason="qemu-img is not installed")
G = 2**30


def new_vm(name="Disk test", **kw):
    kw.setdefault("disk_size", G)
    return create.create(create.NewVMSpec(name=name, os="linux", network="none", **kw))


def image(path: Path, fmt="qcow2", size=64 * 2**20) -> Path:
    subprocess.run(["qemu-img", "create", "-q", "-f", fmt, str(path), str(size)], check=True)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def second(tmp_path):
    d = tmp_path / "second-drive"
    d.mkdir()
    return d


def test_default_is_the_vm_folder():
    cfg = new_vm()
    d = cfg.disks[0]
    assert d.file == "disks/disk0.qcow2" and not d.owned
    assert storage.default_disk_dir() is None
    assert storage.created_by_rehome(cfg, d) and storage.is_inside_vm(cfg, d)


def test_default_folder_from_preferences(second):
    storage.set_default_disk_dir(second)
    assert storage.default_disk_dir() == second
    cfg = new_vm()
    d = cfg.disks[0]
    assert d.owned and Path(d.file).parent == second and Path(d.file).exists()
    assert storage.owned_files_outside(cfg) == [Path(d.file)]
    # "" overrides the default: inside the VM's folder.
    inside = new_vm("Inside", disk_dir="")
    assert inside.disks[0].file == "disks/disk0.qcow2"
    storage.set_default_disk_dir(None)
    assert storage.default_disk_dir() is None


def test_chosen_folder_for_a_new_vm(second):
    cfg = new_vm(disk_dir=str(second))
    f = Path(cfg.disks[0].file)
    assert f.parent == second and f.name == f"Disk-test-{cfg.short_id}-disk0.qcow2"
    assert int(disks.info(f)["virtual-size"]) == G


def test_bad_folders_are_refused(tmp_path):
    with pytest.raises(RehomeError, match="does not exist"):
        create.plan(create.NewVMSpec(name="x", os="linux", disk_dir=str(tmp_path / "missing")))
    with pytest.raises(RehomeError, match="does not exist"):
        storage.set_default_disk_dir(tmp_path / "missing")
    if os.geteuid() != 0:                     # root may write anywhere
        ro = tmp_path / "read-only"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            with pytest.raises(RehomeError, match="cannot write"):
                storage.check_folder(ro)
        finally:
            ro.chmod(0o700)


def test_describe(tmp_path, second):
    assert storage.describe(None).startswith("In the VM's folder (")
    assert storage.describe(second).startswith(f"{second} (") and "free" in storage.describe(second)
    assert "less than the disk's full size" in storage.describe(second, need=2**60)
    assert "not available" in storage.describe(tmp_path / "unplugged")


def test_new_disk_never_overwrites_a_file(second):
    cfg = new_vm()
    taken = second / f"Disk-test-{cfg.short_id}-disk1.qcow2"
    taken.write_text("precious")
    file, owned = storage.new_disk_path(cfg, "disk1", second)
    assert owned and file.endswith("disk1-2.qcow2")
    file, owned = storage.new_disk_path(cfg, "disk0", "")
    assert file == "disks/disk0-2.qcow2" and not owned          # disk0.qcow2 exists
    assert taken.read_text() == "precious"


def test_add_new_and_existing_disks(tmp_path, second):
    cfg = new_vm()
    d = storage.add_disk(cfg, size=2 * G, folder=second)
    assert d.id == "disk1" and d.owned and Path(d.file).parent == second
    img = image(tmp_path / "data.img", "raw")
    a = storage.add_disk(cfg, image=img, bus="sata")
    assert (a.id, a.file, a.format, a.bus, a.owned) == ("disk2", str(img), "raw", "sata", False)
    saved = library.reload(cfg)
    assert [x.id for x in saved.disks] == ["disk0", "disk1", "disk2"]
    with pytest.raises(RehomeError, match="size"):
        storage.add_disk(cfg)


def test_physical_disks_and_non_images_are_refused(tmp_path):
    cfg = new_vm()
    for dev in ("/dev/null", "/dev/zero"):
        with pytest.raises(RehomeError, match="physical disk"):
            storage.add_disk(cfg, image=dev)
    with pytest.raises(RehomeError, match="does not exist"):
        storage.add_disk(cfg, image=tmp_path / "nothing.img")
    with pytest.raises(RehomeError, match="not a disk image file"):
        storage.add_disk(cfg, image=tmp_path)
    assert len(library.reload(cfg).disks) == 1


def test_replace_with_an_image_keeps_slot_and_old_file(tmp_path):
    cfg = new_vm()
    old = cfg.resolve(cfg.disks[0].file)
    with library.locked(cfg):
        fresh = library.reload(cfg)
        fresh.disks[0].bus, fresh.disks[0].serial = "sata", "OLD-SERIAL"
        library.save(fresh)
    img = image(tmp_path / "other.qcow2")
    before = sha(img)
    d, note = storage.replace_disk(cfg, "disk0", image=img)
    assert (d.id, d.file, d.bus, d.boot_index, d.serial) == ("disk0", str(img), "sata", 1, "OLD-SERIAL")
    assert old.exists() and "kept" in note
    assert library.reload(cfg).disks[0].file == str(img)
    # The image is used where it is; nothing was written to it.
    assert sha(img) == before
    with pytest.raises(RehomeError, match="already used by this VM"):
        storage.replace_disk(cfg, "disk0", image=img)


def test_images_in_use_are_refused(tmp_path):
    a = new_vm("A")
    b = new_vm("B")
    with pytest.raises(RehomeError, match='already used by "A"'):
        storage.add_disk(b, image=a.resolve(a.disks[0].file))
    with pytest.raises(RehomeError, match="already used by this VM"):
        storage.add_disk(a, image=a.resolve(a.disks[0].file))
    # The original of a linked import is only ever read.
    linked, base, _before = _linked_vm(tmp_path)
    with pytest.raises(RehomeError, match="original disk it was imported from"):
        storage.add_disk(b, image=base)


def test_replace_with_new_disk_and_delete_old(second):
    cfg = new_vm()
    old = cfg.resolve(cfg.disks[0].file)
    d, note = storage.replace_disk(cfg, "disk0", size=3 * G, folder=second, delete_old=True)
    assert d.owned and Path(d.file).parent == second and not old.exists()
    assert "Deleted" in note
    # Replace again into the VM folder: disk0.qcow2 is free again.
    d2, _ = storage.replace_disk(cfg, "disk0", size=G, folder="", delete_old=True)
    assert d2.file == "disks/disk0.qcow2" and not Path(d.file).exists()


def test_attached_images_are_never_deleted(tmp_path):
    cfg = new_vm()
    img = image(tmp_path / "mine.qcow2")
    storage.add_disk(cfg, image=img)
    note = storage.remove_disk(cfg, "disk1", delete_file=True)
    assert img.exists() and "only deletes" in note
    storage.replace_disk(cfg, "disk0", image=img, delete_old=True)
    _d, note = storage.replace_disk(cfg, "disk0", size=G, delete_old=True)
    assert img.exists() and "only deletes" in note


def test_replace_refused_with_checkpoints_or_saved_state(tmp_path, monkeypatch):
    cfg = new_vm()
    snapshots.create(cfg, "before")
    with pytest.raises(RehomeError, match="checkpoints"):
        storage.replace_disk(library.reload(cfg), "disk0", size=G)
    snapshots.delete(library.reload(cfg), "before")
    monkeypatch.setattr(runner, "state", lambda c, timeout=2.0: runner.VMState(runner.SAVED))
    with pytest.raises(RehomeError, match="saved"):
        storage.replace_disk(cfg, "disk0", size=G)
    monkeypatch.setattr(runner, "state", lambda c, timeout=2.0: runner.VMState(runner.RUNNING))
    with pytest.raises(RehomeError, match="turned off"):
        storage.add_disk(cfg, size=G)


def test_new_vm_with_an_existing_image(tmp_path):
    img = image(tmp_path / "installed.qcow2")
    cfg = new_vm(existing_disk=str(img))
    d = cfg.disks[0]
    assert d.file == str(img) and not d.owned and d.boot_index == 1
    with pytest.raises(RehomeError, match="physical disk"):
        create.plan(create.NewVMSpec(name="x", os="linux", existing_disk="/dev/zero"))
    with pytest.raises(RehomeError, match="already used"):
        create.plan(create.NewVMSpec(name="y", os="linux", existing_disk=str(img)))


def test_revert_to_a_checkpoint_older_than_an_added_disk():
    cfg = new_vm()
    snapshots.create(cfg, "before")
    storage.add_disk(cfg, size=G)
    snapshots.revert(library.reload(cfg), "before")         # the new disk stays as it is
    assert [d.id for d in library.reload(cfg).disks] == ["disk0", "disk1"]


def test_failed_change_leaves_no_new_file(second, monkeypatch):
    from rehome.vm import edit
    cfg = new_vm()
    monkeypatch.setattr(edit, "validate", lambda c, host=None: ["broken"])
    with pytest.raises(RehomeError):
        storage.add_disk(cfg, size=G, folder=second)
    assert list(second.iterdir()) == []


def test_delete_vm_removes_owned_disks_elsewhere(tmp_path, second):
    cfg = new_vm(disk_dir=str(second))
    mine = image(tmp_path / "mine.qcow2")
    storage.add_disk(cfg, image=mine)
    f = Path(cfg.disks[0].file)
    library.delete(library.reload(cfg))
    assert not f.exists() and mine.exists()


def test_delete_vm_keeping_disks_leaves_them_in_place(tmp_path, second):
    cfg = new_vm(disk_dir=str(second))
    storage.add_disk(cfg, size=G, folder="")
    mine = image(tmp_path / "mine.qcow2")
    storage.add_disk(cfg, image=mine)
    cfg = library.reload(cfg)
    outside = Path(cfg.disks[0].file)
    kept = library.delete(cfg, keep_disks=True)
    assert outside.exists() and mine.exists()                 # not moved across drives
    assert (kept / "disk1.qcow2").exists() and (kept / "vm.toml").exists()


def test_clone_into_a_folder(tmp_path, second):
    cfg = new_vm()
    storage.add_disk(cfg, image=image(tmp_path / "data.img", "raw"))
    new = lifecycle.clone(library.reload(cfg), "Copy", folder=str(second))
    names = sorted(p.name for p in second.iterdir())
    assert names == [f"Copy-{new.short_id}-disk0.qcow2", f"Copy-{new.short_id}-disk1.img"]
    assert all(d.owned for d in new.disks)
    library.delete(new)
    assert list(second.iterdir()) == []


def _linked_vm(tmp_path):
    base = image(tmp_path / "source.qcow2")
    before = sha(base)
    cfg = new_vm(disk_size=0)
    overlay = cfg.dir / "disks" / "disk0.qcow2"
    disks.create(overlay, 64 * 2**20, backing=str(base), backing_fmt="qcow2")
    with library.locked(cfg):
        fresh = library.reload(cfg)
        fresh.disks.append(Disk(id="disk0", file="disks/disk0.qcow2", boot_index=1,
                                source=DiskSource(kind="image", path=str(base), format="qcow2",
                                                  mode="linked", size=64 * 2**20)))
        library.save(fresh)
    return library.reload(cfg), base, before


def test_make_standalone_into_a_folder(tmp_path, second):
    cfg, base, before = _linked_vm(tmp_path)
    lifecycle.make_standalone(cfg, folder=str(second))
    cfg = library.reload(cfg)
    d = cfg.disks[0]
    assert d.owned and Path(d.file).parent == second and d.source.mode == "copy"
    assert "backing-filename" not in disks.info(d.file)
    assert not (cfg.dir / "disks" / "disk0.qcow2").exists()   # the overlay is gone
    assert sha(base) == before                                  # the source was only read


def test_make_standalone_in_place_by_default(tmp_path):
    cfg, base, before = _linked_vm(tmp_path)
    lifecycle.make_standalone(cfg)
    d = library.reload(cfg).disks[0]
    assert not d.owned and d.file == "disks/disk0-2.qcow2"
    assert "backing-filename" not in disks.info(cfg.resolve(d.file))
    assert not (cfg.dir / "disks" / "disk0.qcow2").exists()
    assert sha(base) == before


def test_copy_import_into_a_folder(tmp_path, second):
    from rehome.disk import overlay
    from rehome.disk.sources import Source
    src_file = image(tmp_path / "old-pc.img", "raw")
    before = sha(src_file)
    cfg = new_vm(disk_size=0)
    src = Source(kind="image", path=str(src_file), stable_path=str(src_file), size=64 * 2**20)
    d = overlay.create_copy(cfg, "disk0", src, str(src_file), folder=str(second))
    assert d.owned and Path(d.file).parent == second and d.source.mode == "copy"
    assert sha(src_file) == before
    d = overlay.create_copy(cfg, "disk1", src, str(src_file), folder="")
    assert d.file == "disks/disk1.qcow2" and not d.owned


def test_cli_disk_commands(tmp_path, second, capsys):
    from rehome.cli.main import main
    cfg = new_vm()
    img = image(tmp_path / "other.qcow2")
    assert main(["disk", "folder", str(second)]) == 0
    assert main(["disk", "add", cfg.name, "--size", "1G"]) == 0
    assert main(["disk", "replace", cfg.name, "disk0", "--image", str(img)]) == 0
    assert main(["disk", "list", cfg.name, "--json"]) == 0
    out = capsys.readouterr().out
    assert str(second) in out and str(img) in out
    assert main(["disk", "remove", cfg.name, "disk1", "--delete-file"]) == 0
    assert list(second.iterdir()) == []
    assert main(["disk", "folder", "--reset"]) == 0
    assert storage.default_disk_dir() is None
