from rehome.inspect import bootchain as bc


def test_classify_boot_sectors():
    mkfs = b"\x0e\x1f\xbe[|\xac" + b"This is not a bootable disk.  Please insert a bootable floppy and"
    assert bc.classify(mkfs, bc.VBR_LOADERS) == "mkfs.fat"
    assert bc.classify(b"\0" * 512, bc.VBR_LOADERS) == "none"
    assert bc.classify(b"...Invalid system disk...IO      SYSMSDOS   SYS", bc.VBR_LOADERS) == "msdos"
    assert bc.classify(b"Remove disks or other media.\xffDisk error", bc.VBR_LOADERS) == "msdos"
    assert bc.classify(b"KERNEL  SYS", bc.VBR_LOADERS) == "freedos"
    assert bc.classify(b"\x90SYSLINUX", bc.VBR_LOADERS) == "syslinux"
    assert bc.classify(b"Invalid partition table\0Error loading", bc.MBR_LOADERS) == "microsoft"


def test_problems_and_their_order():
    chain = bc.BootChain(disk="/dev/sda", partition=1, scheme="msdos", mbr_code="none",
                         active=[2], vbr_code="mkfs.fat")
    p = chain.problems
    assert len(p) == 3 and "no boot code" in p[0] and "partition 2 is" in p[1] and "mkfs.fat" in p[2]
    ok = bc.BootChain(disk="/dev/sda", partition=1, scheme="msdos", mbr_code="microsoft",
                      active=[1], vbr_code="msdos")
    assert ok.problems == []
    assert "GPT" in bc.BootChain(partition=1, scheme="gpt").problems[0]
    whole_disk = bc.BootChain(disk="/dev/sda", partition=0, scheme="none", vbr_code="mkfs.fat")
    assert len(whole_disk.problems) == 1


def _bpb(fat32: bool, total: int, spc: int, hidden: int = 63) -> bytes:
    import struct
    vbr = bytearray(512)
    vbr[0:3] = b"\xeb\x58\x90" if fat32 else b"\xeb\x3c\x90"
    if fat32:
        fatsz = (total // spc * 4 + 511) // 512
        struct.pack_into("<HBHBHHBHHHII", vbr, 11, 512, spc, 32, 2, 0, 0, 0xF8, 0, 63, 255, hidden,
                         total)
        struct.pack_into("<IHHIHH", vbr, 0x24, fatsz, 0, 0, 2, 1, 6)
    else:
        fatsz = (total // spc * 2 + 511) // 512
        struct.pack_into("<HBHBHHBHHHII", vbr, 11, 512, spc, 1, 2, 512, 0, 0xF8, fatsz, 63, 255,
                         hidden, total)
    vbr[510:] = b"\x55\xaa"
    return bytes(vbr)


def test_fat_type_from_the_bpb():
    assert bc.fat_type(_bpb(True, 600_000, 1)) == "FAT32"
    assert bc.fat_type(_bpb(False, 600_000, 16)) == "FAT16"
    assert bc.fat_type(_bpb(False, 8_000, 4)) == "FAT12"          # < 4085 clusters
    assert bc.fat_type(b"\0" * 512) == "" and bc.fat_type(b"NTFS    " * 64) == ""


def test_partition_type_and_hidden_sector_problems():
    linux = bc.BootChain(disk="/dev/sda", partition=1, scheme="msdos", mbr_code="microsoft",
                         active=[1], vbr_code="msdos", fat="FAT32", part_id=0x83, start=2048,
                         end=999_999, hidden=0)
    p = linux.problems
    assert len(p) == 2 and "0x83" in p[0] and "starts at sector 0, but it starts at 2048" in p[1]
    far = bc.BootChain(disk="/dev/sda", partition=1, scheme="msdos", mbr_code="microsoft",
                       active=[1], vbr_code="msdos", fat="FAT32", part_id=0x0B, start=63,
                       end=bc.CHS_LIMIT + 5, hidden=63)
    assert len(far.problems) == 1 and "CHS" in far.problems[0]
    far.part_id = 0x0C
    assert far.problems == []


def test_partition_type_rehome_sets():
    from rehome.patch.legacy import _dos_partition_type as pick
    chain = dict(disk="/dev/sda", partition=1, scheme="msdos", start=63)
    assert pick(bc.BootChain(**chain, fat="FAT32", part_id=0x83, end=500_000), lba=True) == 0x0C
    assert pick(bc.BootChain(**chain, fat="FAT32", part_id=0x0B, end=500_000), lba=True) == 0
    assert pick(bc.BootChain(**chain, fat="FAT32", part_id=0x0B, end=bc.CHS_LIMIT), lba=True) == 0x0C
    assert pick(bc.BootChain(**chain, fat="FAT16", part_id=0x06, end=bc.CHS_LIMIT), lba=True) == 0x0E
    # MS-DOS 6 does not know the LBA types.
    assert pick(bc.BootChain(**chain, fat="FAT16", part_id=0x06, end=bc.CHS_LIMIT), lba=False) == 0
    assert pick(bc.BootChain(**chain, fat="FAT16", part_id=0x83, end=200_000), lba=False) == 0x06
    assert pick(bc.BootChain(**chain, fat="FAT16", part_id=0x83, end=30_000), lba=False) == 0x04
    assert pick(bc.BootChain(**chain, fat="FAT12", part_id=0x83, end=8_000), lba=True) == 0x01


def test_rehomes_boot_code_is_recognised():
    from rehome.patch import bootcode
    assert bc.classify(bootcode.WIN9X_FAT32, bc.VBR_LOADERS) == "rehome"
    assert bc.classify(bootcode.WIN9X_FAT16, bc.VBR_LOADERS) == "rehome"
    assert bc.classify(bootcode.MBR, bc.MBR_LOADERS) == "rehome"
    assert "rehome" not in bc.UNBOOTABLE


def test_boot_code_matches_its_source():
    """rehome/patch/bootcode.py is scripts/gen_boot_code.py's output for the .asm files."""
    import shutil
    import pytest
    if not shutil.which("nasm"):
        pytest.skip("nasm is not installed")
    import importlib.util
    from pathlib import Path
    from rehome.patch import bootcode
    path = Path(__file__).resolve().parents[2] / "scripts" / "gen_boot_code.py"
    spec = importlib.util.spec_from_file_location("gen_boot_code", path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    for name, (source, options, size) in gen.CODE.items():
        code = gen.assemble(source, options)
        assert len(code) == size and code == getattr(bootcode, name), name
    # Boot sectors: a jump over the BPB to the code, and the 55AA signature.
    assert bootcode.WIN9X_FAT32[:3] == b"\xeb\x58\x90" and bootcode.WIN9X_FAT16[:3] == b"\xeb\x3c\x90"
    assert bootcode.WIN9X_FAT32[510:] == bootcode.WIN9X_FAT16[510:] == b"\x55\xaa"
