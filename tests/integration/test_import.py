"""End-to-end imports of synthetic and real disk images.

Each test builds a source image, imports it through rehome.importer (the same
code the CLI and GUI use) and checks: identification, chosen hardware, the
changes inside the VM's overlay, that the source is byte-for-byte unchanged,
and (where the image is bootable) that the VM boots.
"""

import hashlib
import json
import time

import pytest

pytestmark = pytest.mark.slow

from rehome.inspect import gfs  # noqa: E402

if not gfs.backend():
    pytest.skip("libguestfs is not installed", allow_module_level=True)

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


def _windows7_x86(images, name, drivers):
    return W.build(str(images / name), W.WinSpec(
        product="Windows 7 Professional", version="6.1", build=7601, edition_id="Professional",
        display_version="", arch="i386", scheme="mbr", boot_driver="msahci",
        driver_store=drivers))


def test_windows7_switch_to_virtio_makes_the_disk_driver_boot_start(images, monkeypatch):
    """Regression: Windows 7 (32-bit) blue-screened after "Switch to faster devices". The SPICE
    guest tools only put viostor in the driver store (the VM had no virtio disk yet), so
    Windows had no driver for its boot disk (0x7B). Rehome now makes viostor boot-start
    first, as virt-v2v does, and reverting the checkpoint also puts the old devices back."""
    from rehome import importer
    from rehome.vm import guest_tools, library, snapshots
    monkeypatch.setattr(guest_tools, "_check_available", lambda cfg, changes: None)  # QXL module
    src = _windows7_x86(images, "win7-x86-tools.img", ("viostor", "netkvm", "qxl"))
    before = sha256(src)
    a = importer.analyze([src])
    assert a.profile.id == "windows-vista-7" and a.os.arch == "i386"
    cfg = importer.run(a, importer.ImportOptions(mode="linked"))
    assert (cfg.disks[0].bus, cfg.nics[0].model) == ("sata", "e1000")
    log = guest_tools.switch_to_paravirt(cfg)
    assert "Windows: the virtio disk driver (viostor) now loads at boot" in log, log
    cfg = library.reload(cfg)
    assert (cfg.disks[0].bus, cfg.nics[0].model, cfg.display.device) == \
        ("virtio", "virtio-net-pci", "qxl-vga")
    hive = "/Windows/System32/config/SYSTEM"
    svc = "ControlSet001\\Services\\viostor"
    assert overlay_hive_value(cfg, hive, svc, "Start") == 0
    assert overlay_hive_value(cfg, hive, svc, "ImagePath") == "system32\\drivers\\viostor.sys"
    assert overlay_hive_value(cfg, hive, svc, "Group") == "SCSI miniport"
    cdd = "ControlSet001\\Control\\CriticalDeviceDatabase\\pci#ven_1af4&dev_1001&subsys_00021af4&rev_00"
    assert overlay_hive_value(cfg, hive, cdd, "Service") == "viostor"
    assert overlay_hive_value(cfg, hive, cdd, "ClassGUID") == "{4D36E97B-E325-11CE-BFC1-08002BE10318}"
    assert overlay_has(cfg, None, "/Windows/System32/drivers/viostor.sys")
    # Going back to the checkpoint undoes both the disk changes and the device switch.
    snap = next(s for s in cfg.snapshots if s.kind == "pre-tools")
    snapshots.revert(cfg, snap.id)
    cfg = library.reload(cfg)
    assert (cfg.disks[0].bus, cfg.nics[0].model, cfg.display.device, cfg.guest.paravirt) == \
        ("sata", "e1000", "VGA", False)
    assert overlay_hive_value(cfg, hive, svc, "Start") is None
    assert sha256(src) == before


