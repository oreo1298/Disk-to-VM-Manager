import lzma
import os
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


def _input_dev(sysdir: Path, udev: Path, minor: int, props: list[str]):
    ev = sysdir / f"{sysdir.name}:1.0" / f"0003:1234:{minor:04X}.0001" / "input" / \
        f"input{minor}" / f"event{minor}"
    ev.mkdir(parents=True)
    (ev / "dev").write_text(f"13:{64 + minor}\n")
    udev.mkdir(exist_ok=True)
    (udev / f"c13:{64 + minor}").write_text("".join(f"E:{p}=1\n" for p in ["ID_INPUT"] + props))


def test_usb_pointers_and_touchscreens_stay_game_controllers_go(tmp_path):
    dev = usb.UsbHostDevice(bus=1, devnum=2, vendor_id="0627", product_id="0001")
    udev = tmp_path / "udev"
    tablet = _usbdev(tmp_path, "3-1", [("03", "00", "00")])     # no boot protocol
    _input_dev(tablet, udev, 1, ["ID_INPUT_TABLET", "ID_INPUT_MOUSE"])
    touch = _usbdev(tmp_path, "3-2", [("03", "00", "00")])
    _input_dev(touch, udev, 2, ["ID_INPUT_TOUCHSCREEN"])
    pad = _usbdev(tmp_path, "3-3", [("03", "00", "00")])
    _input_dev(pad, udev, 3, ["ID_INPUT_JOYSTICK"])
    assert "touchscreen" in runtime.usb_reason_to_skip(dev, tablet, [], set(), udev)
    assert "touchscreen" in runtime.usb_reason_to_skip(dev, touch, [], set(), udev)
    assert runtime.usb_reason_to_skip(dev, pad, [], set(), udev) == ""


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
    sdl = argv_of(cfg, tmp_path, caps(displays={"gtk", "sdl", "none"})).argv
    assert values(sdl, "-display") == ["sdl,gl=on,window-close=off"] and "-full-screen" in sdl
    plain = argv_of(cfg, tmp_path, caps(displays={"sdl"}), kiosk_gl=False).argv
    assert values(plain, "-display") == ["sdl,window-close=off"]
    gtk = argv_of(cfg, tmp_path, caps(displays={"gtk", "none"})).argv
    assert values(gtk, "-display") == [("gtk,full-screen=on,zoom-to-fit=on,show-menubar=off,"
                                        "show-tabs=off,window-close=off")]


def test_guest_is_offered_the_screen_resolution(tmp_path):
    from tests.unit.test_qemu_cmd import argv_of, values
    cfg = VMConfig(name="Linux")
    cfg.path = tmp_path / "vm"
    cfg.path.mkdir()
    cfg.display.console = "kiosk"
    for device, expect in (("virtio-vga", "virtio-vga,xres=1920,yres=1080,id=video0"),
                           ("VGA", "VGA,xres=3840,yres=2160,vgamem_mb=32,id=video0"),
                           ("cirrus-vga", "cirrus-vga,id=video0")):    # cirrus has no EDID
        cfg.display.device = device
        size = (3840, 2160) if device == "VGA" else (1920, 1080)
        argv = argv_of(cfg, tmp_path, display_size=size).argv
        assert expect in values(argv, "-device"), values(argv, "-device")
    cfg.display.device = "virtio-vga"
    assert "virtio-vga,id=video0" in values(argv_of(cfg, tmp_path).argv, "-device")


def test_screen_size_from_drm(tmp_path):
    for name, status, modes in (("card0-HDMI-A-1", "disconnected", ""),
                                ("card0-eDP-1", "connected", "1920x1080\n1680x1050\n"),
                                ("card1-DP-2", "connected", "3840x2160\n")):
        d = tmp_path / name
        d.mkdir()
        (d / "status").write_text(status + "\n")
        (d / "modes").write_text(modes)
    assert hardware.screen_size(tmp_path) == (1920, 1080)
    assert hardware.screen_size(tmp_path / "missing") is None
    (tmp_path / "dri").mkdir()
    assert not hardware.has_gpu_acceleration(tmp_path / "dri")
    (tmp_path / "dri" / "renderD128").write_text("")
    assert hardware.has_gpu_acceleration(tmp_path / "dri")


