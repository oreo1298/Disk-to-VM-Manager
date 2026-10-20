"""Disk layout: partition scheme, ESP, boot code, encryption, BSD and macOS volumes."""

from __future__ import annotations

from rehome.inspect.gfs import ci_path, mounted
from rehome.inspect.model import DetectedOS, DiskLayout, Volume

ESP_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
BIOS_BOOT_GUID = "21686148-6449-6E6F-744E-656564454649"
MSR_GUID = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
BSD_GPT = {
    "516E7CB6-6ECF-11D6-8FF8-00022D09712B": ("freebsd", "ufs"),
    "516E7CBA-6ECF-11D6-8FF8-00022D09712B": ("freebsd", "zfs"),
    "83BD6B9D-7F41-11DC-BE0B-001560B84F0F": ("freebsd", "boot"),
    "49F48D5A-B10E-11DC-B99B-0019D1879648": ("netbsd", "ffs"),
    "824CC7A0-36A8-11E3-890A-952519AD3F61": ("openbsd", "data"),
}
BSD_MBR = {0xA5: "freebsd", 0xA6: "openbsd", 0xA9: "netbsd"}
MACOS_GPT = {
    "7C3457EF-0000-11AA-AA11-00306543ECAC": "APFS",
    "48465300-0000-11AA-AA11-00306543ECAC": "HFS+",
    "426F6F74-0000-11AA-AA11-00306543ECAC": "Apple Boot",
}

MBR_LOADERS = [
    (b"VeraCrypt", "veracrypt"), (b"TrueCrypt", "truecrypt"), (b"GRUB", "grub"),
    (b"LILO", "lilo"), (b"Missing OS", "syslinux"),
    # MS-DOS 5+ and Windows NT-family MBRs share these messages.
    (b"Invalid partition table", "microsoft"), (b"Error loading operating system", "microsoft"),
    (b"Missing operating system", "microsoft"),
    (b"FreeBSD", "freebsd"), (b"Default: F", "freebsd"),
]


def mbr_loader(code: bytes) -> str:
    if not code.strip(b"\x00"):
        return "none"
    for needle, name in MBR_LOADERS:
        if needle in code:
            return name
    return "unknown"


def analyze_disk(g, index: int, device: str, filesystems: dict[str, str]) -> DiskLayout:
    lay = DiskLayout(index=index, device=device)
    try:
        lay.size = g.blockdev_getsize64(device)
    except RuntimeError:
        pass
    try:
        lay.scheme = g.part_get_parttype(device)
    except RuntimeError:
        lay.scheme = "none"
    try:
        head = g.pread_device(device, 64 * 512, 0)
    except RuntimeError:
        head = b""
    lay.mbr_loader = mbr_loader(head[:440] + head[512:64 * 512])
    if lay.scheme in ("gpt", "msdos"):
        for p in g.part_list(device):
            num = p["part_num"]
            dev = _part_dev(g, device, num)
            v = Volume(device=dev, size=p.get("part_size", 0), fstype=filesystems.get(dev, ""))
            if lay.scheme == "gpt":
                try:
                    v.part_type = g.part_get_gpt_type(device, num).upper()
                except RuntimeError:
                    pass
                if v.part_type == ESP_GUID:
                    lay.has_esp = True
                if v.part_type == BIOS_BOOT_GUID:
                    lay.has_bios_boot = True
            else:
                try:
                    mbr_id = g.part_get_mbr_id(device, num)
                    v.part_type = f"0x{mbr_id:02x}"
                    if mbr_id == 0xEF:
                        lay.has_esp = True
                    v.bootable = bool(g.part_get_bootable(device, num))
                except RuntimeError:
                    pass
            try:
                v.label = g.vfs_label(dev) if v.fstype and v.fstype != "unknown" else ""
            except RuntimeError:
                pass
            v.encryption = _encryption(g, dev, v.fstype)
            lay.volumes.append(v)
    elif filesystems.get(device):
        lay.volumes.append(Volume(device=device, fstype=filesystems[device], size=lay.size,
                                  encryption=_encryption(g, device, filesystems[device])))
    return lay


def _part_dev(g, device: str, num: int) -> str:
    for p in g.list_partitions():
        try:
            if g.part_to_dev(p) == device and g.part_to_partnum(p) == num:
                return p
        except RuntimeError:
            continue
    return f"{device}{num}"


def _encryption(g, dev: str, fstype: str) -> str:
    if fstype == "crypto_LUKS":
        return "luks"
    if fstype.lower() == "bitlocker":
        return "bitlocker"
    if fstype in ("", "unknown"):
        try:
            boot = g.pread_device(dev, 512, 0)
        except RuntimeError:
            return ""
        if boot[3:11] == b"-FVE-FS-":
            return "bitlocker"
    return ""


def veracrypt_system(g, lay: DiskLayout) -> bool:
    if lay.mbr_loader in ("veracrypt", "truecrypt"):
        return True
    for v in lay.volumes:
        if v.part_type == ESP_GUID or v.part_type == "0xef":
            try:
                with mounted(g, v.device):
                    if ci_path(g, "/EFI/VeraCrypt/DcsBoot.efi"):
                        return True
            except Exception:
                continue
    return False


def special_oses(lay: DiskLayout) -> list[DetectedOS]:
    """Systems recognised from partition types alone (BSD on ZFS, macOS)."""
    out: list[DetectedOS] = []
    seen = set()
    for v in lay.volumes:
        if v.part_type in MACOS_GPT and "macos" not in seen:
            seen.add("macos")
            o = DetectedOS(root=v.device, family="macos", distro="macos", name="macOS",
                           disk_index=lay.index, boot_mode="uefi")
            o.warnings.append("macOS was found. Apple's license only allows running macOS on "
                              "Apple hardware, so Rehome does not create a VM for it.")
            out.append(o)
        bsd = BSD_GPT.get(v.part_type)
        if bsd is None and v.part_type.startswith("0x"):
            name = BSD_MBR.get(int(v.part_type, 16))
            bsd = (name, "slice") if name else None
        if bsd and bsd[1] in ("zfs", "ufs", "ffs", "data", "slice") and bsd[0] not in seen:
            seen.add(bsd[0])
            o = DetectedOS(root=v.device, family="bsd", distro=bsd[0],
                           name={"freebsd": "FreeBSD", "openbsd": "OpenBSD", "netbsd": "NetBSD"}[bsd[0]],
                           disk_index=lay.index, arch="x86_64",
                           boot_mode="uefi" if lay.has_esp else "bios")
            if bsd[1] == "zfs":
                o.notes.append("The root filesystem is ZFS, which Rehome can identify but not "
                               "open; the VM is set up without modifying it.")
            o.details["partition_kind"] = bsd[1]
            out.append(o)
    return out


def boot_mode_for(lay: DiskLayout) -> str:
    if lay.scheme == "gpt":
        if lay.has_esp:
            return "uefi"
        if lay.has_bios_boot:
            return "bios"
        return "uefi"
    if lay.has_esp and lay.mbr_loader in ("none", "unknown"):
        return "uefi"
    return "bios"
