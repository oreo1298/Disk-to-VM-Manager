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
