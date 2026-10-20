"""End-to-end imports of synthetic and real disk images.

Each test builds a source image, imports it through rehome.importer (the same
code the CLI and GUI use) and checks: identification, chosen hardware, the
changes inside the VM's overlay, that the source is byte-for-byte unchanged,
and (where the image is bootable) that the VM boots.
"""

import hashlib
import time

import pytest

pytestmark = pytest.mark.slow

try:
    import guestfs  # noqa: F401
except ImportError:
    pytest.skip("libguestfs Python bindings are not installed", allow_module_level=True)

from tests.fixtures import freedos
from tests.fixtures import windows as W

MOUSE_CLASS = "{4D36E96F-E325-11CE-BFC1-08002BE10318}"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def images(tmp_path_factory):
    d = tmp_path_factory.mktemp("sources")
    return d


def overlay_hive_value(cfg, hive_rel, key, name):
    """Read a registry value from inside the VM's overlay (through libguestfs)."""
    from rehome.inspect.gfs import Drive, session
    from rehome.inspect.registry import Hive
    with session([Drive(str(cfg.resolve(cfg.disks[0].file)), "qcow2")]) as g:
        root = g.inspect_os()[0]
        for mp, dev in sorted(g.inspect_get_mountpoints(root).items(), key=lambda kv: len(kv[0])):
            g.mount_ro(dev, mp)
        with Hive(g, hive_rel) as h:
            return h.get(key, name)


def overlay_has(cfg, device_hint, path):
    from rehome.inspect.gfs import Drive, ci_path, session
    with session([Drive(str(cfg.resolve(cfg.disks[0].file)), "qcow2")]) as g:
        root = g.inspect_os()[0]
        g.mount_ro(g.inspect_get_mountpoints(root)["/"], "/")
        return ci_path(g, path) is not None


def test_windows10_uefi_import_patch_and_rollback(images):
    from rehome import importer
    from rehome.patch import engine
    from rehome.vm import library
    src = W.build(str(images / "win10.img"), W.WinSpec(
        boot_driver="iaStorAC", fast_startup=True, hostile=["VMTools", "VBoxMouse"],
        class_filters={MOUSE_CLASS: ["VBoxMouse", "mouclass"]}))
    before = sha256(src)
    a = importer.analyze([src])
    o = a.os
    assert o.family == "windows" and o.build == "19045" and o.edition == "client"
    assert o.boot_mode == "uefi" and o.original_bus == "sata" and o.hibernated and o.fast_startup
    assert a.profile.id == "windows-modern"
    assert "hibernation" in a.questions
    with pytest.raises(Exception):
        importer.run(a, importer.ImportOptions())          # must decide about hibernation
    cfg = importer.run(a, importer.ImportOptions(discard_hibernation=True, verify_hash=True))
    m = cfg.machine
    assert (m.type, m.firmware, m.rtc, m.hyperv) == ("q35", "uefi", "localtime", True)
    assert cfg.disks[0].bus == "sata" and cfg.nics[0].model == "e1000e"
    assert cfg.disks[0].source.mode == "linked"
    hive = "/Windows/System32/config/SYSTEM"
    svc = "ControlSet001\\Services"
    assert overlay_hive_value(cfg, hive, f"{svc}\\storahci", "Start") == 0
    assert overlay_hive_value(cfg, hive, f"{svc}\\storahci\\StartOverride", "0") is None
    assert overlay_hive_value(cfg, hive, f"{svc}\\stornvme", "Start") == 0
    assert overlay_hive_value(cfg, hive, f"{svc}\\intelppm", "Start") == 4
    assert overlay_hive_value(cfg, hive, f"{svc}\\VMTools", "Start") == 4
    assert overlay_hive_value(cfg, hive, f"{svc}\\VBoxMouse", "Start") == 4
    assert overlay_hive_value(cfg, hive, f"ControlSet001\\Control\\Class\\{MOUSE_CLASS}",
                              "UpperFilters") == ["mouclass"]
    assert overlay_hive_value(cfg, hive, "ControlSet001\\Control\\Session Manager\\Power",
                              "HiberbootEnabled") == 0
    assert not overlay_has(cfg, None, "/hiberfil.sys")
    # The source is untouched (the hash was also verified by the importer).
    assert sha256(src) == before
    assert cfg.disks[0].source.sha256_before == before == cfg.disks[0].source.sha256_after
    log = (cfg.dir / "logs" / "patch.log").read_text()
    assert "storahci" in log and "applied" in log
    # Roll everything back.
    engine.rollback(cfg)
    cfg = library.reload(cfg)
    assert overlay_hive_value(cfg, hive, f"{svc}\\storahci", "Start") == 3
    assert overlay_has(cfg, None, "/hiberfil.sys")
    assert not cfg.import_info.patched
    assert sha256(src) == before


