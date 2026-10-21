import lzma
from pathlib import Path

import pytest

from rehome.appliance import build, hardware, runtime, ubuntu
from rehome.errors import RehomeError
from rehome.vm import usb
from rehome.vm.config import CdRom, Disk, DiskSource, Nic, Share, VMConfig

INRELEASE = """\
-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

Origin: Ubuntu
Codename: resolute
MD5Sum:
 0123456789abcdef0123456789abcdef  1234 main/binary-amd64/Packages.xz
SHA256:
 aaaa000000000000000000000000000000000000000000000000000000000001  1500 main/binary-amd64/Packages.xz
 bbbb000000000000000000000000000000000000000000000000000000000002  9000 universe/binary-amd64/Packages.xz
-----BEGIN PGP SIGNATURE-----
"""


def test_release_hashes_reads_only_the_sha256_section():
    h = ubuntu._release_hashes(INRELEASE)
    assert h["main/binary-amd64/Packages.xz"] == ("aaaa" + "0" * 59 + "1", 1500)
    assert h["universe/binary-amd64/Packages.xz"][1] == 9000


def test_load_release_lists_every_package_once():
    r = ubuntu.load_release()
    assert r.codename and r.url.startswith("https://")
    assert len(r.sha256) == 64
    pkgs = r.package_list
    assert pkgs == sorted(set(pkgs))
    for needed in ("linux-image-generic", "qemu-system-x86", "cage", "grub-efi-amd64-bin",
                   "grub-pc-bin", "python3"):
        assert needed in pkgs


def test_parse_print_uris_and_rejects_foreign_paths():
    text = (f"Reading package lists...\n"
            f"'file:{ubuntu.MIRROR_IN_IMAGE}/pool/main/q/qemu/qemu-utils_8.2.2%2bds-1_amd64.deb' "
            f"qemu-utils_1%3a8.2.2+ds-1_amd64.deb 2220842 MD5Sum:4ca8\n")
    assert ubuntu.parse_print_uris(text) == ["pool/main/q/qemu/qemu-utils_8.2.2+ds-1_amd64.deb"]
    with pytest.raises(RehomeError):
        ubuntu.parse_print_uris("'http://evil.example/pool/x.deb' x.deb 1 MD5Sum:00\n")
    with pytest.raises(RehomeError):
        ubuntu.parse_print_uris(f"'file:{ubuntu.MIRROR_IN_IMAGE}/pool/../../etc/x.deb' x.deb 1 "
                                "MD5Sum:00\n")


def test_package_hashes_from_index(tmp_path):
    idx = tmp_path / "dists/resolute/main/binary-amd64/Packages.xz"
    idx.parent.mkdir(parents=True)
    with lzma.open(idx, "wt") as f:
        f.write("Package: a\nVersion: 1\nFilename: pool/main/a/a_1_amd64.deb\nSize: 10\n"
                "SHA256: AB12\nDescription: x\n continued\n\n"
                "Package: b\nFilename: pool/main/b/b_2_amd64.deb\nSize: 20\nSHA256: cd34\n")
    m = ubuntu.Mirror(tmp_path, ["dists/resolute/main/binary-amd64/Packages.xz"])
    got = ubuntu.package_hashes(m, {"pool/main/a/a_1_amd64.deb", "pool/main/b/b_2_amd64.deb"})
    assert got == {"pool/main/a/a_1_amd64.deb": ("ab12", 10),
                   "pool/main/b/b_2_amd64.deb": ("cd34", 20)}


