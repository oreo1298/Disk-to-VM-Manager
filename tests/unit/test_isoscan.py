"""Recognising installer ISOs from their files (rehome/media/isoscan.py)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from rehome.media import isoscan, osinfo

pytestmark = pytest.mark.skipif(not shutil.which("xorriso"), reason="xorriso is not installed")

DISCS = {
    # name: (volume label, {path: content}, expected short id, expected name fragment)
    "xp": ("WXPFPP_EN", {"I386/TXTSETUP.SIF": "[Version]\nSignature = \"$Windows NT$\"\n"
                                              "[SetupData]\nMajorVersion = 5\nMinorVersion = 1\n",
                         "WIN51": "", "WIN51IP": ""}, "winxp", "Windows XP Professional"),
    "xp-x64": ("GRTMPXFRE", {"AMD64/TXTSETUP.SIF": "[SetupData]\nMajorVersion=5\nMinorVersion=2\n",
                             "WIN51AP": ""}, "winxp", "Windows XP Professional x64"),
    "2003": ("ARMXFRE", {"I386/TXTSETUP.SIF": "[SetupData]\nMajorVersion = 5\nMinorVersion = 2\n",
                         "WIN51IS": ""}, "win2k3", "Server 2003"),
    "2000": ("W2PFPP", {"I386/TXTSETUP.SIF": "[SetupData]\nMajorVersion = 5\nMinorVersion = 0\n",
                        "CDROM_IP.5": ""}, "win2k", "Windows 2000"),
    "nt4": ("NTWKS40", {"I386/TXTSETUP.SIF": "[SetupData]\nMajorVersion = 4\nMinorVersion = 0\n"},
            "winnt4.0", "NT 4.0"),
    "98": ("WIN98 SE", {"WIN98/BASE4.CAB": "x", "SETUP.EXE": "x"}, "win98", "Windows 98"),
    "me": ("WINME", {"WIN9X/BASE4.CAB": "x"}, "winme", "Windows Me"),
    "95": ("WIN95", {"WIN95/WIN95_02.CAB": "x"}, "win95", "Windows 95"),
    "win7": ("GSP1RMCULFRER_EN_DVD", {"sources/idwbinfo.txt": "[BUILDINFO]\nBuildArch=amd64\n"
                                                               "BuildBranch=win7sp1_rtm\n",
                                      "sources/install.wim": "x"}, "win7", "Windows 7"),
    "win11": ("CCCOMA_X64FRE_EN-US_DV9",
              {"README.TXT": "This disc contains a \"UDF\" file system and requires an operating "
                             "system that supports the ISO-13346 \"UDF\" file system "
                             "specification."}, "win11", "Windows 11"),
    "win11-24h2": ("CCCOMA_X64FRE_EN-US_DV9",
                   {"sources/idwbinfo.txt": "BuildArch=amd64\nBuildBranch=ge_release\n"},
                   "win11", "Windows 11"),
    "omarchy": ("OMARCHY_202510", {"arch/version": "2025.10.01", "arch/boot/x86_64/vmlinuz-linux": "k",
                                   "boot/syslinux/syslinux.cfg": ""},
                "archlinux", "Omarchy (based on Arch Linux)"),
    "arch": ("ARCH_202510", {"arch/version": "2025.10.01", "arch/boot/x86_64/vmlinuz-linux": "k"},
             "archlinux", "Arch Linux"),
    "ubuntu": ("Ubuntu 24.04.3 LTS amd64", {".disk/info": "Ubuntu 24.04.3 LTS \"Noble Numbat\" - "
                                                          "Release amd64 (20250805)\n",
                                            "casper/vmlinuz": "k"}, "ubuntu24.04", "Ubuntu 24.04.3"),
    "mint": ("Linux Mint 22.1 Cinnamon 64-bit", {".disk/info": "Linux Mint 22.1 \"Xia\" - Cinnamon "
                                                               "64-bit 20250108\n",
                                                 "casper/vmlinuz": "k"}, "", "Linux Mint 22.1"),
    "debian": ("Debian 12.5.0 amd64 n", {".disk/info": "Debian GNU/Linux 12.5.0 \"Bookworm\" - "
                                                       "Official amd64 NETINST\n",
                                         "install.amd/vmlinuz": "k"}, "debian12", "Debian"),
    "fedora": ("Fedora-S-dvd-x86_64-41", {".treeinfo": "[general]\nfamily = Fedora\nversion = 41\n",
                                          "images/pxeboot/vmlinuz": "k"}, "fedora41", "Fedora 41"),
    "freebsd": ("14_1_RELEASE_AMD64_CD", {"bin/freebsd-version": "USERLAND_VERSION=\"14.1-RELEASE\"\n",
                                          "boot/kernel/kernel": "k"}, "freebsd14.1", "FreeBSD 14.1"),
    "openbsd": ("OpenBSD/amd64   7.5 Install CD", {"7.5/amd64/bsd.rd": "k"}, "openbsd7.5",
                "OpenBSD 7.5"),
    "reactos": ("ReactOS", {"reactos/reactos.cab": "x"}, "reactos0.4", "ReactOS"),
    "linux-generic": ("SPARKYOS_8", {"isolinux/vmlinuz": "k", "isolinux/isolinux.cfg": ""}, "",
                      "Sparkyos"),
}


def make_iso(tmp: Path, label: str, files: dict, plain: bool = False) -> Path:
    root = tmp / "root"
    shutil.rmtree(root, ignore_errors=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    out = tmp / "disc.iso"
    out.unlink(missing_ok=True)
    names = ["-iso-level", "2"] if plain else ["-J", "-R"]
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-V", label[:32], *names,
                    "-o", str(out), str(root)], check=True, capture_output=True)
    return out


@pytest.mark.parametrize("disc", sorted(DISCS))
def test_recognises_installer_layouts(tmp_path, disc):
    label, files, sid, name = DISCS[disc]
    g = isoscan.detect(str(make_iso(tmp_path, label, files)))
    assert g is not None, disc
    assert g.short_id == sid
    assert name in g.name
    assert g.evidence


@pytest.mark.parametrize("disc", ["xp", "98", "omarchy", "freebsd"])
def test_plain_iso9660_names_without_joliet_or_rock_ridge(tmp_path, disc):
    label, files, sid, _name = DISCS[disc]
    g = isoscan.detect(str(make_iso(tmp_path, label, files, plain=True)))
    assert g is not None and g.short_id == sid


def test_reader_lists_and_reads(tmp_path):
    iso = make_iso(tmp_path, "TESTDISC", {"Dir/Sub/File.txt": "hello", "top.txt": "x" * 5000})
    with isoscan.Iso9660(str(iso)) as r:
        assert r.volume_id == "TESTDISC"
        assert r.isdir("dir/sub") and r.exists("DIR/SUB/FILE.TXT")
        assert r.read("dir/sub/file.txt") == b"hello"
        assert len(r.read("top.txt")) == 5000
        assert r.read("missing") is None and not r.exists("dir/nothing")


def test_not_an_iso(tmp_path):
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\0" * 70000)
    assert isoscan.detect(str(junk)) is None
    assert isoscan.detect(str(tmp_path / "missing.iso")) is None
    unknown = make_iso(tmp_path, "DATA", {"photos/cat.jpg": "x"})
    assert isoscan.detect(str(unknown)) is None


def test_identify_media_without_libosinfo(tmp_path, monkeypatch):
    label, files, sid, name = DISCS["omarchy"]
    iso = make_iso(tmp_path, label, files)
    monkeypatch.setattr(osinfo, "available", lambda: False)
    mi = osinfo.identify_media(str(iso))
    assert mi.os is not None and mi.os.short_id == sid and mi.label == name
    assert mi.volume_id == label and "archiso" in mi.evidence
    facts = mi.os.facts()
    assert facts["family"] == "linux"
    xp = osinfo.identify_media(str(make_iso(tmp_path, *DISCS["xp"][:2])))
    assert xp.os.facts()["family"] == "windows" and xp.os.facts()["version"] == "5.1"


def test_new_vm_plan_from_unknown_distro_gets_linux_hardware(tmp_path, monkeypatch):
    from rehome.vm import create
    label, files, _sid, _name = DISCS["omarchy"]
    iso = make_iso(tmp_path, label, files)
    monkeypatch.setattr(osinfo, "available", lambda: False)
    p = create.plan(create.NewVMSpec(iso=str(iso)))
    assert p.config.os.family == "linux" and "Omarchy" in p.os_label
    assert p.config.display.device == "virtio-vga"