def test_windows7_without_virtio_drivers_keeps_its_disk_and_network(images, monkeypatch):
    """Guest tools without viostor/netkvm: the disks stay SATA and the network e1000."""
    from rehome import importer
    from rehome.vm import guest_tools, library
    monkeypatch.setattr(guest_tools, "_check_available", lambda cfg, changes: None)
    src = _windows7_x86(images, "win7-x86-qxl-only.img", ("qxl",))
    cfg = importer.run(importer.analyze([src]), importer.ImportOptions(mode="linked"))
    log = guest_tools.switch_to_paravirt(cfg)
    assert any("disks stay SATA" in line and "viostor" in line for line in log), log
    assert any("network stays e1000" in line and "netkvm" in line for line in log), log
    cfg = library.reload(cfg)
    assert (cfg.disks[0].bus, cfg.nics[0].model, cfg.display.device) == ("sata", "e1000", "qxl-vga")


def test_windows98_fast_cpu_fix(images):
    """Regression: a Windows 98 appliance stopped with "While initializing device NDIS:
    Windows protection error" on a fast computer (QEMU 7.2+ emulates LOOP fast enough
    for the delay-loop calibration to divide by zero). NDIS.VXD and the disk drivers
    are patched in the VM's disk as patcher9x does."""
    from rehome import importer
    from rehome.inspect.gfs import Drive, session
    from rehome.patch.win9x_speed_data import DRIVERS, NDIS
    from tests.unit.test_win9x_speed import embed
    ndis, _ = embed(NDIS[0])
    esdi, _ = embed(DRIVERS[0], seed=5)
    src = W.build_win9x(str(images / "win98fe-ndis.img"), kernel32=(4, 10, 1998, 0), extra={
        "/WINDOWS/SYSTEM/NDIS.VXD": bytes(ndis),
        "/WINDOWS/SYSTEM/IOSUBSYS/ESDI_506.PDR": bytes(esdi)})
    before = sha256(src)
    a = importer.analyze([src])
    assert a.os.distro == "win98"
    cfg = importer.run(a, importer.ImportOptions())
    entry = next(json.loads(line) for line in (cfg.dir / "logs" / "patch.log").read_text()
                 .splitlines() if '"win9x.cpu_speed"' in line)
    assert entry["status"] == "applied", entry
    assert any("NDIS.VXD" in c for c in entry["changes"])
    assert any("ESDI_506.PDR" in c for c in entry["changes"])
    with session([Drive(str(cfg.resolve(cfg.disks[0].file)), "qcow2")]) as g:
        g.mount_ro("/dev/sda1", "/")
        patched = g.read_file("/WINDOWS/SYSTEM/NDIS.VXD")
    assert bytes.fromhex("b900004006") in patched and bytes(ndis) != patched
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


@pytest.mark.skipif(bool(freedos.available()), reason="cannot build FreeDOS image")
@pytest.mark.parametrize("nbd_backend", ["builtin", "nbdkit"])
def test_physical_disk_import_with_exclusive_helper(images, monkeypatch, nbd_backend):
    """A "physical disk" (block source) through a helper whose opens are exclusive.

    The real helper opens disks with O_EXCL, so a second open of the same disk
    fails while the first fd is held. The fake helper below reproduces that
    with an exclusive lock per open file description. Regression: --verify
    used to ask the helper for a second fd while the import held the first.
    """
    import fcntl
    import os
    from rehome import importer
    from rehome.disk import sources
    from rehome.util.proc import which
    if nbd_backend == "nbdkit" and not which("nbdkit"):
        pytest.skip("nbdkit is not installed")
    if nbd_backend == "builtin":
        monkeypatch.setenv("REHOME_NO_NBDKIT", "1")
    src = images / "freedos-block.img"
    if not src.exists():
        freedos.build(src)
    before = sha256(src)

    def describe_as_disk(path):
        s = sources.describe_image(path)
        s.kind, s.format, s.label = "block", "raw", "Fake SATA disk"
        return s
    monkeypatch.setattr(sources, "describe", describe_as_disk)

    def exclusive_helper(path):
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            from rehome.errors import RehomeError
            raise RehomeError(f"{path} is busy (used by another program).", code="busy")
        return fd

    a = importer.analyze([str(src)], smart_check=False, fd_provider=exclusive_helper)
    assert a.sources[0].kind == "block" and a.os.distro == "freedos"
    cfg = importer.run(a, importer.ImportOptions(mode="linked", verify_hash=True),
                       fd_provider=exclusive_helper)
    d = cfg.disks[0]
    assert d.source.kind == "block" and d.source.sha256_before == before
    assert [r["unchanged"] for r in importer.verify(cfg, fd_provider=exclusive_helper)] == [True]
    assert sha256(src) == before