def test_what_happens_when_the_vm_stops():
    assert runtime.outcome_for("guest-shutdown")["outcome"] == "poweroff"
    assert runtime.outcome_for("host-signal")["outcome"] == "poweroff"
    assert runtime.outcome_for("guest-reset")["outcome"] == "restart-vm"
    assert runtime.outcome_for("host-qmp-quit")["outcome"] == "maintenance"
    assert "Ctrl+Alt+Q" in runtime.outcome_for("host-ui")["message"]
    crash = runtime.outcome_for("")
    assert crash["outcome"] == "error" and crash["reason"] == "crash" and "message" not in crash


def test_units_call_existing_commands():
    import configparser
    import re as _re

    from rehome import paths
    from rehome.cli import cmd_appliance
    units = sorted((paths.data_files_dir() / "appliance/rootfs/etc/systemd/system").glob("*.service"))
    assert {u.name for u in units} >= {"rehome-boot.service", "rehome-kiosk.service",
                                       "rehome-maintenance.service", "rehome-firstboot.service"}
    for u in units:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read_string(u.read_text())
        for key, value in cp["Service"].items():
            if key.startswith("exec"):
                for cmd in _re.findall(r"rehome appliance ([\w-]+)", value):
                    assert cmd in cmd_appliance.INSIDE, f"{u.name}: {value}"
    wrapper = paths.data_files_dir() / "appliance/rootfs/usr/local/bin/rehome"
    assert wrapper.read_text().startswith("#!/bin/sh")


def test_built_in_and_bluetooth_usb_devices_stay_unless_included(tmp_path):
    dev = usb.UsbHostDevice(bus=1, devnum=5, vendor_id="8087", product_id="0aaa")
    camera = _usbdev(tmp_path, "1-5", [("0e", "01", "00"), ("0e", "02", "00")])
    (camera / "removable").write_text("fixed\n")
    finger = _usbdev(tmp_path, "1-6", [("ff", "00", "00")])
    (finger / "removable").write_text("unknown\n")
    (finger / "port").mkdir()
    (finger / "port" / "connect_type").write_text("hardwired\n")
    dongle = _usbdev(tmp_path, "1-7", [("e0", "01", "01"), ("e0", "01", "01")])
    (dongle / "removable").write_text("removable\n")
    stick = _usbdev(tmp_path, "1-8", [("08", "06", "50")])
    (stick / "removable").write_text("removable\n")
    (stick / "port").mkdir()
    (stick / "port" / "connect_type").write_text("hotplug\n")
    assert runtime.usb_reason_to_skip(dev, camera, [], set()) == runtime.BUILT_IN
    assert runtime.usb_reason_to_skip(dev, finger, [], set()) == runtime.BUILT_IN
    assert runtime.usb_reason_to_skip(dev, dongle, [], set()) == runtime.BLUETOOTH
    assert runtime.usb_reason_to_skip(dev, stick, [], set()) == ""
    # Included in the maintenance console: given to the VM after all.
    for d in (camera, finger, dongle):
        assert runtime.usb_reason_to_skip(dev, d, [], set(), included={"8087:0aaa"}) == ""
    # Excluding wins, and a built-in keyboard can never be included.
    assert "excluded" in runtime.usb_reason_to_skip(dev, camera, [], {"8087:0aaa"},
                                                    included={"8087:0aaa"})
    kbd = _usbdev(tmp_path, "1-9", [("03", "01", "01")])
    (kbd / "removable").write_text("fixed\n")
    assert "keyboard" in runtime.usb_reason_to_skip(dev, kbd, [], set(), included={"8087:0aaa"})


