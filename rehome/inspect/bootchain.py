"""The BIOS boot chain of DOS-era systems: MBR code, active partition, boot sector.

A BIOS loads sector 0 of the disk (the MBR), whose code starts the boot
sector of the *active* partition, whose code loads the system (IO.SYS for
MS-DOS and Windows 9x, KERNEL.SYS for FreeDOS). Disks prepared on Linux often
break this chain: a partition formatted with mkfs.fat has placeholder code
("This is not a bootable disk...") and, on an image file, 0 hidden sectors;
partitioning tools may leave no partition marked active, no MBR code at all,
or a partition type DOS does not know.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass

SECTOR = 512
# Partition types DOS reads as FAT. The LBA ones (0x0C, 0x0E) need MS-DOS 7 (Windows 95 OSR2+).
FAT_IDS = {0x01: "FAT12", 0x04: "FAT16", 0x06: "FAT16", 0x0B: "FAT32", 0x0C: "FAT32",
           0x0E: "FAT16"}
CHS_TYPES = (0x01, 0x04, 0x06, 0x0B)
# Sectors a CHS address can reach (1024 cylinders x 255 heads x 63 sectors, about 7.8 GiB).
CHS_LIMIT = 1024 * 255 * 63

# Boot sector (VBR) code, by distinctive strings. Checked in order.
VBR_LOADERS = [
    (b"Rehome: no IO.SYS", "rehome"),                     # rehome/patch/win9x_boot.asm
    (b"This is not a bootable disk", "mkfs.fat"),        # dosfstools placeholder
    (b"WINBOOT SYS", "msdos"), (b"IO      SYS", "msdos"),  # MS-DOS 5-7 and Windows 9x
    (b"IBMBIO  COM", "pcdos"), (b"KERNEL  SYS", "freedos"),
    (b"Remove disks or other media", "msdos"), (b"Invalid system disk", "msdos"),
    (b"NTLDR", "ntldr"), (b"BOOTMGR", "bootmgr"),
    (b"SYSLINUX", "syslinux"), (b"GRUB", "grub"),
]
MBR_LOADERS = [
    (b"Rehome MBR", "rehome"),                            # rehome/patch/mbr.asm
    (b"Invalid partition table", "microsoft"), (b"Error loading operating system", "microsoft"),
    (b"Missing operating system", "microsoft"), (b"Missing OS", "syslinux"),
    (b"GRUB", "grub"), (b"LILO", "lilo"), (b"This is not a bootable disk", "mkfs.fat"),
]
# Boot sectors that cannot start anything (Rehome may replace them in the overlay).
UNBOOTABLE = ("none", "mkfs.fat")


def fat_type(vbr: bytes) -> str:
    """FAT12/FAT16/FAT32 from a boot sector's BPB, or '' if it has none.

    FAT32 means the FAT32 BPB layout (16-bit FAT size 0), which is what boot
    code goes by; FAT12 and FAT16 differ by cluster count, as Microsoft's
    specification defines them.
    """
    if len(vbr) < SECTOR:
        return ""
    bps, spc, rsvd, nfats, rootent, tot16, _, fatsz16 = struct.unpack_from("<HBHBHHBH", vbr, 11)
    tot32, fatsz32 = struct.unpack_from("<II", vbr, 0x20)
    if bps not in (512, 1024, 2048, 4096) or not spc or spc & (spc - 1) or not rsvd or not nfats:
        return ""
    fatsz, total = fatsz16 or fatsz32, tot16 or tot32
    data = total - rsvd - nfats * fatsz - (rootent * 32 + bps - 1) // bps
    if not fatsz or data <= 0:
        return ""
    if not fatsz16:
        return "FAT32"
    return "FAT12" if data // spc < 4085 else "FAT16"


def classify(code: bytes, table) -> str:
    if not code.strip(b"\x00"):
        return "none"
    for needle, name in table:
        if needle in code:
            return name
    return "unknown"


@dataclass
class BootChain:
    disk: str = ""                # appliance device of the disk, e.g. /dev/sda
    partition: int = 0            # partition number of the system (0 = whole disk, no table)
    scheme: str = ""              # msdos / gpt / none
    mbr_code: str = ""            # loader in sector 0 (when partitioned)
    active: list[int] = None      # partitions marked active
    vbr_code: str = ""            # loader in the system partition's boot sector
    fat: str = ""                 # FAT12 / FAT16 / FAT32 ('' if not FAT)
    part_id: int = 0              # MBR partition type of the system's partition
    start: int = 0                # its first and last sector on the disk
    end: int = 0
    hidden: int = 0               # where its boot sector says it starts (BPB hidden sectors)

    @property
    def problems(self) -> list[str]:
        out = []
        if self.scheme == "gpt":
            out.append("The disk uses GPT, which DOS and Windows 9x cannot boot from.")
            return out
        if self.partition and self.mbr_code in UNBOOTABLE:
            out.append("The disk's master boot record has no boot code.")
        if self.partition and self.partition not in (self.active or []):
            other = ", ".join(str(n) for n in self.active or [])
            out.append("The system's partition is not marked active"
                       + (f" (partition {other} is)." if other else " (no partition is)."))
        if self.vbr_code in UNBOOTABLE:
            out.append("The system's partition has no working boot code"
                       + (" (it was formatted with Linux's mkfs.fat, which writes a "
                          "\"This is not a bootable disk\" placeholder)." if self.vbr_code == "mkfs.fat"
                          else "."))
        if self.scheme == "msdos" and self.partition and self.fat:
            if self.part_id not in FAT_IDS:
                out.append(f"The system's partition has type 0x{self.part_id:02X}, which DOS does "
                           f"not recognise as {self.fat}.")
            elif self.part_id in CHS_TYPES and self.end >= CHS_LIMIT and self.fat != "FAT12":
                out.append(f"The system's partition reaches past the first 8 GB but its type "
                           f"(0x{self.part_id:02X}) tells DOS to use CHS addressing, which cannot "
                           "get there.")
        if self.fat and self.hidden != self.start and self.vbr_code not in UNBOOTABLE:
            out.append(f"The boot sector says the partition starts at sector {self.hidden}, but it "
                       f"starts at {self.start}.")
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["problems"] = self.problems
        return d


def analyze(g, root: str) -> BootChain:
    """Inspect the boot chain leading to ``root`` (a partition or a whole-disk filesystem)."""
    try:
        disk, part = g.part_to_dev(root), g.part_to_partnum(root)
    except RuntimeError:
        disk, part = root, 0                       # filesystem on the whole disk
    bc = BootChain(disk=disk, partition=part, active=[])
    try:
        bc.scheme = g.part_get_parttype(disk) if part else "none"
    except RuntimeError:
        bc.scheme = "none"
    if part:
        bc.mbr_code = classify(g.pread_device(disk, 440, 0), MBR_LOADERS)
        for p in g.part_list(disk):
            if p["part_num"] == part:
                bc.start, bc.end = p["part_start"] // SECTOR, p["part_end"] // SECTOR
        if bc.scheme == "msdos":
            for p in g.part_list(disk):
                try:
                    if g.part_get_bootable(disk, p["part_num"]):
                        bc.active.append(p["part_num"])
                except RuntimeError:
                    pass
            try:
                bc.part_id = g.part_get_mbr_id(disk, part)
            except RuntimeError:
                pass
    # FAT32 boot code continues in sector 2 of the partition, so look at three sectors.
    vbr = g.pread_device(root, 3 * SECTOR, 0)
    bc.vbr_code = classify(vbr[:SECTOR - 2] + vbr[2 * SECTOR:], VBR_LOADERS)
    bc.fat = fat_type(vbr[:SECTOR])
    if bc.fat:
        bc.hidden = struct.unpack_from("<I", vbr, 0x1C)[0]
    return bc
