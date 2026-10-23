"""Guest tools: which driver disc a VM gets, and the switch to faster devices."""

import shutil
import subprocess

import pytest

from rehome.errors import RehomeError
from rehome.vm import create, guest_tools, library, qemu_cmd

pytestmark = pytest.mark.skipif(not shutil.which("qemu-img"), reason="qemu-img is not installed")


def _vm(os_id: str, **kw):
    return create.create(create.NewVMSpec(name=os_id, os=os_id, network="none", **kw))


def test_windows_xp_gets_the_spice_tools_and_qxl(tmp_path):
    cfg = _vm("winxp", disk_size=2**30)
    assert guest_tools.legacy_windows(cfg)
    p = guest_tools.plan(cfg)
    assert p.kind == "windows-iso" and p.needs_download and p.url == guest_tools.XP_TOOLS_URL
    assert "QXL" in p.message and "Service Pack 3" in p.message
    assert guest_tools.paravirt_changes(cfg) == {"display": "qxl-vga"}
    assert guest_tools.describe_changes({"display": "qxl-vga"}) == "display: QXL graphics"


@pytest.mark.skipif(not shutil.which("xorriso"), reason="xorriso is not installed")
def test_xp_tools_disc(tmp_path):
    from rehome.media import isoscan
    exe = tmp_path / "spice-guest-tools-0.65.exe"
    exe.write_bytes(b"MZ" + b"\0" * 4000)
    iso = guest_tools.make_xp_tools_iso(exe)
    with isoscan.Iso9660(iso) as r:
        assert r.volume_id == "REHOME_TOOLS"
        assert r.read("spice-guest-tools-0.65.exe") == exe.read_bytes()
        readme = r.read("README.TXT").decode()
    assert "\r\n" in readme and "Service Pack 3" in readme and "1920×1080" in readme
    cfg = _vm("winxp", disk_size=0)
    p = guest_tools.plan(cfg)
    assert p.iso == iso and not p.needs_download


@pytest.mark.parametrize("os_id", ["winxp", "windows-xp"])
def test_xp_chosen_by_hand_or_detected(os_id):
    """The New VM wizard's "Operating system" choice is a profile; it must still count as XP."""
    cfg = _vm(os_id, disk_size=0)
    assert cfg.os.version == "5.1" and guest_tools.windows_era(cfg) == "xp"
    assert guest_tools.plan(cfg).url == guest_tools.XP_TOOLS_URL
    # VMs made before the version was recorded: the profile decides.
    cfg.os.version = ""
    assert guest_tools.windows_era(cfg) == "xp"
    assert "virtio-win" not in guest_tools.plan(cfg).url


def test_windows_7_gets_the_tools_libosinfo_lists(monkeypatch):
    for os_id in ("win7", "windows-vista-7"):
        cfg = _vm(os_id, disk_size=0)
        p = guest_tools.plan(cfg)
        assert guest_tools.windows_era(cfg) == "vista7"
        assert p.url.endswith("/spice-guest-tools-0.141.exe") and p.needs_download
        assert "Windows 8" in p.message           # why not current virtio-win
    assert guest_tools.windows_era(_vm("win8.1", disk_size=0)) == "modern"


def test_older_and_newer_windows(monkeypatch):
    for os_id in ("win2k", "windows-2000", "windows-nt4"):
        old = _vm(os_id, disk_size=0)
        assert not guest_tools.legacy_windows(old)
        assert guest_tools.plan(old).kind == "none"
    win10 = _vm("win10", disk_size=0)
    monkeypatch.setattr(guest_tools, "virtio_win_path", lambda: None)
    p = guest_tools.plan(win10)
    assert p.kind == "windows-iso" and p.url == guest_tools.VIRTIO_WIN_URL


def _has_qxl(monkeypatch, yes: bool):
    """Pretend this computer's QEMU does (or does not) have the QXL display."""
    import dataclasses
    from rehome.host import info
    real = info.gather()
    caps = real.caps("x86_64")
    if caps is None:
        pytest.skip("no QEMU for x86_64")
    devices = (caps.devices | {"qxl-vga"}) if yes else ((caps.devices - {"qxl-vga"}) or {"VGA"})
    fake = dataclasses.replace(caps, devices=devices)
    monkeypatch.setattr(type(real), "caps", lambda self, arch: fake)


def test_switching_xp_needs_qemus_qxl_module(monkeypatch):
    cfg = _vm("winxp", disk_size=2**30)
    _has_qxl(monkeypatch, False)
    with pytest.raises(RehomeError, match="no QXL") as e:
        guest_tools.switch_to_paravirt(cfg)
    assert "qemu-system-modules-spice" in e.value.fix
    assert library.reload(cfg).display.device == "cirrus-vga"
    assert not library.reload(cfg).snapshots


def test_switching_xp_to_qxl_keeps_a_checkpoint(monkeypatch):
    _has_qxl(monkeypatch, True)
    cfg = _vm("winxp", disk_size=2**30)
    assert cfg.display.device == "cirrus-vga"
    log = guest_tools.switch_to_paravirt(cfg)
    cfg = library.reload(cfg)
    assert log == ["display: cirrus-vga → qxl-vga"]
    assert cfg.display.device == "qxl-vga" and cfg.disks[0].bus == "ide"
    assert [s.name for s in cfg.snapshots] == ["Before switching to virtio"]


@pytest.mark.skipif(not shutil.which("qemu-system-x86_64"), reason="QEMU is not installed")
def test_qxl_display_in_the_qemu_command(tmp_path):
    from rehome.host import info
    cfg = _vm("winxp", disk_size=0)
    cfg.display.device = "qxl-vga"
    host = info.gather()
    caps = host.caps("x86_64")
    if caps and caps.devices and not caps.has_device("qxl-vga"):
        pytest.skip("this QEMU has no QXL display")
    from rehome.vm import runner
    plan = qemu_cmd.build(cfg, caps, runner.runtime(cfg), qemu_cmd.LaunchOptions(headless=True))
    devices = [plan.argv[i + 1] for i, a in enumerate(plan.argv) if a == "-device"]
    assert any(d.startswith("qxl-vga,") for d in devices), devices
    # QEMU accepts it (the device exists and its options are valid).
    out = subprocess.run(["qemu-system-x86_64", "-device", "qxl-vga,help"], capture_output=True,
                         text=True)
    assert "vgamem_mb" in out.stdout