def test_crash_message_names_qemus_error_and_the_usb_devices():
    from rehome.vm import runner
    log = ("=== 2026-10-22 10:00:00 starting Old PC (kvm) ===\n$ qemu-system-x86_64 -name x\n"
           "warning: something Rehome noted\n"
           "qemu-system-x86_64: warning: host doesn't support requested feature\n"
           "qemu-system-x86_64: ../hw/usb/hcd-uhci.c:967: uhci_queue_fill: Assertion "
           "`ret == TD_RESULT_ASYNC_START' failed.\n")
    line = runner.qemu_crash_line(log)
    assert line.startswith("qemu-system-x86_64: ../hw/usb/hcd-uhci.c:967")
    # Only this run counts; a quiet crash (a signal) has no line.
    assert runner.qemu_crash_line(log + "=== 2026-10-22 10:05:00 starting Old PC (kvm) ===\n"
                                        "$ qemu-system-x86_64 -name x\n") == ""
    msg = runtime.crash_message(line, ["Intel Bluetooth"])
    assert "uhci_queue_fill" in msg and "Intel Bluetooth" in msg and "diagnostics" in msg
    assert "Its last message" not in runtime.crash_message("", [])


def test_kiosk_uses_qemus_own_nat(monkeypatch):
    """The VM starts before Wi-Fi is up: passt would hand the guest link-local addresses."""
    import types
    cfg = types.SimpleNamespace(name="Old PC", machine=types.SimpleNamespace(memory_mib=512, cpus=1))
    seen = {}
    monkeypatch.setattr(runtime, "load_config", lambda: {"kiosk": {"countdown": 0}})
    monkeypatch.setattr(runtime, "the_vm", lambda: cfg)
    monkeypatch.setattr(runtime, "cmdline", lambda text=None: {})
    monkeypatch.setattr(runtime, "_disk_numbers", lambda c: (10 * 2**30, 0))
    monkeypatch.setattr(runtime, "_say", lambda lines: None)
    monkeypatch.setattr(runtime, "_wait_key", lambda s: "")
    monkeypatch.setattr(runtime, "_write_json", lambda path, data: seen.setdefault(path.name, data))
    monkeypatch.setattr(runtime, "_read_json", lambda path: {"outcome": "poweroff"})
    monkeypatch.setattr(runtime.hardware, "gather", lambda: None)
    monkeypatch.setattr(runtime.hardware, "start_checks",
                        lambda *a, **k: types.SimpleNamespace(notes=[]))
    monkeypatch.setattr(runtime.hardware, "has_gpu_acceleration", lambda: True)
    monkeypatch.setattr(runtime.hardware, "screen_size", lambda: (1920, 1080))

    def run(argv, env=None, check=False):
        seen["env"] = env
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime.kiosk() == 0
    assert seen["env"]["REHOME_NO_PASST"] == "1"


def _zoneinfo(tmp_path: Path) -> Path:
    zi = tmp_path / "zoneinfo"
    for name in ("Europe/Berlin", "America/New_York", "America/Argentina/Buenos_Aires", "Etc/UTC"):
        (zi / name).parent.mkdir(parents=True, exist_ok=True)
        (zi / name).write_bytes(b"TZif2")
    (zi / "zone1970.tab").write_text(
        "# comment\nDE,DK\t+5230+01322\tEurope/Berlin\nUS\t+404251-0740023\tAmerica/New_York\t"
        "Eastern\nAR\t-3436-05827\tAmerica/Argentina/Buenos_Aires\tBuenos Aires (BA, CF)\n")
    return zi


