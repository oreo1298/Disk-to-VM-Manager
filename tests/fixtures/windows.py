"""Synthetic Windows (and Win9x/DOS) disk images for identification and patch tests.

These are not bootable: they carry the files and registry hives that
identification and offline patching look at (system root layout, SYSTEM and
SOFTWARE hives, cmd.exe, HAL, hiberfil.sys, boot files), laid out on MBR or
GPT disks exactly as Windows Setup would.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from tests.fixtures import regf

ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
MSR = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
BASIC_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
WIN_MBR_CODE = (b"\x33\xc0\x8e\xd0" + b"\x90" * 300 + b"Invalid partition table\x00Error loading "
                b"operating system\x00Missing operating system\x00")


@dataclass
class WinSpec:
    product: str = "Windows 10 Pro"
    version: str = "10.0"             # NT version
    build: int = 19045
    edition_id: str = "Professional"
    installation_type: str = "Client"
    display_version: str = "22H2"
    arch: str = "x86_64"
    scheme: str = "gpt"               # gpt (UEFI) / mbr (BIOS)
    boot_driver: str = "storahci"     # service that is Start=0 on the original machine
    controller_pci: str = "VEN_8086&DEV_A352&SUBSYS_00000000&REV_10"
    hibernated: bool = False
    fast_startup: bool = False
    hostile: list[str] = field(default_factory=list)
    hal: str = ""                     # XP: original HAL file name, e.g. halmacpi.dll
    rtc_utc: bool = False
    size_mib: int = 700
    hostname: str = "OLDPC"
    class_filters: dict = field(default_factory=dict)   # class GUID -> UpperFilters list


def _system_hive(spec: WinSpec) -> dict:
    cs = "ControlSet001"
    services = {
        "atapi": 3, "intelide": 3, "pciide": 3, "storahci": 3, "msahci": 3, "stornvme": 3,
        "iaStorAC": 4, "iaStorV": 4, "intelppm": 1, "viostor": None,
    }
    xp = spec.version.startswith("5.")
    if xp:
        services = {"atapi": 0, "intelide": 3, "pciide": 3, "iaStor": 4, "intelppm": 1}
    tree = {
        "Select": {"Current": 1, "Default": 1, "LastKnownGood": 2, "Failed": 0},
        f"{cs}\\Control\\ComputerName\\ComputerName": {"ComputerName": spec.hostname},
        f"{cs}\\Services\\Tcpip\\Parameters": {"Hostname": spec.hostname},
        f"{cs}\\Control\\Session Manager\\Power": {"HiberbootEnabled": 1 if spec.fast_startup else 0,
                                                   "HibernateEnabled": 1},
        f"{cs}\\Control\\TimeZoneInformation": {"RealTimeIsUniversal": 1} if spec.rtc_utc else
        {"TimeZoneKeyName": "W. Europe Standard Time"},
        "MountedDevices": {},
    }
    for name, start in services.items():
        if start is None:
            continue
        if name == spec.boot_driver:
            start = 0
        tree[f"{cs}\\Services\\{name}"] = {"Start": start, "Type": 1, "ErrorControl": 3,
                                           "ImagePath": f"system32\\drivers\\{name}.sys"}
    if spec.boot_driver not in services:
        tree[f"{cs}\\Services\\{spec.boot_driver}"] = {"Start": 0, "Type": 1, "ErrorControl": 3}
    if not xp and spec.boot_driver != "storahci":
        # What Windows 8+ does to unused in-box storage drivers.
        tree[f"{cs}\\Services\\storahci\\StartOverride"] = {"0": 3}
    tree[f"{cs}\\Enum\\PCI\\{spec.controller_pci}\\3&11583659&0&B8"] = {"Service": spec.boot_driver}
    for svc in spec.hostile:
        tree[f"{cs}\\Services\\{svc}"] = {"Start": 2, "Type": 16}
    for guid, filters in spec.class_filters.items():
        tree[f"{cs}\\Control\\Class\\{guid}"] = {"UpperFilters": filters}
    if xp:
        tree[f"{cs}\\Control\\CriticalDeviceDatabase\\pci#ven_8086&dev_24db"] = {
            "Service": "intelide", "ClassGUID": "{4D36E96A-E325-11CE-BFC1-08002BE10318}"}
    return tree


def _software_hive(spec: WinSpec) -> dict:
    cv = {"ProductName": spec.product, "CurrentBuildNumber": str(spec.build),
          "CurrentBuild": str(spec.build), "EditionID": spec.edition_id,
          "InstallationType": spec.installation_type, "SystemRoot": "C:\\Windows"}
    major, minor = spec.version.split(".")[:2]
    if int(major) >= 10:
        cv.update({"CurrentMajorVersionNumber": int(major), "CurrentMinorVersionNumber": int(minor),
                   "CurrentVersion": "6.3", "DisplayVersion": spec.display_version, "UBR": 1})
    else:
        cv["CurrentVersion"] = spec.version
    return {"Microsoft\\Windows NT\\CurrentVersion": cv,
            "Microsoft\\Windows\\CurrentVersion": {"ProgramFilesDir": "C:\\Program Files"}}


def build(out: str, spec: WinSpec, fmt: str = "raw") -> str:
    import guestfs
    os.environ.setdefault("LIBGUESTFS_BACKEND", "direct")
    xp = spec.version.startswith("5.")
    sysroot = "/WINDOWS" if xp else "/Windows"
    sys32 = f"{sysroot}/system32" if xp else f"{sysroot}/System32"
    with tempfile.TemporaryDirectory() as td:
        system, software = os.path.join(td, "SYSTEM"), os.path.join(td, "SOFTWARE")
        regf.build_hive(system, _system_hive(spec), "SYSTEM")
        regf.build_hive(software, _software_hive(spec), "SOFTWARE")
        cmd = os.path.join(td, "cmd.exe")
        with open(cmd, "wb") as f:
            f.write(regf.minimal_pe(spec.arch))
        g = guestfs.GuestFS(python_return_dict=True)
        g.disk_create(out, fmt, spec.size_mib * 2**20)
        g.add_drive_opts(out, format=fmt, readonly=False)
        g.launch()
        try:
            dev = "/dev/sda"
            if spec.scheme == "gpt":
                g.part_init(dev, "gpt")
                g.part_add(dev, "p", 2048, 2048 + 100 * 2048 - 1)            # ESP 100 MiB
                g.part_add(dev, "p", 2048 + 100 * 2048, 2048 + 116 * 2048 - 1)  # MSR 16 MiB
                g.part_add(dev, "p", 2048 + 116 * 2048, -2048)
                g.part_set_gpt_type(dev, 1, ESP)
                g.part_set_gpt_type(dev, 2, MSR)
                g.part_set_gpt_type(dev, 3, BASIC_DATA)
                g.mkfs("vfat", "/dev/sda1")
                g.mkfs("ntfs", "/dev/sda3")
                sysdev = "/dev/sda3"
                g.mount("/dev/sda1", "/")
                g.mkdir_p("/EFI/Microsoft/Boot")
                g.mkdir_p("/EFI/Boot")
                g.write("/EFI/Microsoft/Boot/bootmgfw.efi", regf.minimal_pe("x86_64"))
                g.write("/EFI/Boot/bootx64.efi", regf.minimal_pe("x86_64"))
                g.write("/EFI/Microsoft/Boot/BCD", b"regf")
                g.umount("/")
            else:
                g.part_init(dev, "mbr")
                g.part_add(dev, "p", 2048, -2048)
                g.part_set_mbr_id(dev, 1, 0x07)
                g.part_set_bootable(dev, 1, True)
                g.mkfs("ntfs", "/dev/sda1")
                sysdev = "/dev/sda1"
                g.pwrite_device(dev, WIN_MBR_CODE[:440], 0)
            g.mount(sysdev, "/")
            g.mkdir_p(f"{sys32}/config")
            g.mkdir_p(f"{sys32}/drivers")
            g.upload(system, f"{sys32}/config/{'system' if xp else 'SYSTEM'}")
            g.upload(software, f"{sys32}/config/{'software' if xp else 'SOFTWARE'}")
            g.upload(cmd, f"{sys32}/cmd.exe")
            for drv in ("storahci", "stornvme", "atapi", "intelide", "pciide", spec.boot_driver):
                g.write(f"{sys32}/drivers/{drv}.sys", regf.minimal_pe(spec.arch))
            if spec.hal:
                g.write(f"{sys32}/hal.dll", regf.minimal_pe("i386") +
                        regf.version_resource(5, 1, 2600, 5512, original_filename=spec.hal))
            g.write("/pagefile.sys", b"\x00" * 4096)
            if spec.hibernated or spec.fast_startup:
                g.write("/hiberfil.sys", b"HIBR" + b"\x00" * 4092)
            else:
                g.write("/hiberfil.sys", b"\x00" * 4096)
            if xp:
                g.write("/ntldr", b"NTLDR stub")
                g.write("/boot.ini", b"[boot loader]\r\ntimeout=30\r\ndefault=multi(0)disk(0)"
                        b"rdisk(0)partition(1)\\WINDOWS\r\n[operating systems]\r\nmulti(0)disk(0)"
                        b"rdisk(0)partition(1)\\WINDOWS=\"Microsoft Windows XP Professional\" "
                        b"/noexecute=optin /fastdetect\r\n")
            elif spec.scheme == "mbr":
                g.write("/bootmgr", b"bootmgr stub")
                g.mkdir_p("/Boot")
                g.write("/Boot/BCD", b"regf")
            g.mkdir_p("/Users/Default" if not xp else "/Documents and Settings/All Users")
            g.umount_all()
        finally:
            g.shutdown()
            g.close()
    return out


def build_win9x(out: str, kernel32=(4, 10, 2222, 0), windir: str = "WINDOWS") -> str:
    """FAT32 disk with Windows 98 SE (or 95/Me, per kernel32 version)."""
    import guestfs
    os.environ.setdefault("LIBGUESTFS_BACKEND", "direct")
    g = guestfs.GuestFS(python_return_dict=True)
    g.disk_create(out, "raw", 300 * 2**20)
    g.add_drive_opts(out, format="raw", readonly=False)
    g.launch()
    try:
        g.part_init("/dev/sda", "mbr")
        g.part_add("/dev/sda", "p", 63, -1)
        g.part_set_mbr_id("/dev/sda", 1, 0x0E)
        g.part_set_bootable("/dev/sda", 1, True)
        g.mkfs("vfat", "/dev/sda1")
        g.pwrite_device("/dev/sda", WIN_MBR_CODE[:440], 0)
        g.mount("/dev/sda1", "/")
        g.write("/IO.SYS", b"\xe9" + b"\x00" * 1000)
        g.write("/MSDOS.SYS", f"[Paths]\r\nWinDir=C:\\{windir}\r\nWinBootDir=C:\\{windir}\r\n"
                              f"HostWinBootDrv=C\r\n\r\n[Options]\r\nBootGUI=1\r\n".encode())
        g.write("/COMMAND.COM", b"\xe9\x00\x00Microsoft(R) Windows 98\x00")
        g.mkdir_p(f"/{windir}/SYSTEM/IOSUBSYS")
        g.write(f"/{windir}/WIN.COM", b"\xe9WIN")
        g.write(f"/{windir}/SYSTEM.INI", b"[boot]\r\nshell=Explorer.exe\r\n[386Enh]\r\n[vcache]\r\n")
        g.write(f"/{windir}/SYSTEM/VMM32.VXD", b"MZ" + b"\x00" * 200)
        g.write(f"/{windir}/SYSTEM/IOSUBSYS/ESDI_506.PDR", b"LE")
        g.write(f"/{windir}/SYSTEM/KERNEL32.DLL", regf.minimal_pe("i386") +
                regf.version_resource(*kernel32, original_filename="KERNEL32.DLL"))
        g.umount_all()
    finally:
        g.shutdown()
        g.close()
    return out


def build_msdos(out: str, version: str = "6.22") -> str:
    import guestfs
    os.environ.setdefault("LIBGUESTFS_BACKEND", "direct")
    g = guestfs.GuestFS(python_return_dict=True)
    g.disk_create(out, "raw", 64 * 2**20)
    g.add_drive_opts(out, format="raw", readonly=False)
    g.launch()
    try:
        g.part_init("/dev/sda", "mbr")
        g.part_add("/dev/sda", "p", 63, -1)
        g.part_set_mbr_id("/dev/sda", 1, 0x06)
        g.part_set_bootable("/dev/sda", 1, True)
        g.mkfs("vfat", "/dev/sda1")
        g.mount("/dev/sda1", "/")
        g.write("/IO.SYS", b"\x00" * 512)
        g.write("/MSDOS.SYS", b"\x00" * 512)
        g.write("/COMMAND.COM", b"\xe9" + f"MS-DOS Version {version}".encode() + b"\x00" * 100)
        g.write("/CONFIG.SYS", b"DEVICE=C:\\DOS\\HIMEM.SYS\r\n")
        g.write("/AUTOEXEC.BAT", b"@ECHO OFF\r\nPATH C:\\DOS\r\n")
        g.mkdir("/DOS")
        g.umount_all()
    finally:
        g.shutdown()
        g.close()
    return out
