import json

import pytest

from rehome.errors import RehomeError
from rehome.host.firmware import Firmware
from rehome.host.qemu import Machine, QemuCaps
from rehome.vm.config import CdRom, Disk, DiskSource, Nic, Share, VMConfig
from rehome.vm.qemu_cmd import LaunchOptions, RuntimePaths, build

DEVICES = {"VGA", "cirrus-vga", "virtio-vga", "qxl-vga", "virtio-blk-pci", "ide-hd", "ide-cd",
           "nvme", "qemu-xhci", "usb-tablet", "piix3-usb-uhci", "e1000e", "e1000", "rtl8139",
           "virtio-net-pci", "ich9-intel-hda", "hda-duplex", "AC97", "sb16", "tpm-crb",
           "tpm-tis", "virtio-serial-pci", "virtserialport", "vhost-user-fs-pci",
           "virtio-rng-pci", "virtio-balloon-pci", "pvpanic", "virtio-scsi-pci", "scsi-hd",
           "ahci", "pcnet", "ne2k_isa", "usb-storage"}


def caps(**kw):
    c = QemuCaps(binary="/usr/bin/qemu-system-x86_64", version=(9, 1, 0),
                 devices=set(DEVICES), displays={"dbus", "none", "egl-headless"},
                 audiodevs={"none", "pipewire"}, accels={"kvm", "tcg"})
    c.machines = {"pc-q35-9.1": Machine("pc-q35-9.1", alias="q35"),
                  "pc-i440fx-9.1": Machine("pc-i440fx-9.1", alias="pc")}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture
def vm(tmp_path):
    cfg = VMConfig(name="Test VM")
    cfg.path = tmp_path / "vm"
    (cfg.path / "disks").mkdir(parents=True)
    (cfg.path / "disks" / "disk0.qcow2").write_bytes(b"")
    cfg.disks.append(Disk(id="disk0", file="disks/disk0.qcow2", bus="virtio", boot_index=1))
    cfg.nics.append(Nic(id="net0", model="virtio-net-pci", mac="52:54:00:12:34:56"))
    return cfg


def rt(tmp_path):
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    return RuntimePaths(d)


def argv_of(cfg, tmp_path, c=None, **opts):
    o = LaunchOptions(**opts)
    return build(cfg, c or caps(), rt(tmp_path), o)


def values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def blockdevs(argv):
    return [json.loads(v) for v in values(argv, "-blockdev")]


def test_basic_q35_kvm(vm, tmp_path):
    plan = argv_of(vm, tmp_path, accel="kvm")
    a = plan.argv
    assert values(a, "-machine") == ["type=q35"]
    assert values(a, "-accel") == ["kvm"]
    assert values(a, "-cpu") == ["host"]
    assert "-nodefaults" in a
    assert any(v.startswith("virtio-blk-pci,drive=disk0") and "bootindex=1" in v
               for v in values(a, "-device"))
    assert values(a, "-display") == ["dbus,p2p=yes"]
    assert any("qemu-vdagent" in v for v in values(a, "-chardev"))
    # NAT without passt falls back to slirp
    assert values(a, "-netdev") == ["user,id=hostnet-net0"]


def test_tcg_uses_max_cpu_and_no_hyperv(vm, tmp_path):
    vm.machine.hyperv = True
    vm.os.family = "windows"
    a = argv_of(vm, tmp_path, accel="tcg").argv
    assert values(a, "-cpu") == ["max"]
    assert "-global" not in a or not any("kvm-pit" in g for g in values(a, "-global"))
    k = argv_of(vm, tmp_path, accel="kvm").argv
    assert "hv-relaxed=on" in values(k, "-cpu")[0]
    assert "kvm-pit.lost_tick_policy=delay" in values(k, "-global")


def test_pc_ide_layout_and_legacy_devices(vm, tmp_path):
    vm.machine.type = "pc"
    vm.os.family = "win9x"
    vm.disks[0].bus = "ide"
    vm.cdroms.append(CdRom(id="cd0", bus="ide"))
    vm.nics[0].model = "ne2k_isa"
    vm.sound.device = "sb16"
    vm.display.device = "cirrus-vga"
    vm.usb.controller = "none"
    vm.input.pointer = "ps2"
    a = argv_of(vm, tmp_path, accel="tcg", audio_backend="pipewire").argv
    devs = values(a, "-device")
    assert any(d.startswith("ide-hd,bus=ide.0,unit=0") for d in devs)
    assert any(d.startswith("ide-cd,bus=ide.1,unit=0") for d in devs)
    assert "sb16,audiodev=snd0" in devs
    assert any(d.startswith("ne2k_isa") for d in devs)
    assert any(d.startswith("cirrus-vga") for d in devs)
    assert "pcspk-audiodev=snd0" in values(a, "-machine")[0]
    # no virtio-serial for 9x
    assert not any(d.startswith("virtio-serial") for d in devs)