def test_grub_menu_entries_and_quoting():
    cfg = build.grub_cfg("1234-uuid", "Joe's Windows XP ünïcode", "console=ttyS0")
    assert "search --no-floppy --fs-uuid --set=root 1234-uuid" in cfg
    assert "menuentry 'Start Joe'\\''s Windows XP unicode' --id vm {" in cfg
    for entry_id in ("vm", "vm-basic", "maintenance", "maintenance-basic"):
        assert f"--id {entry_id} {{" in cfg
    assert cfg.count("root=UUID=1234-uuid ro") == 4
    assert cfg.count("console=ttyS0") == 4
    basic = cfg.split("--id vm-basic")[1].split("}")[0]
    assert "nomodeset" in basic and "rehome.graphics=basic" in basic
    assert "rehome.mode=maintenance" in cfg.split("--id maintenance {")[1].split("}")[0]
    assert "timeout_style=hidden" in cfg and 'if [ "${recordfail}" = 1 ]' in cfg


def test_fstab_uses_uuids_and_nofail_for_data():
    rows = {line.split()[1]: line.split() for line in build.fstab("R", "E", "D").splitlines()
            if not line.startswith("#")}
    assert rows["/"][0] == "UUID=R" and rows["/"][2] == "ext4"
    assert rows["/boot/efi"][0] == "UUID=E" and rows["/boot/efi"][2] == "vfat"
    assert rows["/var/lib/rehome"][0] == "UUID=D" and "nofail" in rows["/var/lib/rehome"][3]


def test_vm_for_appliance_makes_disks_standalone():
    cfg = VMConfig(name="XP")
    cfg.disks = [Disk(id="disk0", file="disks/disk0.qcow2",
                      source=DiskSource(kind="block", path="/dev/sdz", mode="linked"))]
    cfg.cdroms = [CdRom(id="cd0", file="/isos/xp.iso"), CdRom(id="cd1", file="/isos/drv.iso")]
    cfg.shares = [Share(path="/home/x")]
    cfg.nics = [Nic(mode="bridge", bridge="br0")]
    cfg.display.gl = True
    cfg.passthrough.pci = ["0000:01:00.0"]
    out = build.vm_for_appliance(cfg, build.Options(output=Path("x.img")), {"cd0": "media/xp.iso"})
    assert out.disks[0].file == "disks/disk0.qcow2" and out.disks[0].source.mode == "copy"
    assert [c.file for c in out.cdroms] == ["media/xp.iso", ""]
    assert out.display.console == "kiosk" and not out.display.gl
    assert out.shares == [] and out.passthrough.pci == []
    assert out.nics[0].mode == "nat"
    cfg.machine.accel = "kvm"
    pinned = build.vm_for_appliance(cfg, build.Options(output=Path("x"), cpu_model="Westmere"), {})
    assert pinned.machine.accel == "auto" and pinned.machine.cpu_model == "Westmere"
    assert cfg.disks[0].source.mode == "linked"          # the original is untouched
    none = build.vm_for_appliance(cfg, build.Options(output=Path("x"), network="none"), {})
    assert none.nics == []


def test_cmdline_parsing():
    c = runtime.cmdline("BOOT_IMAGE=/boot/vmlinuz root=UUID=x ro quiet rehome.mode=maintenance")
    assert c["rehome.mode"] == "maintenance" and c["quiet"] == "" and c["root"] == "UUID=x"


def test_power_key_capability_bitmap(tmp_path):
    f = tmp_path / "key"
    f.write_text("10000000000000 0\n")          # bit 116 = word 1, bit 52
    assert runtime._has_key(f, 116)
    f.write_text("4000000000000000 0\n")        # another key
    assert not runtime._has_key(f, 116)
    f.write_text("0\n")
    assert not runtime._has_key(f, 116)


def _usbdev(root: Path, name: str, interfaces: list[tuple[str, str, str]], net=False):
    d = root / name
    d.mkdir()
    for i, (cls, sub, proto) in enumerate(interfaces):
        intf = d / f"{name}:1.{i}"
        intf.mkdir()
        (intf / "bInterfaceClass").write_text(cls)
        (intf / "bInterfaceSubClass").write_text(sub)
        (intf / "bInterfaceProtocol").write_text(proto)
        if net:
            (intf / "net").mkdir()
    return d


