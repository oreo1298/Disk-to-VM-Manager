"""Unattended installs: answer files, answer media, DirectBoot, safety.

Uses small stand-in ISOs (tests/fixtures/fake_installers.py) with the real
volume IDs and file layout of each installer, so libosinfo identification,
boot-menu parsing and kernel extraction run for real.
"""

import logging
import re
import stat
import subprocess
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest

from rehome.errors import RehomeError
from rehome.media import unattended as u
from rehome.vm import library
from rehome.vm.config import CdRom, Disk, OsInfo, VMConfig
from tests.fixtures import fake_installers

pytestmark = pytest.mark.skipif(fake_installers.available() is not None,
                                reason=fake_installers.available() or "")

PASSWORD = "Pa ss<&>\"w0rd!"          # XML specials + space: must survive escaping
ANSWERS = u.Answers(user="alice", password=PASSWORD, full_name="Alice Example",
                    locale="de_DE", keyboard="de", timezone="Europe/Berlin")

OS = {
    "win11": dict(family="windows", distro="windows", version="10.0", build="22000",
                  edition="client", osinfo_id="http://microsoft.com/win/11",
                  name="Microsoft Windows 11"),
    "win2k25": dict(family="windows", distro="windows", version="10.0", build="26100",
                    edition="server", osinfo_id="http://microsoft.com/win/2k25",
                    name="Microsoft Windows Server 2025"),
    "fedora42-server": dict(family="linux", distro="fedora", version="42",
                            osinfo_id="http://fedoraproject.org/fedora/42", name="Fedora Linux 42"),
    "fedora42-workstation": dict(family="linux", distro="fedora", version="42",
                                 osinfo_id="http://fedoraproject.org/fedora/42",
                                 name="Fedora Linux 42"),
    "ubuntu24.04-server": dict(family="linux", distro="ubuntu", version="24.04",
                               osinfo_id="http://ubuntu.com/ubuntu/24.04", name="Ubuntu 24.04 LTS"),
    "ubuntu24.04-desktop": dict(family="linux", distro="ubuntu", version="24.04",
                                osinfo_id="http://ubuntu.com/ubuntu/24.04",
                                name="Ubuntu 24.04 LTS"),
    "linuxmint22": dict(family="linux", distro="ubuntu", version="24.04",
                        osinfo_id="http://ubuntu.com/ubuntu/24.04", name="Linux Mint 22.2"),
    "debian12-netinst": dict(family="linux", distro="debian", version="12",
                             osinfo_id="http://debian.org/debian/12", name="Debian 12"),
    "opensusetumbleweed": dict(family="linux", distro="opensuse", version="tumbleweed",
                               osinfo_id="http://opensuse.org/opensuse/tumbleweed",
                               name="openSUSE Tumbleweed"),
}


@pytest.fixture(scope="module")
def isos(tmp_path_factory):
    d = tmp_path_factory.mktemp("isos")
    return {k: fake_installers.build(k, d / f"{k}.iso") for k in fake_installers.SPECS}


def make_vm(tmp_path, isos, kind, *, firmware=None, disk_bus=None, name=None):
    facts = OS[kind]
    cfg = VMConfig(name=name or facts["name"])
    cfg.path = tmp_path / "vm"
    (cfg.path / "disks").mkdir(parents=True)
    cfg.os = OsInfo(**{k: v for k, v in facts.items()})
    windows = facts["family"] == "windows"
    cfg.machine.type = "q35"
    cfg.machine.firmware = firmware or ("uefi-secure" if windows else "uefi")
    cfg.machine.tpm = "2.0" if windows else "none"
    cfg.disks.append(Disk(id="disk0", file="disks/disk0.qcow2",
                          bus=disk_bus or ("sata" if windows else "virtio"), boot_index=2))
    cfg.cdroms.append(CdRom(id="cd0", file=str(isos[kind]), bus="sata", boot_index=1))
    library.save(cfg)
    return cfg


def volume_id(iso: Path) -> str:
    out = subprocess.run(["xorriso", "-indev", str(iso), "-pvd_info"], capture_output=True,
                         text=True).stdout
    return re.search(r"^Volume Id\s*:\s*(.*?)\s*$", out, re.M).group(1)


def iso_listing(iso: Path) -> list[str]:
    return u.iso_files(str(iso))


def iso_file(iso: Path, name: str) -> bytes:
    return u.iso_read(str(iso), name)