def test_windows_9x_gets_rehomes_vga_bios(vm, tmp_path, monkeypatch):
    """Regression: Windows 98 crashed when it shut down ("A fatal exception 0D has occurred
    at C000:00002007"), in QEMU's VGA BIOS; Rehome's build has no debug output (D43)."""
    data = tmp_path / "data"
    (data / "firmware").mkdir(parents=True)
    rom = data / "firmware" / "vgabios-cirrus.bin"
    rom.write_bytes(b"\x55\xaa")
    monkeypatch.setenv("REHOME_DATA_DIR", str(data))
    vm.machine.type = "pc"
    vm.os.family = "win9x"
    vm.display.device = "cirrus-vga"

    def cirrus():
        devs = values(argv_of(vm, tmp_path, accel="tcg").argv, "-device")
        return next(d for d in devs if d.startswith("cirrus-vga"))
    assert f"romfile={rom}" in cirrus()
    vm.os.family = "windows"                # Windows XP and later keep QEMU's own
    assert "romfile" not in cirrus()
    vm.os.family = "win9x"
    rom.unlink()                            # not built: QEMU's own
    assert "romfile" not in cirrus()


def test_windows_9x_vms_are_watched_for_the_end_of_shutdown(vm, tmp_path):
    """D45: a third QMP socket and the watcher sidecar, for Windows 9x only."""
    vm.machine.type = "pc"
    vm.os.family = "win9x"
    plan = argv_of(vm, tmp_path, accel="tcg", log_dir=tmp_path)
    assert any("qmp-watch.sock" in c for c in values(plan.argv, "-chardev"))
    assert len(values(plan.argv, "-mon")) == 3
    (sc,) = [s for s in plan.sidecars if s.name == "watch"]
    assert sc.argv[-3:] == [vm.id, str(tmp_path / "run" / "qmp-watch.sock"),
                            str(tmp_path / "watch.log")]
    vm.os.family = "windows"
    plan = argv_of(vm, tmp_path, accel="tcg")
    assert len(values(plan.argv, "-mon")) == 2 and not plan.sidecars


def test_q35_sata_ports(vm, tmp_path):
    vm.disks[0].bus = "sata"
    vm.cdroms.append(CdRom(id="cd0", bus="sata", file=""))
    a = argv_of(vm, tmp_path, accel="kvm").argv
    devs = values(a, "-device")
    assert any(d.startswith("ide-hd,bus=ide.0,drive=disk0") for d in devs)
    assert any(d.startswith("ide-cd,bus=ide.1,id=cd0") for d in devs)


def test_uefi_secure_boot_and_tpm(vm, tmp_path):
    vm.machine.firmware = "uefi-secure"
    vm.machine.tpm = "2.0"
    fw = Firmware(path="d.json", description="sb", interface_types=["uefi"], device="flash",
                  executable="/fw/code.fd", nvram_template="/fw/vars.fd",
                  features={"secure-boot", "enrolled-keys", "requires-smm"},
                  targets=[("x86_64", ["pc-q35-*"])])
    plan = argv_of(vm, tmp_path, accel="kvm", firmware=fw, nvram_path="/vm/nvram.qcow2",
                   swtpm="/usr/bin/swtpm")
    a = plan.argv
    m = values(a, "-machine")[0]
    assert "pflash0=fw-code" in m and "pflash1=fw-vars" in m and "smm=on" in m
    assert "driver=cfi.pflash01,property=secure,value=on" in values(a, "-global")
    bds = {b["node-name"]: b for b in blockdevs(a)}
    assert bds["fw-code-file"]["filename"] == "/fw/code.fd" and bds["fw-code"]["read-only"]
    assert bds["fw-vars"]["driver"] == "qcow2"
    assert "tpm-crb,tpmdev=tpm0" in values(a, "-device")
    sw = [s for s in plan.sidecars if s.name == "swtpm"][0]
    assert "--tpm2" in sw.argv and "--terminate" in sw.argv


def test_secure_boot_rejects_pc(vm, tmp_path):
    vm.machine.type = "pc"
    vm.machine.firmware = "uefi-secure"
    fw = Firmware(path="d", description="", interface_types=["uefi"], device="flash",
                  executable="/c", nvram_template="/v", features={"requires-smm", "secure-boot"})
    with pytest.raises(RehomeError):
        argv_of(vm, tmp_path, accel="kvm", firmware=fw, nvram_path="/n")


