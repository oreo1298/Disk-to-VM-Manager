"""Tiny stand-ins for real installer ISOs.

Each image has the volume ID, system/publisher ID and file layout of the real
installer (so libosinfo identifies it and Rehome finds the kernel, initrd and
boot menu), plus an El Torito boot record (libosinfo refuses non-bootable
media). The kernel and initrd are dummies; the initrd is a real gzip cpio so
initrd injection can be checked.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

from rehome.media.unattended import cpio_newc

FEDORA_GRUB = """set default="1"
set timeout=60
search --no-floppy --set=root -l '{vol}'
menuentry 'Install Fedora {ver}' --class fedora --class gnu-linux --class gnu --class os {{
\tlinux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL={vol} quiet
\tinitrd /images/pxeboot/initrd.img
}}
menuentry 'Test this media & install Fedora {ver}' --class fedora --class gnu-linux {{
\tlinux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL={vol} rd.live.check quiet
\tinitrd /images/pxeboot/initrd.img
}}
submenu 'Troubleshooting -->' {{
\tmenuentry 'Install Fedora {ver} in basic graphics mode' --class fedora {{
\t\tlinux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL={vol} nomodeset quiet
\t\tinitrd /images/pxeboot/initrd.img
\t}}
\tmenuentry 'Rescue a Fedora system' --class fedora {{
\t\tlinux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL={vol} inst.rescue quiet
\t\tinitrd /images/pxeboot/initrd.img
\t}}
}}
"""

UBUNTU_SERVER_GRUB = """set timeout=30
loadfont unicode
menuentry "Try or Install Ubuntu Server" {
\tset gfxpayload=keep
\tlinux\t/casper/vmlinuz  ---
\tinitrd\t/casper/initrd
}
menuentry "Ubuntu Server with the HWE kernel" {
\tset gfxpayload=keep
\tlinux\t/casper/hwe-vmlinuz  ---
\tinitrd\t/casper/hwe-initrd
}
grub_platform
if [ "$grub_platform" = "efi" ]; then
menuentry 'Boot from next volume' {
\texit 1
}
menuentry 'UEFI Firmware Settings' {
\tfwsetup
}
fi
"""

UBUNTU_DESKTOP_GRUB = """set timeout=30
menuentry "Try or Install Ubuntu" {
\tset gfxpayload=keep
\tlinux\t/casper/vmlinuz  --- quiet splash
\tinitrd\t/casper/initrd
}
menuentry "Ubuntu (safe graphics)" {
\tlinux\t/casper/vmlinuz  nomodeset --- quiet splash
\tinitrd\t/casper/initrd
}
"""

DEBIAN_GRUB = """if loadfont $prefix/font.pf2 ; then
  set gfxmode=800x600