def assert_private(cfg):
    work = cfg.dir / u.ANSWER_DIR
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    for p in work.rglob("*"):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), (p, oct(mode))


# --- primitives ----------------------------------------------------------------------------------

def test_sha512_crypt_reference_vectors():
    # From Ulrich Drepper's SHA-crypt specification.
    assert u.sha512_crypt("Hello world!", "saltstring") == (
        "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEd"
        "FCoEOfaS35inz1")
    assert u.sha512_crypt("Hello world!", "saltstringsaltstring", rounds=10000) == (
        "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8v"
        "OuZeHBy/YTBmSK6H9qs/y3RnOaw5v.")
    h = u.sha512_crypt("x")
    assert h.startswith("$6$") and u.sha512_crypt("x", h.split("$")[2]) == h


def parse_cpio(data: bytes) -> dict[str, bytes]:
    out, i = {}, 0
    while i < len(data):
        assert data[i:i + 6] == b"070701"
        fields = [int(data[i + 6 + 8 * k:i + 14 + 8 * k], 16) for k in range(13)]
        size, namesize = fields[6], fields[11]
        name = data[i + 110:i + 110 + namesize - 1].decode()
        i += 110 + namesize
        i += -i % 4
        body = data[i:i + size]
        i += size
        i += -i % 4
        if name == "TRAILER!!!":
            break
        out[name] = body
    return out


def unpack_initrd(data: bytes) -> dict[str, bytes]:
    files, rest = {}, data
    while rest.strip(b"\0"):
        rest = rest.lstrip(b"\0")
        d = zlib.decompressobj(31)
        files.update(parse_cpio(d.decompress(rest)))
        rest = d.unused_data
    return files


def test_cpio_and_initrd_injection(tmp_path):
    assert parse_cpio(u.cpio_newc({"a/b.cfg": b"hello", "c": b""})) == {"a/b.cfg": b"hello",
                                                                         "c": b""}
    initrd = tmp_path / "initrd"
    initrd.write_bytes(fake_installers.fake_initrd() + b"x")      # odd length: needs padding
    u.inject_initrd(initrd, {"preseed.cfg": b"d-i foo"})
    data = initrd.read_bytes()
    files = unpack_initrd(data[:len(fake_installers.fake_initrd())] +
                          data[len(fake_installers.fake_initrd()) + 1:])
    assert files["preseed.cfg"] == b"d-i foo" and "init" in files
    assert stat.S_IMODE(initrd.stat().st_mode) == 0o600


def test_grub_parsing_picks_the_install_entry():
    fed = u.parse_grub(fake_installers.FEDORA_GRUB.format(vol="Fedora-S-dvd-x86_64-42", ver=42))
    assert len(fed) == 4
    e = u.pick_boot_entry(fed, "images/pxeboot/vmlinuz", "images/pxeboot/initrd.img")
    assert e.title == "Install Fedora 42" and "rd.live.check" not in e.args
    deb = u.parse_grub(fake_installers.DEBIAN_GRUB)
    e = u.pick_boot_entry(deb, "install.amd/vmlinuz", "install.amd/initrd.gz")
    assert e.title == "Install" and e.args == "vga=788 --- quiet"
    ubu = u.pick_boot_entry(u.parse_grub(fake_installers.UBUNTU_SERVER_GRUB))
    assert ubu.kernel == "casper/vmlinuz" and ubu.args == "---"
    suse = u.pick_boot_entry(u.parse_grub(fake_installers.OPENSUSE_GRUB))
    assert suse.title == "Installation"


def test_kernel_args_go_before_the_separator():
    assert u.kernel_args("---", ["autoinstall"]) == "autoinstall ---"
    assert u.kernel_args("vga=788 --- quiet", ["a=1"]) == "vga=788 a=1 --- quiet"
    assert u.kernel_args("inst.stage2=hd:LABEL=X quiet", ["b"]) == "inst.stage2=hd:LABEL=X quiet b"


def test_yaml_writer_round_trips():
    yaml = pytest.importorskip("yaml")
    data = {"autoinstall": {"version": 1, "identity": {"password": "$6$a$b", "realname": 'A "q"'},
                            "list": ["x", {"k": True}], "empty": {}}}
    assert yaml.safe_load(u.to_yaml(data)) == data


# --- answers ------------------------------------------------------------------------------------