def test_linked_image_source(vm, tmp_path):
    src = tmp_path / "win.vmdk"
    src.write_bytes(b"")
    vm.disks[0].source = DiskSource(kind="image", path=str(src), format="vmdk", mode="linked")
    a = argv_of(vm, tmp_path, accel="kvm").argv
    bds = {b["node-name"]: b for b in blockdevs(a)}
    assert bds["disk0-src"]["driver"] == "vmdk"
    assert bds["disk0-src"]["read-only"] is True
    assert bds["disk0-src"]["file"]["read-only"] is True
    assert bds["disk0"]["backing"] == "disk0-src"


def test_linked_block_source_uses_fdset(vm, tmp_path):
    vm.disks[0].source = DiskSource(kind="block", path="/dev/disk/by-id/ata-X", mode="linked")
    a = argv_of(vm, tmp_path, accel="kvm", fdsets={"disk0": 3}).argv
    bds = {b["node-name"]: b for b in blockdevs(a)}
    assert bds["disk0-src"]["file"] == {"driver": "host_device", "filename": "/dev/fdset/3",
                                        "read-only": True, "aio": "threads"}
    with pytest.raises(RehomeError):
        argv_of(vm, tmp_path, accel="kvm")   # fd not provided


def test_copy_mode_has_no_backing(vm, tmp_path):
    vm.disks[0].source = DiskSource(kind="block", path="/dev/sdb", mode="copy")
    a = argv_of(vm, tmp_path, accel="kvm").argv
    bds = {b["node-name"]: b for b in blockdevs(a)}
    assert "backing" not in bds["disk0"]


def test_passt_and_forwards(vm, tmp_path):
    vm.nics[0].port_forwards = ["tcp:2222:22"]
    plan = argv_of(vm, tmp_path, accel="kvm", passt="/usr/bin/passt")
    assert values(plan.argv, "-netdev")[0].startswith("stream,id=hostnet-net0,server=off,addr.type=unix")
    passt = plan.sidecars[0]
    assert passt.name == "passt" and "-t" in passt.argv and "2222:22" in passt.argv
    slirp = argv_of(vm, tmp_path, accel="kvm").argv
    assert values(slirp, "-netdev") == ["user,id=hostnet-net0,hostfwd=tcp:127.0.0.1:2222-:22"]


def test_shares_need_memfd(vm, tmp_path):
    vm.shares.append(Share(tag="home", path=str(tmp_path)))
    plan = argv_of(vm, tmp_path, accel="kvm", virtiofsd="/usr/libexec/virtiofsd")
    a = plan.argv
    assert "memory-backend=mem" in values(a, "-machine")[0]
    assert any(o.startswith("memory-backend-memfd,id=mem") for o in values(a, "-object"))
    assert any(d.startswith("vhost-user-fs-pci") and "tag=home" in d for d in values(a, "-device"))
    with pytest.raises(RehomeError):
        argv_of(vm, tmp_path, accel="kvm")   # virtiofsd missing


def test_display_fallbacks(vm, tmp_path):
    vm.display.gl = True
    c = caps(displays={"none"})            # no dbus, no GL
    plan = argv_of(vm, tmp_path, c=c, accel="kvm")
    assert values(plan.argv, "-display") == ["none"]
    assert values(plan.argv, "-vnc")
    assert plan.console == "vnc"
    assert any("3D" in w for w in plan.warnings)
    headless = argv_of(vm, tmp_path, accel="kvm", headless=True)
    assert values(headless.argv, "-display") == ["none"]


def test_usb_tablet_without_controller(vm, tmp_path):
    vm.usb.controller = "none"
    plan = argv_of(vm, tmp_path, accel="kvm")
    assert not any(d.startswith("usb-tablet") for d in values(plan.argv, "-device"))
    assert plan.warnings


def test_passthrough_is_reserved(vm, tmp_path):
    vm.passthrough.pci = ["0000:01:00.0"]
    with pytest.raises(RehomeError, match="pass-through"):
        argv_of(vm, tmp_path, accel="kvm")


def test_missing_disk_file(vm, tmp_path):
    (vm.path / "disks" / "disk0.qcow2").unlink()
    with pytest.raises(RehomeError, match="missing"):
        argv_of(vm, tmp_path, accel="kvm")


def test_one_shot_direct_boot_does_not_reboot(vm, tmp_path):
    from rehome.vm.config import DirectBoot
    (vm.path / "vmlinuz").write_bytes(b"")
    vm.direct_boot = DirectBoot(kernel="vmlinuz", append="autoinstall", once=True)
    a = argv_of(vm, tmp_path, accel="tcg").argv
    assert values(a, "-kernel") == [str(vm.path / "vmlinuz")] and "-no-reboot" in a
    vm.direct_boot.once = False
    assert "-no-reboot" not in argv_of(vm, tmp_path, accel="tcg").argv