def test_usb_skip_rules(tmp_path):
    dev = usb.UsbHostDevice(bus=1, devnum=2, vendor_id="0781", product_id="5581")
    stick = _usbdev(tmp_path, "1-1", [("08", "06", "50")])
    kbd = _usbdev(tmp_path, "1-2", [("03", "01", "01"), ("03", "00", "00")])
    pad = _usbdev(tmp_path, "1-3", [("03", "00", "00")])
    wifi = _usbdev(tmp_path, "1-4", [("ff", "ff", "ff")], net=True)
    assert runtime.usb_reason_to_skip(dev, stick, [], set()) == ""
    assert "keyboard" in runtime.usb_reason_to_skip(dev, kbd, [], set())
    assert runtime.usb_reason_to_skip(dev, pad, [], set()) == ""          # game controllers go
    assert "network" in runtime.usb_reason_to_skip(dev, wifi, [], set())
    boot = [str(stick.resolve()) + "/1-1:1.0/host0/target0:0:0/0:0:0:0/block/sdb/sdb3"]
    assert "runs from" in runtime.usb_reason_to_skip(dev, stick, boot, set())
    assert "excluded" in runtime.usb_reason_to_skip(dev, stick, [], {"0781:5581"})


def test_start_checks_fit_the_vm_to_the_computer():
    cfg = VMConfig(name="XP")
    cfg.machine.memory_mib, cfg.machine.cpus = 8192, 8
    r = hardware.Report(cpus=4, memory_mib=4096, kvm=False, virtualization="")
    c = hardware.start_checks(cfg, r, data_free=10 * 2**30, virtual_total=20 * 2**30)
    assert cfg.machine.memory_mib == 4096 - hardware.RESERVED_MIB and cfg.machine.cpus == 4
    assert c.changed
    text = " ".join(c.notes)
    assert "firmware settings" in text and "may grow" in text
    ok = VMConfig(name="98")
    ok.machine.memory_mib, ok.machine.cpus = 256, 1
    c2 = hardware.start_checks(ok, hardware.Report(cpus=8, memory_mib=16384, kvm=True))
    assert not c2.notes and not c2.changed and ok.machine.memory_mib == 256


def test_inside_commands_refuse_outside_an_appliance(monkeypatch, capsys):
    from rehome.cli import main
    monkeypatch.delenv("REHOME_APPLIANCE", raising=False)
    assert main.main(["appliance", "after-kiosk"]) == 1
    assert "only runs inside a Rehome appliance" in capsys.readouterr().err


def test_kiosk_console_kind_in_qemu_command(tmp_path):
    from tests.unit.test_qemu_cmd import argv_of, caps, values
    cfg = VMConfig(name="XP")
    cfg.path = tmp_path / "vm"
    cfg.path.mkdir()
    cfg.display.console = "kiosk"
    gtk = argv_of(cfg, tmp_path, caps(displays={"gtk", "sdl", "none"})).argv
    assert values(gtk, "-display") == [("gtk,full-screen=on,zoom-to-fit=on,show-menubar=off,"
                                        "show-tabs=off,window-close=off")]
    sdl = argv_of(cfg, tmp_path, caps(displays={"sdl", "none"})).argv
    assert values(sdl, "-display") == ["sdl,window-close=off"] and "-full-screen" in sdl


def test_what_happens_when_the_vm_stops():
    assert runtime.outcome_for("guest-shutdown")["outcome"] == "poweroff"
    assert runtime.outcome_for("host-signal")["outcome"] == "poweroff"
    assert runtime.outcome_for("guest-reset")["outcome"] == "restart-vm"
    assert runtime.outcome_for("host-qmp-quit")["outcome"] == "maintenance"
    assert "Ctrl+Alt+Q" in runtime.outcome_for("host-ui")["message"]
    crash = runtime.outcome_for("")
    assert crash["outcome"] == "error" and crash["reason"] == "crash" and "message" not in crash