def test_windows11_gets_secure_boot_and_tpm(images):
    from rehome import importer
    src = W.build(str(images / "win11.img"), W.WinSpec(product="Windows 10 Pro", build=22631,
                                                      display_version="23H2", boot_driver="stornvme"))
    a = importer.analyze([src])
    assert a.os.name.startswith("Windows 11 Pro") and a.os.original_bus == "nvme"
    assert a.profile.id == "windows-11"
    cfg = importer.run(a, importer.ImportOptions(discard_hibernation=True, patch=False))
    m = cfg.machine
    assert (m.firmware, m.tpm, m.type) == ("uefi-secure", "2.0", "q35")
    assert cfg.disks[0].bus == "nvme"     # matches the original NVMe controller


def test_windows_xp_mergeide_and_hal(images):
    from rehome import importer
    src = W.build(str(images / "winxp.img"), W.WinSpec(
        product="Microsoft Windows XP", version="5.1", build=2600, edition_id="",
        installation_type="", display_version="", arch="i386", scheme="mbr",
        boot_driver="iaStor", hal="halaacpi.dll"))
    before = sha256(src)
    a = importer.analyze([src])
    assert a.os.hal == "halaacpi.dll" and a.os.boot_mode == "bios"
    assert a.profile.id == "windows-xp"
    cfg = importer.run(a, importer.ImportOptions(mode="copy"))
    m = cfg.machine
    assert (m.type, m.firmware, m.cpus) == ("pc", "bios", 1)        # uniprocessor HAL
    assert cfg.disks[0].bus == "ide" and cfg.display.device == "cirrus-vga"
    assert cfg.nics[0].model == "rtl8139"
    assert cfg.disks[0].source.mode == "copy"
    from rehome.vm import disks
    assert "backing-filename" not in disks.info(cfg.resolve(cfg.disks[0].file))
    hive = "/WINDOWS/system32/config/system"
    cdd = "ControlSet001\\Control\\CriticalDeviceDatabase"
    assert overlay_hive_value(cfg, hive, f"{cdd}\\pci#ven_8086&dev_7010", "Service") == "intelide"
    assert overlay_hive_value(cfg, hive, f"{cdd}\\primary_ide_channel", "Service") == "atapi"
    assert overlay_hive_value(cfg, hive, "ControlSet001\\Services\\intelide", "Start") == 0
    assert sha256(src) == before


def test_windows98_and_msdos(images):
    from rehome import importer
    src98 = W.build_win9x(str(images / "win98.img"))
    a = importer.analyze([src98])
    assert (a.os.family, a.os.distro, a.os.name) == ("win9x", "win98", "Windows 98 Second Edition")
    cfg = importer.run(a, importer.ImportOptions())
    m = cfg.machine
    assert (m.type, m.accel, m.memory_mib <= 512) == ("pc", "tcg", True)
    assert (cfg.sound.device, cfg.nics[0].model, cfg.display.device) == ("sb16", "pcnet", "cirrus-vga")
    srcdos = W.build_msdos(str(images / "msdos.img"))
    a = importer.analyze([srcdos])
    assert (a.os.family, a.os.name) == ("dos", "MS-DOS 6.22")
    assert a.profile.id == "dos"


@pytest.mark.skipif(bool(freedos.available()), reason="cannot build FreeDOS image")
@pytest.mark.parametrize("mode", ["linked", "copy"])
def test_freedos_import_boots_and_source_unchanged(images, mode):
    from rehome import importer
    from rehome.vm import runner
    src = images / f"freedos-{mode}.img"
    if not src.exists():
        freedos.build(src)
    before = sha256(src)
    a = importer.analyze([str(src)])
    assert a.os.distro == "freedos"
    cfg = importer.run(a, importer.ImportOptions(mode=mode, verify_hash=True))
    try:
        runner.start(cfg, headless=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if freedos.MARKER in runner.screen_text(cfg):
                break
            time.sleep(0.5)
        assert freedos.MARKER in runner.screen_text(cfg)
        runner.type_text(cfg, "echo written > C:\\NEWFILE.TXT\n")
        time.sleep(2)
    finally:
        runner.force_off(cfg)
    # Definition of done: the source's hash is unchanged after import and boot.
    assert sha256(src) == before
    from rehome.importer import verify
    assert all(r["unchanged"] for r in verify(cfg))
