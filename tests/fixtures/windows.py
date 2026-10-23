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
    from rehome.inspect.gfs import new_handle
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
        g = new_handle()
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
    from rehome.inspect.gfs import new_handle
    os.environ.setdefault("LIBGUESTFS_BACKEND", "direct")
    g = new_handle()
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
    from rehome.inspect.gfs import new_handle
    os.environ.setdefault("LIBGUESTFS_BACKEND", "direct")
    g = new_handle()
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


def fake_msdos7_iosys(size: int = 222_390, seed: int = 98) -> bytes:
    """An IO.SYS laid out like MS-DOS 7's that reports how it was started (iosys_stub.asm).

    Needs nasm. The bytes after the stub's code are pseudo-random, and the stub
    checks they arrived in order.
    """
    import random
    import struct
    import subprocess
    from pathlib import Path
    src = Path(__file__).with_name("iosys_stub.asm")
    with tempfile.TemporaryDirectory() as t:
        out = Path(t) / "IO.SYS"
        subprocess.run(["nasm", "-f", "bin", "-o", str(out), str(src)], check=True,
                       capture_output=True)
        stub = bytearray(out.read_bytes())
    assert len(stub) == 0xC00 and stub[0x804:0x808] == b"RHM7"
    data = random.Random(seed).randbytes(size - len(stub))
    a = b = 0
    for byte in data:
        a = (a + byte) & 0xFFFF
        b = (b + a) & 0xFFFF
    stub[0x808:0x810] = struct.pack("<IHH", len(data), a, b)
    return bytes(stub) + data