def test_host_timezone(tmp_path, monkeypatch):
    zi = _zoneinfo(tmp_path)
    monkeypatch.delenv("TZ", raising=False)
    link = tmp_path / "localtime"
    link.symlink_to(zi / "America" / "New_York")
    assert build.host_timezone(link, tmp_path / "none") == "America/New_York"
    plain = tmp_path / "copied-localtime"
    plain.write_bytes(b"TZif2")                         # a copy, not a link: /etc/timezone
    (tmp_path / "timezone").write_text("Europe/Berlin\n")
    assert build.host_timezone(plain, tmp_path / "timezone") == "Europe/Berlin"
    monkeypatch.setenv("TZ", ":Europe/Berlin")
    assert build.host_timezone(link, tmp_path / "none") == "Europe/Berlin"


def test_set_and_find_timezones(tmp_path):
    zi = _zoneinfo(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    assert runtime.timezone(etc) == "Etc/UTC"
    runtime.set_timezone("America/New_York", etc, zi)
    assert os.readlink(etc / "localtime") == str(zi / "America" / "New_York")
    assert runtime.timezone(etc) == "America/New_York"
    runtime.set_timezone("Europe/Berlin", etc, zi)              # replaced in place
    assert os.readlink(etc / "localtime").endswith("Europe/Berlin")
    for bad in ("Mars/Olympus", "../etc/passwd", "/etc/passwd", "Europe/../../x", ""):
        assert not runtime.valid_timezone(bad, zi)
        with pytest.raises(RehomeError):
            runtime.set_timezone(bad, etc, zi)
    assert runtime.find_timezones("berlin", zi) == ["Europe/Berlin"]
    assert runtime.find_timezones("buenos aires", zi) == ["America/Argentina/Buenos_Aires"]
    assert runtime.find_timezones("america", zi) == ["America/Argentina/Buenos_Aires",
                                                     "America/New_York"]


def test_build_checks_the_timezone(monkeypatch):
    monkeypatch.setattr(build, "host_timezone", lambda *a: "Europe/Berlin")
    assert build.timezone_for(build.Options(output=Path("x"))) == "Europe/Berlin"
    assert build.timezone_for(build.Options(output=Path("x"), timezone="Etc/UTC")) == "Etc/UTC"
    if runtime.ZONEINFO.is_dir():
        with pytest.raises(RehomeError, match="not a time zone"):
            build.timezone_for(build.Options(output=Path("x"), timezone="Mars/Olympus"))


def test_kiosk_says_what_happens_next(monkeypatch):
    """The text screen must not keep showing "Starting ..." while the computer turns off."""
    import types
    cfg = types.SimpleNamespace(name="Old PC", machine=types.SimpleNamespace(memory_mib=512, cpus=1))
    said = []
    results = iter([{"outcome": "restart-vm"}, {"outcome": "poweroff"}])
    monkeypatch.setattr(runtime, "load_config", lambda: {"kiosk": {"countdown": 0}})
    monkeypatch.setattr(runtime, "the_vm", lambda: cfg)
    monkeypatch.setattr(runtime, "cmdline", lambda text=None: {})
    monkeypatch.setattr(runtime, "_disk_numbers", lambda c: (10 * 2**30, 0))
    monkeypatch.setattr(runtime, "_say", lambda lines: said.append(lines[0]))
    monkeypatch.setattr(runtime, "_wait_key", lambda s: "")
    monkeypatch.setattr(runtime, "_write_json", lambda path, data: None)
    monkeypatch.setattr(runtime, "_read_json", lambda path: next(results))
    monkeypatch.setattr(runtime.hardware, "gather", lambda: None)
    monkeypatch.setattr(runtime.hardware, "start_checks",
                        lambda *a, **k: types.SimpleNamespace(notes=[]))
    monkeypatch.setattr(runtime.hardware, "has_gpu_acceleration", lambda: True)
    monkeypatch.setattr(runtime.hardware, "screen_size", lambda: (1920, 1080))
    monkeypatch.setattr(runtime.subprocess, "run",
                        lambda argv, env=None, check=False: types.SimpleNamespace(returncode=0))
    assert runtime.kiosk() == 0
    assert said[-2:] == ["Restarting Old PC...", "Turning off..."]