def test_answers_validation():
    with pytest.raises(RehomeError) as ei:
        u.validate(u.Answers(user="alice"))
    assert "password" in ei.value.message
    with pytest.raises(RehomeError):
        u.validate(u.Answers(user="Bad User", password="x"), "subiquity")
    with pytest.raises(RehomeError):
        u.validate(u.Answers(user="root", password="x"), "osinfo")
    with pytest.raises(RehomeError):
        u.validate(u.Answers(user="Administrator", password="x"), "windows")
    with pytest.raises(RehomeError):
        u.validate(u.Answers(user="a:b", password="x"), "windows")
    with pytest.raises(RehomeError):
        u.validate(u.Answers(password="x", product_key="1234"))
    with pytest.raises(RehomeError):
        u.validate(u.Answers(password="x", timezone="Mars/Olympus_Mons"))
    with pytest.raises(RehomeError):
        u.validate(u.Answers(password="a\nb"))
    u.validate(u.Answers(user="Jane Doe", password="x", product_key="AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"),
               "windows")


def test_answers_never_show_the_password():
    a = u.Answers(user="alice", password="topsecret", product_key="AAAAA-BBBBB-CCCCC-DDDDD-EEEEE")
    assert "topsecret" not in repr(a) and "AAAAA" not in repr(a)


def test_defaults_come_from_the_host(monkeypatch):
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    cfg = VMConfig(name="Microsoft Windows 11 Pro")
    a = u.complete(u.Answers(password="x"), cfg, "windows")
    assert a.locale == "fr_FR" and a.user and a.timezone and a.keyboard
    assert a.hostname == "windows-11-pro" and len(a.hostname) <= 15
    lin = u.complete(u.Answers(password="x", locale="en_US"), VMConfig(name="Ubuntu 24.04 LTS"),
                     "subiquity")
    assert lin.hostname == "ubuntu-24-04-lts" and re.fullmatch(r"[a-z_][a-z0-9_-]*", lin.user)


# --- Windows ------------------------------------------------------------------------------------

NS = {"u": "urn:schemas-microsoft-com:unattend"}


def test_windows11_answer_disc(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "win11")
    assert u.check(cfg).method == "windows"
    notes = u.prepare(cfg, ANSWERS)
    answer_iso = cfg.dir / u.ANSWER_ISO
    assert volume_id(answer_iso) == "UNATTEND"
    assert "autounattend.xml" in iso_listing(answer_iso)
    xml = iso_file(answer_iso, "autounattend.xml").decode()
    root = ET.fromstring(xml)
    # Local administrator account with the (escaped) password.
    acct = root.find(".//u:LocalAccount", NS)
    assert acct.find("u:Name", NS).text == "alice"
    assert acct.find("u:Group", NS).text == "Administrators"
    assert acct.find("u:Password/u:Value", NS).text == PASSWORD
    assert root.find(".//u:HideOnlineAccountScreens", NS).text == "true"
    # UEFI: GPT layout with EFI + MSR + Windows partitions, Windows on partition 3.
    types = [e.text for e in root.findall(".//u:CreatePartition/u:Type", NS)]
    assert types == ["EFI", "MSR", "Primary"]
    assert root.find(".//u:InstallTo/u:PartitionID", NS).text == "3"
    # No product key invented, no edition forced on client media.
    assert root.find(".//u:ProductKey", NS) is None
    assert root.find(".//u:InstallFrom", NS) is None
    # Language of the media (EN-US from the volume label), user's regional settings.
    assert root.find(".//u:SetupUILanguage/u:UILanguage", NS).text == "en-US"
    assert root.find(".//u:UserLocale", NS).text == "de-DE"
    assert root.find(".//u:TimeZone", NS).text == "W. Europe Standard Time"
    assert root.find(".//u:ComputerName", NS).text == "windows-11"
    # Nothing that weakens Windows 11's requirements or the account flow.
    for forbidden in ("LabConfig", "BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck",
                      "BypassNRO", "bypassnro", "MoSetup", "AllowUpgradesWithUnsupportedTPM"):
        assert forbidden not in xml
    # Config: answer disc attached, Windows DVD still boots first, no direct boot.
    cfg2 = library.reload(cfg)
    cds = {c.id: c for c in cfg2.cdroms}
    assert cds["cd1"].file == u.ANSWER_ISO and cds["cd1"].boot_index == 0
    assert cds["cd0"].boot_index == 1 and cfg2.disks[0].boot_index == 2
    assert cfg2.direct_boot is None
    assert u.read_state(cfg)["press_boot_key"] is True
    assert any("Press any key" in n for n in notes)
    assert_private(cfg)


