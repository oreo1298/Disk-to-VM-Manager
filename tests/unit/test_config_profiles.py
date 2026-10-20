import pytest

from rehome.patch import profiles
from rehome.vm import snapshots
from rehome.vm.config import Disk, DiskSource, ImportInfo, Snapshot, VMConfig


def test_config_roundtrip():
    cfg = VMConfig(name="X")
    cfg.disks.append(Disk(id="disk0", file="disks/a.qcow2",
                          source=DiskSource(kind="block", path="/dev/sdb", size=10)))
    cfg.import_info = ImportInfo(mode="linked", notes=["a"])
    d = cfg.to_dict()
    assert "import" in d and "import_info" not in d
    back = VMConfig.from_dict(d)
    assert back.disks[0].source.path == "/dev/sdb"
    assert back.import_info.notes == ["a"]
    assert back == cfg


def facts(**kw):
    base = {"family": "windows", "arch": "x86_64"}
    base.update(kw)
    return base


def test_profile_selection_windows():
    assert profiles.select(facts(version="5.1", build=2600)).id == "windows-xp"
    assert profiles.select(facts(version="5.2", build=3790)).id == "windows-xp"
    assert profiles.select(facts(version="5.0")).id == "windows-2000"
    assert profiles.select(facts(version="6.1", build=7601)).id == "windows-vista-7"
    assert profiles.select(facts(version="10.0", build=19045, edition="client")).id == "windows-modern"
    assert profiles.select(facts(version="10.0", build=22631, edition="client")).id == "windows-11"
    assert profiles.select(facts(version="10.0", build=26100, edition="server")).id == "windows-modern"
    assert profiles.select(facts(version="5.2", distro="reactos")).id == "reactos"
    assert profiles.select(facts(version="4.0")).id == "windows-nt4"


def test_profile_selection_other_families():
    assert profiles.select({"family": "win9x", "distro": "win95"}).id == "win95"
    assert profiles.select({"family": "win9x", "distro": "winme"}).id == "win98"
    assert profiles.select({"family": "dos", "distro": "freedos"}).id == "dos"
    assert profiles.select({"family": "linux", "distro": "arch", "kernel": "6.10.1"}).id == "linux"
    assert profiles.select({"family": "linux", "kernel": "2.6.18"}).id == "linux-legacy"
    assert profiles.select({"family": "bsd", "distro": "freebsd"}).id == "bsd"
    assert not profiles.select({"family": "macos"}).supported
    assert profiles.select({"family": "haiku"}).id == "generic"


def test_every_profile_patch_is_registered():
    registry = pytest.importorskip("rehome.patch.registry")
    known = registry.all_ids()
    for p in profiles.load_all():
        for pid in p.patches:
            assert pid in known, f"{p.id} references unknown patch {pid}"


def test_snapshot_tree_rendering():
    cfg = VMConfig(name="T")
    cfg.snapshots = [
        Snapshot(id="a", name="A", created="1"),
        Snapshot(id="b", name="B", parent="a", created="2"),
        Snapshot(id="c", name="C", parent="a", created="3", has_state=True),
    ]
    cfg.current_snapshot = "b"
    lines = snapshots.render_tree(cfg)
    assert lines[0].startswith("A")
    assert any("B" in line for line in lines)
    assert any("Now" in line for line in lines)
    roots = snapshots.tree(cfg)
    assert len(roots) == 1 and len(roots[0].children) == 2
    b = [c for c in roots[0].children if c.snapshot and c.snapshot.id == "b"][0]
    assert b.children[0].snapshot is None  # "Now" under the current checkpoint


def test_create_plan_network_none_has_no_nic():
    from rehome.vm import create
    plan = create.plan(create.NewVMSpec(name="Offline", os="reactos", disk_size=0, network="none"))
    assert plan.config.nics == []
    plan = create.plan(create.NewVMSpec(name="Online", os="reactos", disk_size=0))
    assert [n.mode for n in plan.config.nics] == ["nat"]
    assert plan.config.nics[0].model == "pcnet"