@pytest.mark.skipif(bool(freedos.available()), reason="cannot build FreeDOS image")
@pytest.mark.parametrize("broken", [
    dict(boot_code=False),                                          # mkfs.fat placeholder only
    dict(fat=32, boot_code=False, active=False, mbr_code=False),     # everything missing
])
def test_dos_disk_prepared_on_linux_is_made_bootable(images, broken):
    """A DOS/Windows 9x disk whose files were copied onto a Linux-formatted partition.

    Regression for a Windows 98 VDI that stopped at mkfs.fat's "This is not a
    bootable disk" message: Rehome repairs the boot chain in the overlay with
    SYSLINUX + chain.c32, and the source stays unchanged.
    """
    from rehome import importer
    from rehome.vm import runner
    src = images / f"freedos-broken-{len(broken)}.img"
    if not src.exists():
        freedos.build(src, **broken)
    before = sha256(src)
    a = importer.analyze([str(src)], smart_check=False)
    assert a.os.distro == "freedos" and a.os.details["boot_chain"]["problems"]
    cfg = importer.run(a, importer.ImportOptions(mode="linked"))
    try:
        runner.start(cfg, headless=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and freedos.MARKER not in runner.screen_text(cfg):
            time.sleep(0.5)
        assert freedos.MARKER in runner.screen_text(cfg), runner.screen_text(cfg)
    finally:
        runner.force_off(cfg)
    assert sha256(src) == before


@pytest.mark.skipif(bool(freedos.available()), reason="cannot build FreeDOS image")
@pytest.mark.parametrize("broken, expect", [
    (("syslinux",), "extlinux"),                          # no mtools in libguestfs's helper VM
    (("syslinux", "extlinux"), "syslinux on this computer"),   # no SYSLINUX in it at all
])
def test_boot_repair_when_libguestfs_cannot_install_syslinux(images, monkeypatch, broken, expect):
    """libguestfs's syslinux needs mtools in its helper VM, and both it and extlinux need the
    SYSLINUX package there (built from this computer's packages, e.g. on Arch). Rehome falls
    back to extlinux, then to this computer's own syslinux on a copy of the partition."""
    from rehome import importer
    from rehome.patch import legacy
    from rehome.vm import runner
    real = legacy._install_boot_sector
    used = []

    def failing(g, device, workdir):
        def syslinux(*a, **k):
            raise RuntimeError("syslinux: sh: 1: mcopy: not found\nsyslinux: failed to create "
                               "ldlinux.sys")

        def extlinux(*a, **k):
            raise RuntimeError("extlinux: feature 'extlinux' is not available in this build of "
                               "libguestfs")
        if "syslinux" in broken:
            g.syslinux = syslinux
        if "extlinux" in broken:
            g.extlinux = extlinux
        used.append(real(g, device, workdir))
        return used[-1]
    monkeypatch.setattr(legacy, "_install_boot_sector", failing)
    src = images / f"freedos-fat32-repair-{len(broken)}.img"
    if not src.exists():
        freedos.build(src, fat=32, boot_code=False, active=False, mbr_code=False)
    before = sha256(src)
    a = importer.analyze([str(src)], smart_check=False)
    cfg = importer.run(a, importer.ImportOptions(mode="copy"))
    assert used == [expect]
    assert not list(cfg.dir.glob("syslinux-*"))        # the partition copy was removed
    try:
        runner.start(cfg, headless=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and freedos.MARKER not in runner.screen_text(cfg):
            time.sleep(0.5)
        assert freedos.MARKER in runner.screen_text(cfg), runner.screen_text(cfg)
    finally:
        runner.force_off(cfg)
    assert sha256(src) == before


STARTED_LIKE_GRUB4DOS = ("IOSYS ax=0000 bx=0000 dx=F880 sp=0400 bp=7C00 ss=0000 ds=0000 es=0000 "
                         "cs=0070 sum=OK")


def _boot_report(cfg, wait=90):
    """What the stand-in IO.SYS (tests/fixtures/iosys_stub.asm) printed, the screen's 80-column
    lines joined (its report is longer than one)."""
    from rehome.vm import runner
    try:
        runner.start(cfg, headless=True)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            text = runner.screen_text(cfg)
            if "IOSYS " in text or "MSLOAD " in text or "Rehome" in text:
                time.sleep(1)
                break
            time.sleep(0.5)
        return "".join(runner.screen_text(cfg).splitlines())
    finally:
        runner.force_off(cfg)


def _patch_entry(cfg, pid):
    import json
    for line in (cfg.dir / "logs" / "patch.log").read_text().splitlines():
        e = json.loads(line)
        if e.get("id") == pid:
            found = e
    return found


needs_nasm_mtools = pytest.mark.skipif(
    not all(__import__("shutil").which(t) for t in ("nasm", "mcopy", "mkfs.fat")),
    reason="needs nasm, mtools and mkfs.fat")


@needs_nasm_mtools
@pytest.mark.parametrize("fat, layout", [
    # Linux-made: placeholder boot code, hidden sectors 0, type 0x83, no MBR code, nothing active
    (32, dict(hidden=0, part_id=0x83, mbr_code=False, active=False)),
    (16, dict(hidden=None)),                                          # only the boot code missing
    # past 8 GB with a CHS type, and past the 128 GiB Windows 9x's IDE driver can reach
    (32, dict(start=268_000_000, part_id=0x0B, hidden=None)),
])
def test_windows98_disk_prepared_on_linux_starts_io_sys(images, fat, layout):
    """Regression: a Windows 98 VDI formatted with mkfs.fat stopped at SYSLINUX's
    "Booting..." (chain.c32's msdos7 mode starts IO.SYS with SS:BP pointing into IO.SYS).
    Rehome's own boot sector loads the rest of IO.SYS (here a fragmented stand-in that
    reports how it was started) to 0070:0000 and starts it like grub4dos does."""
    from rehome import importer
    src = images / f"win98-boot-{fat}-{'-'.join(f'{k}{v}' for k, v in layout.items())}.img"
    if not src.exists():
        W.build_win9x_bootable(src, W.fake_msdos7_iosys(), fat=fat, **layout)
    frags = W.fat_chain(src, layout.get("start", 63), b"IO      SYS")
    assert sum(b != a + 1 for a, b in zip(frags, frags[1:])) >= 10      # IO.SYS is fragmented
    before = sha256(src)
    a = importer.analyze([str(src)], smart_check=False)
    assert a.os.family == "win9x" and a.os.details["boot_chain"]["problems"]
    cfg = importer.run(a, importer.ImportOptions(mode="linked"))
    entry = _patch_entry(cfg, "dos.boot_record")
    assert entry["status"] == "applied", entry
    assert any("Rehome's Windows 9x boot sector" in c for c in entry["changes"]), entry
    if layout.get("part_id") == 0x83:
        assert any("type 0x83 → 0x0C" in c for c in entry["changes"]), entry
        assert any("hidden sectors 0 → 63" in c for c in entry["changes"]), entry
        assert any("master boot record code written" in c for c in entry["changes"]), entry
    big = _patch_entry(cfg, "win9x.big_disk")
    if layout.get("start"):
        assert any("type 0x0B → 0x0C" in c for c in entry["changes"]), entry
        assert big["status"] == "info" and "ends at 128 GiB" in big["message"], big
    else:
        assert big["status"] == "not-needed", big
    assert STARTED_LIKE_GRUB4DOS in _boot_report(cfg)
    assert sha256(src) == before


@needs_nasm_mtools
def test_windows98_started_by_an_older_rehomes_syslinux_is_repaired(images):
    """Rehome 0.3.5/0.3.6 installed SYSLINUX + chain.c32 msdos7=/io.sys, which hangs: running
    the fix again replaces it with Rehome's boot sector and removes the SYSLINUX files."""
    import json
    import shutil
    import subprocess
    from rehome import importer
    from rehome.inspect.gfs import Drive, session
    from rehome.inspect.model import InspectionReport
    from rehome.patch import engine, legacy
    modules = legacy._syslinux_modules()
    if not modules or not shutil.which("syslinux"):
        pytest.skip("SYSLINUX is not installed")
    src = images / "win98-old-rehome-syslinux.img"
    W.build_win9x_bootable(src, W.fake_msdos7_iosys(), fat=32, hidden=None)
    offset = f"{src}@@{63 * 512}"
    cfg_text = ("# Written by Rehome: this partition had no working boot code, so SYSLINUX\r\n"
                "DEFAULT system\r\nPROMPT 0\r\nTIMEOUT 0\r\n"
                "LABEL system\r\n  COM32 chain.c32\r\n  APPEND fs msdos7=/io.sys\r\n")
    cfg_file = images / "syslinux.cfg"
    cfg_file.write_text(cfg_text, newline="")
    env = {"MTOOLS_SKIP_CHECK": "1", "PATH": "/usr/bin:/bin"}
    subprocess.run(["mmd", "-i", offset, "::syslinux"], check=True, env=env)
    for f in [*(f"{modules}/{m}" for m in legacy.CHAIN_MODULES), str(cfg_file)]:
        subprocess.run(["mcopy", "-i", offset, f, "::syslinux/"], check=True, env=env)
    subprocess.run(["syslinux", "--install", "--offset", str(63 * 512), "--directory", "/syslinux",
                    str(src)], check=True, env=env)
    a = importer.analyze([str(src)], smart_check=False)
    assert a.os.details["boot_chain"]["vbr_code"] == "syslinux"
    cfg = importer.run(a, importer.ImportOptions(mode="linked"))
    entry = _patch_entry(cfg, "dos.boot_record")
    assert entry["status"] == "applied" and "chain.c32" in entry["message"], entry
    assert any("SYSLINUX files of an earlier Rehome version removed" in c
               for c in entry["changes"]), entry
    assert STARTED_LIKE_GRUB4DOS in _boot_report(cfg)
    with session([Drive(str(cfg.resolve(cfg.disks[0].file)), "qcow2")]) as g:
        g.mount_ro("/dev/sda1", "/")
        assert not g.exists("/syslinux")
    # Running the fix again (`rehome patch run VM --only dos.boot_record`) finds nothing to do.
    o = InspectionReport.from_dict(json.loads((cfg.dir / "inspection.json").read_text())).oses[0]
    results = engine.run(cfg, o, importer.ImportOptions(), only=["dos.boot_record"])
    assert [r.status for r in results] == ["not-needed"], results


@needs_nasm_mtools
def test_windows_me_boot_sector_is_not_replaced(images):
    """Windows Me's IO.SYS is compressed and needs Microsoft's boot sector: Rehome says so."""
    from rehome import importer
    iosys = bytearray(W.fake_msdos7_iosys())
    iosys[0x800:0x802] = b"CM"
    src = images / "winme-boot.img"
    W.build_win9x_bootable(src, bytes(iosys), fat=32, kernel32=(4, 90, 3000, 0))
    a = importer.analyze([str(src)], smart_check=False)
    assert a.os.distro == "winme"
    cfg = importer.run(a, importer.ImportOptions(mode="linked"))
    entry = _patch_entry(cfg, "dos.boot_record")
    assert entry["status"] == "skipped" and "SYS C:" in entry["message"], entry