def test_windows_bios_layout_key_and_edition():
    xml = u.autounattend_xml(arch="x86_64", uefi=False, ui_language="en-US", locale="en-US",
                             user="bob", full_name="Bob", password="pw", hostname="pc",
                             product_key="AAAAA-BBBBB-CCCCC-DDDDD-EEEEE",
                             edition="Windows 10 Pro")
    root = ET.fromstring(xml)
    assert [e.text for e in root.findall(".//u:CreatePartition/u:Type", NS)] == ["Primary"] * 2
    assert root.find(".//u:ModifyPartition/u:Active", NS).text == "true"
    assert root.find(".//u:InstallTo/u:PartitionID", NS).text == "2"
    assert root.find(".//u:ProductKey/u:Key", NS).text == "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"
    md = root.find(".//u:InstallFrom/u:MetaData", NS)
    assert md.find("u:Key", NS).text == "/IMAGE/NAME"
    assert md.find("u:Value", NS).text == "Windows 10 Pro"


def test_windows_server_defaults_to_desktop_experience(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "win2k25")
    notes = u.prepare(cfg, ANSWERS)
    xml = (cfg.dir / u.ANSWER_DIR / "media" / "autounattend.xml").read_text()
    root = ET.fromstring(xml)
    md = root.find(".//u:InstallFrom/u:MetaData", NS)
    assert md.find("u:Key", NS).text == "/IMAGE/INDEX" and md.find("u:Value", NS).text == "2"
    assert root.find(".//u:AdministratorPassword/u:Value", NS).text == PASSWORD
    assert any("Desktop Experience" in n for n in notes)


def test_after_start_presses_the_boot_key_once(tmp_path, isos, monkeypatch):
    cfg = make_vm(tmp_path, isos, "win11")
    u.prepare(cfg, ANSWERS)
    calls = []
    monkeypatch.setattr(u, "press_boot_key", lambda c, **kw: calls.append(c.id) or 5)
    notes = u.after_start(cfg)
    assert calls == [cfg.id] and "pressed a key" in notes[0]
    assert u.read_state(cfg)["boot_key_pressed"] is True
    assert u.after_start(cfg) == [] and len(calls) == 1          # never again (reboots!)


# --- Fedora (libosinfo kickstart, OEMDRV) ----------------------------------------------------------

def test_fedora_server_kickstart(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "fedora42-server")
    assert u.check(cfg).method == "osinfo"
    notes = u.prepare(cfg, ANSWERS)
    answer_iso = cfg.dir / u.ANSWER_ISO
    assert volume_id(answer_iso) == "OEMDRV"
    assert {"fedora.ks", "ks.cfg"} <= set(iso_listing(answer_iso))
    ks = iso_file(answer_iso, "fedora.ks").decode()
    assert ks == iso_file(answer_iso, "ks.cfg").decode()
    assert re.search(r"(?m)^poweroff$", ks) and not re.search(r"(?m)^reboot", ks)
    assert "useradd -G wheel alice" in ks
    assert "lang de_DE" in ks and "keyboard 'de'" in ks and "timezone --utc Europe/Berlin" in ks
    assert "clearpart --all --drives=/dev/vda" in ks
    assert re.search(r"(?m)^cdrom$", ks)                 # install from the DVD, offline
    db = library.reload(cfg).direct_boot
    assert db.kernel == "unattended/boot/vmlinuz" and db.initrd == "unattended/boot/initrd"
    assert db.once is True
    assert (cfg.dir / db.kernel).read_bytes() == b"KERNEL"
    assert (cfg.dir / db.initrd).read_bytes() == fake_installers.fake_initrd()
    args = db.append.split()
    assert "inst.stage2=hd:LABEL=Fedora-S-dvd-x86_64-42" in args
    assert "inst.ks=hd:LABEL=OEMDRV:/fedora.ks" in args
    assert "rd.live.check" not in args and "console=ttyS0,115200n8" in args
    # After the install the disk boots first; the DVD stays as a fallback.
    c = library.reload(cfg)
    order = {d.id: d.boot_index for d in [*c.disks, *c.cdroms]}
    assert order == {"disk0": 1, "cd0": 2, "cd1": 0}
    assert any("turns off" in n for n in notes)
    assert_private(cfg)