def build_win9x_bootable(out, iosys: bytes, fat: int = 32, size_mib: int = 300, start: int = 63,
                         part_id: int | None = None, hidden: int | None = 0,
                         fragment: bool = True, mbr_code: bool = True, active: bool = True,
                         kernel32=(4, 10, 2222, 0)):
    """Windows 98 files copied onto a partition made with Linux tools, ``iosys`` as IO.SYS.

    The boot sector is mkfs.fat's placeholder, with ``hidden`` as its hidden
    sectors (mkfs.fat writes 0 when it formats an image file; None = correct).
    ``start`` is the partition's first sector (it can lie past the 8 GiB CHS
    limit; the image is sparse). ``fragment`` scatters IO.SYS over the disk.
    ``mbr_code=False`` leaves the MBR without code (True: SYSLINUX's mbr.bin).
    """
    import shutil
    import struct
    import subprocess
    from pathlib import Path
    from tests.fixtures import freedos
    out = Path(out)
    sector = 512
    part_sectors = size_mib * 2**20 // sector
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        part = t / "part.img"
        subprocess.run([freedos._mkfs(), "-F", str(fat), *(["-s", "1"] if fat == 32 else []),
                        "-h", str(start if hidden is None else hidden), "-C", str(part),
                        str(part_sectors * sector // 1024)], check=True, capture_output=True)
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1", MTOOLS_NO_VFAT="1")

        def mtools(*args):
            subprocess.run(list(args), check=True, capture_output=True, env=env)

        def copy(name, data):
            src = t / "file"
            src.write_bytes(data)
            mtools("mcopy", "-o", "-i", str(part), str(src), f"::{name}")
        if fragment:
            # Files of one cluster each; deleting every other one leaves holes IO.SYS fills.
            boot = part.read_bytes()[:sector]
            cluster = boot[0x0D] * sector
            for i in range(40):
                copy(f"GAP{i:02d}.TMP", bytes([i]) * cluster)
            mtools("mdel", "-i", str(part), *[f"::GAP{i:02d}.TMP" for i in range(0, 40, 2)])
            if fat == 32:       # forget FSInfo's "next free cluster" hint: allocate from the start
                with open(part, "r+b") as f:
                    f.seek(struct.unpack_from("<H", boot, 0x30)[0] * sector + 0x1EC)
                    f.write(b"\xff" * 4)
        copy("IO.SYS", iosys)
        copy("MSDOS.SYS", b"[Paths]\r\nWinDir=C:\\WINDOWS\r\nWinBootDir=C:\\WINDOWS\r\n"
                          b"HostWinBootDrv=C\r\n\r\n[Options]\r\nBootGUI=1\r\n")
        copy("COMMAND.COM", b"\xe9\x00\x00Microsoft(R) Windows 98\x00")
        mtools("mmd", "-i", str(part), "::WINDOWS", "::WINDOWS/SYSTEM", "::WINDOWS/SYSTEM/IOSUBSYS")
        copy("WINDOWS/WIN.COM", b"\xe9WIN")
        copy("WINDOWS/SYSTEM.INI", b"[boot]\r\nshell=Explorer.exe\r\n[386Enh]\r\n[vcache]\r\n")
        copy("WINDOWS/SYSTEM/VMM32.VXD", b"MZ" + b"\x00" * 200)
        copy("WINDOWS/SYSTEM/IOSUBSYS/ESDI_506.PDR", b"LE")
        copy("WINDOWS/SYSTEM/KERNEL32.DLL", regf.minimal_pe("i386") +
             regf.version_resource(*kernel32, original_filename="KERNEL32.DLL"))
        code = (Path(next(p for p in freedos.MBR_CANDIDATES if os.path.exists(p)))
                .read_bytes()[:440] if mbr_code else b"")
        if part_id is None:
            part_id = 0x0C if fat == 32 else 0x0E
        end = start + part_sectors - 1
        entry = (bytes([0x80 if active else 0]) + freedos._chs(min(start, 1023 * 255 * 63))
                 + bytes([part_id]) + freedos._chs(min(end, 1023 * 255 * 63))
                 + struct.pack("<II", start, part_sectors))
        mbr = code + b"\x00" * (446 - len(code)) + entry + b"\x00" * 48 + b"\x55\xaa"
        with open(out, "wb") as f:
            f.write(mbr)
            f.seek(start * sector)
            with open(part, "rb") as p:
                shutil.copyfileobj(p, f, 4 * 2**20)
            f.truncate((end + 1 + 2048) * sector)
    return out


def fat_chain(image, start: int, name: bytes) -> list[int]:
    """Clusters of a root-directory file on the FAT partition at sector ``start``."""
    import struct
    with open(image, "rb") as f:
        f.seek(start * 512)
        bpb = f.read(512)
        bps, spc, rsvd, nfats, rootent, _, _, fatsz16 = struct.unpack_from("<HBHBHHBH", bpb, 11)
        fat32 = fatsz16 == 0
        fatsz = struct.unpack_from("<I", bpb, 0x24)[0] if fat32 else fatsz16
        f.seek((start + rsvd) * bps)
        table = f.read(fatsz * bps)
        data = start + rsvd + nfats * fatsz + (0 if fat32 else rootent * 32 // bps)

        def follow(c):
            out = []
            while 2 <= c < (0x0FFFFFF8 if fat32 else 0xFFF8):
                out.append(c)
                c = (struct.unpack_from("<I", table, c * 4)[0] & 0x0FFFFFFF if fat32
                     else struct.unpack_from("<H", table, c * 2)[0])
            return out
        if fat32:
            root = b"".join(_read_cluster(f, data, spc, bps, c)
                            for c in follow(struct.unpack_from("<I", bpb, 0x2C)[0]))
        else:
            f.seek((data - rootent * 32 // bps) * bps)
            root = f.read(rootent * 32)
        for i in range(0, len(root), 32):
            if root[i:i + 11] == name:
                hi, lo = struct.unpack_from("<H", root, i + 20)[0], struct.unpack_from("<H", root, i + 26)[0]
                return follow(hi << 16 | lo if fat32 else lo)
    raise FileNotFoundError(name)


def _read_cluster(f, data, spc, bps, c):
    f.seek((data + (c - 2) * spc) * bps)
    return f.read(spc * bps)
