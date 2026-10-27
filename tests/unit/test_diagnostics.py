from rehome.vm.diagnostics import cpu_summary

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