fi
menuentry --hotkey=g 'Graphical install' {
    set background_color=black
    linux    /install.amd/vmlinuz vga=788 --- quiet
    initrd   /install.amd/gtk/initrd.gz
}
menuentry --hotkey=i 'Install' {
    set background_color=black
    linux    /install.amd/vmlinuz vga=788 --- quiet
    initrd   /install.amd/initrd.gz
}
submenu --hotkey=a 'Advanced options ...' {
    menuentry '... Expert install' {
        linux    /install.amd/vmlinuz priority=low vga=788 ---
        initrd   /install.amd/initrd.gz
    }
}
"""

OPENSUSE_GRUB = """set timeout=60
menuentry 'Installation' --class opensuse --class gnu-linux --class gnu --class os {
  set gfxpayload=keep
  echo 'Loading kernel ...'
  linux /boot/x86_64/loader/linux splash=silent
  echo 'Loading initial ramdisk ...'
  initrd /boot/x86_64/loader/initrd
}
menuentry 'Upgrade' --class opensuse --class gnu-linux --class gnu --class os {
  linux /boot/x86_64/loader/linux splash=silent upgrade=1
  initrd /boot/x86_64/loader/initrd
}
"""

INITRD_PAYLOAD = {"init": b"#!/bin/sh\necho fake init\n", "etc/fake-installer": b"1\n"}


def fake_initrd() -> bytes:
    return gzip.compress(cpio_newc(INITRD_PAYLOAD, mode=0o100755), mtime=0)


SPECS = {
    "win11": dict(volume="CCCOMA_X64FRE_EN-US_DV9", publisher="MICROSOFT CORPORATION",
                  files={"setup.exe": b"MZ", "sources/boot.wim": b"WIM", "bootmgr": b"x",
                         "efi/microsoft/boot/efisys.bin": b"\0" * 2048}),
    "win2k25": dict(volume="SSS_X64FREE_EN-US_DV9", publisher="MICROSOFT CORPORATION",
                    files={"setup.exe": b"MZ", "sources/boot.wim": b"WIM"}),
    "fedora42-server": dict(
        volume="Fedora-S-dvd-x86_64-42", sysid="LINUX",
        files={"images/pxeboot/vmlinuz": b"KERNEL", "images/pxeboot/initrd.img": "INITRD",
               "EFI/BOOT/grub.cfg": FEDORA_GRUB.format(vol="Fedora-S-dvd-x86_64-42", ver=42),
               "boot/grub2/grub.cfg": FEDORA_GRUB.format(vol="Fedora-S-dvd-x86_64-42", ver=42),
               ".discinfo": b"42\nFedora\nx86_64\n"}),
    "fedora42-workstation": dict(
        volume="Fedora-WS-Live-42-1-1", sysid="LINUX",
        files={"images/pxeboot/vmlinuz": b"KERNEL", "images/pxeboot/initrd.img": "INITRD",
               "LiveOS/squashfs.img": b"SQ"}),
    "ubuntu24.04-server": dict(
        volume="Ubuntu-Server 24.04.3 LTS amd64",
        files={"casper/vmlinuz": b"KERNEL", "casper/initrd": "INITRD",
               "casper/hwe-vmlinuz": b"KERNEL", "casper/hwe-initrd": "INITRD",
               "casper/ubuntu-server-minimal.squashfs": b"SQ",
               "boot/grub/grub.cfg": UBUNTU_SERVER_GRUB,
               ".disk/info": b"Ubuntu-Server 24.04.3 LTS \"Noble Numbat\" - Release amd64"}),
    "ubuntu24.04-desktop": dict(
        volume="Ubuntu 24.04.3 LTS amd64",
        files={"casper/vmlinuz": b"KERNEL", "casper/initrd": "INITRD",
               "casper/minimal.squashfs": b"SQ", "casper/minimal.standard.squashfs": b"SQ",
               "boot/grub/grub.cfg": UBUNTU_DESKTOP_GRUB}),
    "linuxmint22": dict(
        volume="Linux Mint 22.2 Cinnamon 64-bit",
        files={"casper/vmlinuz": b"KERNEL", "casper/initrd.lz": "INITRD",
               "casper/filesystem.squashfs": b"SQ", "boot/grub/grub.cfg": UBUNTU_DESKTOP_GRUB}),
    "debian12-netinst": dict(
        volume="Debian 12.11.0 amd64 n",
        files={"install.amd/vmlinuz": b"KERNEL", "install.amd/initrd.gz": "INITRD",
               "install.amd/gtk/initrd.gz": "INITRD", "boot/grub/grub.cfg": DEBIAN_GRUB,
               ".disk/info": b"Debian GNU/Linux 12.11.0"}),
    "opensusetumbleweed": dict(
        volume="openSUSE-Tumbleweed-DVD-x86_64", sysid="LINUX", publisher="SUSE LLC",
        files={"boot/x86_64/loader/linux": b"KERNEL", "boot/x86_64/loader/initrd": "INITRD",
               "EFI/BOOT/grub.cfg": OPENSUSE_GRUB}),
}


def available() -> str | None:
    return None if shutil.which("xorriso") else "xorriso is not installed"


def build(kind: str, out: str | Path) -> Path:
    spec = SPECS[kind]
    out = Path(out)
    root = out.with_name(out.name + ".tree")
    if root.exists():
        shutil.rmtree(root)
    for rel, data in spec["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if data == "INITRD":
            data = fake_initrd()
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    boot = root / "isolinux" / "eltorito.img"
    boot.parent.mkdir(parents=True, exist_ok=True)
    boot.write_bytes(b"\0" * 2048)
    argv = ["xorriso", "-as", "mkisofs", "-quiet", "-o", str(out), "-V", spec["volume"],
            "-J", "-joliet-long", "-r", "-b", "isolinux/eltorito.img", "-no-emul-boot",
            "-boot-load-size", "4"]
    if spec.get("sysid"):
        argv += ["-sysid", spec["sysid"]]
    if spec.get("publisher"):
        argv += ["-publisher", spec["publisher"]]
    subprocess.run(argv + [str(root)], check=True, capture_output=True)
    shutil.rmtree(root)
    return out
