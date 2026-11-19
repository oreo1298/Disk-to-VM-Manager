from rehome.vm.diagnostics import cpu_summary, memory_notes

DUMP = """CPU#0
EAX=00000000 EBX=00000000 ECX=00000000 EDX=00000663
EIP=00002007 EFL=00020202 [-------] CPL=3 II=0 A20=1 SMM=0 HLT=0
ES =0000 00000000 0000ffff 00009300
CS =c000 000c0000 0000ffff 0000f300
CR0=80000011 CR2=00000000 CR3=00000000 CR4=00000000
"""


def test_where_the_processor_is():
    """For hangs: Windows 98 inside the VGA BIOS in V86 mode, or a guest halted for good."""
    assert cpu_summary(DUMP) == "V86 mode at C000:00002007, running, interrupts on"
    done = (DUMP.replace("HLT=0", "HLT=1").replace("EFL=00020202", "EFL=00000002")
            .replace("CR0=80000011", "CR0=00000010").replace("CS =c000", "CS =f000"))
    assert cpu_summary(done) == "real mode at F000:00002007, halted, interrupts off"
    pm = DUMP.replace("EFL=00020202", "EFL=00000202").replace("CS =c000", "CS =0028")
    assert cpu_summary(pm).startswith("protected mode at 0028:00002007")
    assert cpu_summary("no registers") == "(no x86 register dump)"


def test_the_memory_around_a_hang():
    """The BIOS's video variables, the code at CS:EIP and the stack, at linear addresses;
    then where the page tables send text memory and the VGA BIOS, and QEMU's map (D48)."""
    dump = DUMP + ("ESP=000135de\nSS =ed6c 000ed6c0 0000ffff 0000f300 DPL=3 DS16 [-WA]\n")
    asked = []

    def hmp(cmd):
        asked.append(cmd)
        if cmd.startswith("gva2gpa"):
            linear = int(cmd.split()[1], 16)
            return "Unmapped\r\n" if linear == 0xC7000 else f"gpa: {linear ^ 0x79000:#x}\r\n"
        if cmd == "info mtree -f":
            return ("FlatView #0\n  00000000000a0000-00000000000bffff (prio 0, i/o): "
                    "cirrus-low-memory\n  00000000000c0000-00000000000c8fff (prio 0, rom): "
                    "pc.ram @00000000000c0000\n  0000000000100000-000000000fffffff (prio 0, "
                    "ram): pc.ram\nFlatView #1\n  00000000000a0000-00000000000bffff (prio 0, "
                    "i/o): cirrus-low-memory\n")
        return f"{cmd}: 00\r\n"
    notes = memory_notes(dump, hmp)
    assert asked[:4] == ["x /26xb 0x449", "x /4xb 0x484", "x /32xb 0xc2007",
                         "x /48xh 0xf0c9e"]                 # a 16-bit stack: SP only
    assert "Stack at SS:ESP:" in notes and "x /48xh 0xf0c9e: 00" in notes
    pages = notes[notes.index("Linear to physical pages (the guest's page tables), A0000-C7FFF:") + 1:]
    assert pages[0].split()[0] == "A0000->D9000" and pages[3].split()[0] == "B8000->C1000"
    assert pages[4].split()[-1] == "C7000->none" and len(" ".join(pages[:5]).split()) == 40
    assert "Physical memory behind CS:EIP (0xbb007):" in notes
    assert notes[notes.index("QEMU's memory map, A0000-CFFFF:") + 1:] == [
        "00000000000a0000-00000000000bffff (prio 0, i/o): cirrus-low-memory",
        "00000000000c0000-00000000000c8fff (prio 0, rom): pc.ram @00000000000c0000"]
    flat = dump.replace("DPL=3 DS16 [-WA]", "DPL=0 DS   [-WA]")
    asked.clear()
    memory_notes(flat, hmp)
    assert asked[3] == "x /48xh 0x100c9e"            # ED6C0 + 135DE: the whole ESP
    assert memory_notes("no registers", hmp) == []