def test_fedora_workstation_live_is_refused(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "fedora42-workstation")
    s = u.check(cfg)
    assert not s.ok and "live" in s.reason and "Fedora Server" in s.fix
    with pytest.raises(RehomeError) as ei:
        u.prepare(cfg, ANSWERS)
    assert ei.value.code == "unattended-unsupported"
    assert not (cfg.dir / u.ANSWER_DIR).exists()
    assert len(library.reload(cfg).cdroms) == 1


def test_linux_direct_boot_needs_secure_boot_off(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "ubuntu24.04-server", firmware="uefi-secure")
    s = u.check(cfg)
    assert not s.ok and "Secure Boot" in s.reason and "without Secure Boot" in s.fix


def test_kickstart_rejects_quotes_in_password(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "fedora42-server")
    with pytest.raises(RehomeError) as ei:
        u.prepare(cfg, u.Answers(user="alice", password="it's", locale="en_US",
                                 keyboard="us", timezone="UTC"))
    assert ei.value.code == "unattended-answers"


# --- Ubuntu Server (subiquity autoinstall, cidata) ----------------------------------------------------

def test_ubuntu_server_autoinstall(tmp_path, isos):
    yaml = pytest.importorskip("yaml")
    cfg = make_vm(tmp_path, isos, "ubuntu24.04-server")
    assert u.check(cfg).method == "subiquity"
    notes = u.prepare(cfg, ANSWERS)
    answer_iso = cfg.dir / u.ANSWER_ISO
    assert volume_id(answer_iso) == "cidata"
    assert {"user-data", "meta-data"} <= set(iso_listing(answer_iso))
    ud_text = iso_file(answer_iso, "user-data").decode()
    assert ud_text.startswith("#cloud-config\n")
    ai = yaml.safe_load(ud_text)["autoinstall"]
    assert ai["version"] == 1 and ai["shutdown"] == "poweroff"
    assert ai["identity"]["username"] == "alice" and ai["identity"]["realname"] == "Alice Example"
    h = ai["identity"]["password"]
    assert h.startswith("$6$") and u.sha512_crypt(PASSWORD, h.split("$")[2]) == h
    assert ai["locale"] == "de_DE.UTF-8" and ai["keyboard"] == {"layout": "de"}
    assert ai["timezone"] == "Europe/Berlin"
    assert ai["ssh"] == {"install-server": True, "allow-pw": True}
    assert ai["apt"]["fallback"] == "offline-install"
    meta = yaml.safe_load(iso_file(answer_iso, "meta-data").decode())
    assert meta["instance-id"] == f"rehome-{cfg.short_id}"
    db = library.reload(cfg).direct_boot
    assert db.append == "autoinstall console=ttyS0,115200n8 console=tty0 ---"
    assert (cfg.dir / db.kernel).read_bytes() == b"KERNEL"
    # The plain-text password is nowhere on disk (only its hash).
    for p in cfg.dir.rglob("*"):
        if p.is_file():
            assert PASSWORD.encode() not in p.read_bytes(), p
    assert any("SSH" in n for n in notes)
    assert_private(cfg)


def test_ubuntu_desktop_autoinstall(tmp_path, isos):
    yaml = pytest.importorskip("yaml")
    cfg = make_vm(tmp_path, isos, "ubuntu24.04-desktop")
    u.prepare(cfg, ANSWERS)
    ai = yaml.safe_load((cfg.dir / "unattended/media/user-data").read_text())["autoinstall"]
    assert "ssh" not in ai
    assert library.reload(cfg).direct_boot.append.endswith("--- quiet splash")


def test_ubiquity_based_media_is_refused(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "linuxmint22")
    s = u.check(cfg)
    assert not s.ok and "recognise" in s.reason


# --- Debian (preseed in the initrd) and openSUSE (AutoYaST) -----------------------------------------

def test_debian_preseed_is_injected_into_the_initrd(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "debian12-netinst")
    notes = u.prepare(cfg, ANSWERS)
    c = library.reload(cfg)
    assert not (cfg.dir / u.ANSWER_ISO).exists()
    assert [cd.id for cd in c.cdroms] == ["cd0"]
    files = unpack_initrd((cfg.dir / c.direct_boot.initrd).read_bytes())
    assert "init" in files                                   # original initrd still there
    pre = files["preseed.cfg"].decode()
    assert "d-i passwd/username alice" in pre
    assert "d-i partman-auto/disk string /dev/vda" in pre
    assert "d-i debian-installer/exit/poweroff boolean true" in pre
    args = c.direct_boot.append
    assert "debconf/priority=critical" in args.split("---")[0]
    assert args.startswith("vga=788") and args.endswith("--- quiet")
    assert any("internet" in n for n in notes)
    assert_private(cfg)


