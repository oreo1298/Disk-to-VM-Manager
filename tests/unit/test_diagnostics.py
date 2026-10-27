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
    """The BIOS's video variables, the code at CS:EIP and the stack, at linear addresses."""
    dump = DUMP + ("ESP=000135de\nSS =ed6c 000ed6c0 0000ffff 0000f300 DPL=3 DS16 [-WA]\n")
    asked = []
    notes = memory_notes(dump, lambda a, f: asked.append((a, f)) or f"{a:08x}: 00\r\n")
    assert asked == [(0x449, "/26xb"), (0x484, "/4xb"), (0xC0000 + 0x2007, "/32xb"),
                     (0xED6C0 + 0x35DE, "/48xh")]          # a 16-bit stack: SP only
    assert "Stack at SS:ESP:" in notes and "000f0c9e: 00" in notes
    flat = dump.replace("DPL=3 DS16 [-WA]", "DPL=0 DS   [-WA]")
    asked.clear()
    memory_notes(flat, lambda a, f: asked.append((a, f)) or "")
    assert asked[-1] == (0xED6C0 + 0x135DE, "/48xh")
    assert memory_notes("no registers", lambda a, f: "") == []
