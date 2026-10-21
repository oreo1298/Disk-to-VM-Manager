"""The BIOS boot chain of DOS-era systems: MBR code, active partition, boot sector.

A BIOS loads sector 0 of the disk (the MBR), whose code starts the boot
sector of the *active* partition, whose code loads the system (IO.SYS for
MS-DOS and Windows 9x, KERNEL.SYS for FreeDOS). Disks prepared on Linux often
break this chain: a partition formatted with mkfs.fat has placeholder code
("This is not a bootable disk..."), and partitioning tools may leave no
partition marked active or no MBR code at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

SECTOR = 512

# Boot sector (VBR) code, by distinctive strings. Checked in order.
VBR_LOADERS = [
    (b"This is not a bootable disk", "mkfs.fat"),        # dosfstools placeholder
    (b"WINBOOT SYS", "msdos"), (b"IO      SYS", "msdos"),  # MS-DOS 5-7 and Windows 9x
    (b"IBMBIO  COM", "pcdos"), (b"KERNEL  SYS", "freedos"),
    (b"Remove disks or other media", "msdos"), (b"Invalid system disk", "msdos"),
    (b"NTLDR", "ntldr"), (b"BOOTMGR", "bootmgr"),
    (b"SYSLINUX", "syslinux"), (b"GRUB", "grub"),
]
MBR_LOADERS = [
    (b"Invalid partition table", "microsoft"), (b"Error loading operating system", "microsoft"),
    (b"Missing operating system", "microsoft"), (b"Missing OS", "syslinux"),
    (b"GRUB", "grub"), (b"LILO", "lilo"), (b"This is not a bootable disk", "mkfs.fat"),
]
# Boot sectors that cannot start anything (Rehome may replace them in the overlay).
UNBOOTABLE = ("none", "mkfs.fat")


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
        if bc.scheme == "msdos":
            for p in g.part_list(disk):
                try:
                    if g.part_get_bootable(disk, p["part_num"]):
                        bc.active.append(p["part_num"])
                except RuntimeError:
                    pass
    # FAT32 boot code continues in sector 2 of the partition, so look at three sectors.
    vbr = g.pread_device(root, 3 * SECTOR, 0)
    bc.vbr_code = classify(vbr[:SECTOR - 2] + vbr[2 * SECTOR:], VBR_LOADERS)
    return bc