def test_opensuse_autoyast(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "opensusetumbleweed")
    u.prepare(cfg, ANSWERS)
    answer_iso = cfg.dir / u.ANSWER_ISO
    assert volume_id(answer_iso) == "OEMDRV"
    xml = iso_file(answer_iso, "autoinst.xml").decode()
    assert "<final_halt" in xml and "<final_reboot" not in xml
    assert "<username>alice</username>" in xml
    c = library.reload(cfg)
    assert "autoyast=label://OEMDRV/autoinst.xml" in c.direct_boot.append.split()
    assert c.direct_boot.kernel and (cfg.dir / c.direct_boot.kernel).read_bytes() == b"KERNEL"


def test_other_families_are_unsupported(tmp_path):
    cfg = VMConfig(name="FreeDOS")
    cfg.path = tmp_path
    cfg.os = OsInfo(family="dos", name="FreeDOS 1.3")
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"")
    cfg.cdroms.append(CdRom(id="cd0", file=str(iso), boot_index=1))
    s = u.check(cfg)
    assert not s.ok and "FreeDOS" in s.reason
    cfg.cdroms.clear()
    assert "no installer" in u.check(cfg).reason


# --- housekeeping -----------------------------------------------------------------------------------

def test_prepare_twice_and_cleanup(tmp_path, isos):
    cfg = make_vm(tmp_path, isos, "fedora42-server")
    u.prepare(cfg, ANSWERS)
    u.prepare(cfg, ANSWERS)
    c = library.reload(cfg)
    assert [cd.file for cd in c.cdroms].count(u.ANSWER_ISO) == 1
    assert u.pending(cfg)
    u.cleanup(cfg)
    c = library.reload(cfg)
    assert c.direct_boot is None and all(cd.file != u.ANSWER_ISO for cd in c.cdroms)
    assert not (cfg.dir / u.ANSWER_DIR).exists() and not u.pending(cfg)


def test_password_never_logged(tmp_path, isos, caplog):
    caplog.set_level(logging.DEBUG)
    for kind in ("win11", "fedora42-server", "ubuntu24.04-server", "debian12-netinst"):
        d = tmp_path / kind
        d.mkdir()
        cfg = make_vm(d, isos, kind)
        notes = u.prepare(cfg, ANSWERS)
        assert all(PASSWORD not in n for n in notes)
    assert caplog.records
    for r in caplog.records:
        assert PASSWORD not in r.getMessage()


def test_qemu_command_line_boots_the_installer_kernel(tmp_path, isos):
    from rehome.host.qemu import Machine, QemuCaps
    from rehome.vm.qemu_cmd import LaunchOptions, RuntimePaths, build
    cfg = make_vm(tmp_path, isos, "ubuntu24.04-server")
    (cfg.dir / "disks/disk0.qcow2").write_bytes(b"")
    u.prepare(cfg, ANSWERS)
    cfg = library.reload(cfg)
    caps = QemuCaps(binary="/usr/bin/qemu-system-x86_64", version=(9, 1, 0),
                    devices={"virtio-vga", "virtio-blk-pci", "ide-cd", "virtio-net-pci"},
                    displays={"none"}, audiodevs={"none"}, accels={"tcg"})
    caps.machines = {"pc-q35-9.1": Machine("pc-q35-9.1", alias="q35")}
    rt = RuntimePaths(tmp_path / "run")
    from rehome.host.firmware import Firmware
    fw = Firmware(path="/x.json", description="OVMF", interface_types=["uefi"], device="flash",
                  executable="/usr/share/OVMF/OVMF_CODE.fd", mode="stateless")
    plan = build(cfg, caps, rt, LaunchOptions(accel="tcg", headless=True, firmware=fw))
    argv = plan.argv
    assert argv[argv.index("-kernel") + 1] == str(cfg.dir / "unattended/boot/vmlinuz")
    assert argv[argv.index("-initrd") + 1] == str(cfg.dir / "unattended/boot/initrd")
    assert argv[argv.index("-append") + 1].startswith("autoinstall")
    assert any(str(cfg.dir / u.ANSWER_ISO) in a for a in argv)
