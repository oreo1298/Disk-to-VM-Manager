"""The watcher that turns Windows 9x VMs off once Windows has finished shutting down (D45)."""

from rehome.vm import watch

REAL_HALTED = """CPU#0
EAX=00000000 EBX=00000000 ECX=00000000 EDX=00000000
EIP=00000a1c EFL=00000002 [-------] CPL=0 II=0 A20=1 SMM=0 HLT=1
CS =0070 00000700 0000ffff 00009b00
CR0=00000010 CR2=00000000 CR3=00000000 CR4=00000000
"""


def sample(**changes):
    s = watch.parse(REAL_HALTED)
    s.update(changes)
    return s


def test_registers_are_read():
    s = watch.parse(REAL_HALTED)
    assert s == {"real": True, "v86": False, "cs": 0x70, "eip": 0xA1C, "halted": True,
                 "interrupts": False}
    assert watch.describe(s) == "real mode at 0070:00000A1C, halted, interrupts off"
    assert watch.parse("not a register dump") is None


def test_only_a_processor_nothing_can_wake_counts():
    halted = [sample()] * watch.CONFIRM
    assert watch.stopped_for_good(halted)
    assert not watch.stopped_for_good(halted[1:])                     # not long enough yet
    spinning = [sample(halted=False)] * watch.CONFIRM                 # cli; jmp $
    assert watch.stopped_for_good(spinning)
    moving = [sample(halted=False, eip=0xA1C + i) for i in range(watch.CONFIRM)]
    assert not watch.stopped_for_good(moving)
    # A DOS prompt waits with interrupts on; Windows' error screens are in protected mode.
    assert not watch.stopped_for_good([sample(interrupts=True)] * watch.CONFIRM)
    assert not watch.stopped_for_good([sample(real=False)] * watch.CONFIRM)
    assert not watch.stopped_for_good([sample(), sample(), sample(interrupts=True)])


def test_the_watcher_runs_rehome_from_where_it_is_installed():
    argv = watch.sidecar_argv("vm-id", "/run/q.sock", "/logs/watch.log")
    import sys
    from pathlib import Path
    assert argv[0] == sys.executable and argv[-3:] == ["vm-id", "/run/q.sock", "/logs/watch.log"]
    assert (Path(argv[3]) / "rehome" / "vm" / "watch.py").is_file()


class FakeQemu:
    """QMP as the watcher uses it: a run state and a register dump per sample."""
    def __init__(self, states):
        self.states = list(states)          # (status, dump) per sample
        self.commands: list[str] = []
        self.memory = ""                    # QEMU's reply to `xp`

    def connect(self, retries=0.0):
        return self

    def close(self):
        pass

    def status(self):
        from rehome.vm.qmp import QmpClosed
        if not self.states:
            raise QmpClosed()
        self.status_now, self.dump = self.states.pop(0)
        return self.status_now

    def hmp(self, command):
        self.commands.append(command)
        if command == "info registers":
            return self.dump
        return self.memory if command.startswith("xp ") else ""

    def execute(self, cmd, args=None):
        self.commands.append(cmd)


def run_watch(monkeypatch, tmp_path, states, memory=""):
    from rehome.vm import qmp
    fake = FakeQemu(states)
    fake.memory = memory
    monkeypatch.setattr(qmp, "QMP", lambda *a, **k: fake)
    sock = tmp_path / "qmp-watch.sock"
    sock.touch()
    lines: list[str] = []
    result = watch.watch(str(sock), lines.append, interval=0)
    return result, fake, lines


def windows_running(i):
    return REAL_HALTED.replace("CR0=00000010", "CR0=80000011").replace(
        "EIP=00000a1c", f"EIP={0xC0001000 + i:08x}").replace("HLT=1", "HLT=0")


def test_the_vm_is_turned_off_as_the_guest_would(monkeypatch, tmp_path):
    states = [("running", windows_running(i)) for i in range(5)] + \
             [("running", REAL_HALTED)] * watch.CONFIRM
    result, fake, lines = run_watch(monkeypatch, tmp_path, states)
    assert result == "off" and fake.commands[-1] == watch.POWER_OFF
    assert lines[0].startswith("protected mode at 0070:C0001000")
    assert lines[-1] == ("the guest has stopped for good (real mode at 0070:00000A1C, halted, "
                         "interrupts off): turning the VM off")


def test_a_paused_vm_is_not_judged(monkeypatch, tmp_path):
    """Paused with interrupts off in real mode (a DOS program): left alone."""
    states = [("running", REAL_HALTED)] * (watch.CONFIRM - 1) + \
             [("paused", REAL_HALTED)] * 10 + [("running", REAL_HALTED)] * (watch.CONFIRM - 1)
    result, fake, _lines = run_watch(monkeypatch, tmp_path, states)
    assert result == "gone" and watch.POWER_OFF not in fake.commands


def test_the_log_stays_small_while_windows_runs(monkeypatch, tmp_path):
    """Months of samples in a running Windows: one line, not one per sample."""
    states = [("running", windows_running(i)) for i in range(5000)]
    result, _fake, lines = run_watch(monkeypatch, tmp_path, states)
    assert result == "gone" and len(lines) == 1
    # A DOS program flipping interrupts on and off: at most LOG_LIMIT lines and a note.
    flips = [("running", REAL_HALTED.replace("EFL=00000002", f"EFL={0x202 if i % 2 else 2:08x}")
              .replace("HLT=1", "HLT=0").replace("EIP=00000a1c", f"EIP={i:08x}"))
             for i in range(2000)]
    result, _fake, lines = run_watch(monkeypatch, tmp_path, flips)
    assert len(lines) <= watch.LOG_LIMIT + 1 and lines[-1] == "(further changes are not written)"


# The reporter's Windows 98 under KVM after its VGA BIOS was overwritten (D49): V86 mode,
# running the text cells at C000 with interrupts off.
KVM_HANG = """CPU#0
EAX=00000000 EBX=00005a80 ECX=00000100 EDX=00006580
ESI=0e0fff67 EDI=00008000 EBP=000001fe ESP=000037b4
EIP=00001dce EFL=00020046 [---Z-P-] CPL=3 II=0 A20=1 SMM=0 HLT=0
ES =0040 00000400 0000ffff 0000f300 DPL=3 DS16 [-WA]
CS =c000 000c0000 0000ffff 0000f300 DPL=3 DS16 [-WA]
SS =14b9 00014b90 0000ffff 0000f300 DPL=3 DS16 [-WA]
CR0=80000019 CR2=000bf000 CR3=007d4000 CR4=00000200
"""
OVERWRITTEN = "00000000000c0000: 0x20 0x07\n"
INTACT = "00000000000c0000: 0x55 0xaa\n"


def kvm_hang(eip):
    return KVM_HANG.replace("EIP=00001dce", f"EIP={eip:08x}")


def test_the_processor_in_the_vga_bios_with_interrupts_off():
    s = watch.parse(KVM_HANG)
    assert s["v86"] and not s["real"] and not s["interrupts"]
    assert watch.describe(s) == "V86 mode at C000:00001DCE, running, interrupts off"
    hang = [watch.parse(kvm_hang(eip)) for eip in (0x18F0, 0x1DCE, 0x9A00)]
    assert watch.in_vga_bios(hang)
    assert not watch.in_vga_bios(hang[1:])                            # not long enough yet
    assert not watch.in_vga_bios(hang[:2] + [dict(hang[2], interrupts=True)])
    assert not watch.in_vga_bios(hang[:2] + [dict(hang[2], cs=0xF000)])  # the system BIOS
    assert not watch.in_vga_bios(hang[:2] + [dict(hang[2], v86=False)])  # protected mode
    assert watch.in_vga_bios([sample(cs=0xC000, eip=0xFFFE, halted=False)] * watch.CONFIRM)


def test_the_vga_bios_header_is_read_from_qemu():
    assert watch.vga_bios_intact(INTACT)
    assert not watch.vga_bios_intact(OVERWRITTEN)
    assert not watch.vga_bios_intact("00000000000c0000: 0x00 0x00")
    assert watch.vga_bios_intact("")                                   # no answer: left alone
    assert watch.vga_bios_intact("unknown command: 'xp'")


def test_a_vga_bios_overwritten_under_kvm_turns_the_vm_off(monkeypatch, tmp_path):
    states = [("running", windows_running(i)) for i in range(5)] + \
             [("running", kvm_hang(eip)) for eip in (0x18F0, 0x1DCE, 0x2A10)]
    result, fake, lines = run_watch(monkeypatch, tmp_path, states, OVERWRITTEN)
    assert result == "off" and fake.commands[-2:] == [watch.READ_VGA_BIOS, watch.POWER_OFF]
    assert lines[-1] == ("the VGA BIOS has been overwritten and the processor runs what "
                         "replaced it, with interrupts off (V86 mode at C000:00002A10, "
                         "running, interrupts off): turning the VM off")


def test_a_working_vga_bios_is_left_alone(monkeypatch, tmp_path):
    """A long video mode set (interrupts off in the VGA BIOS) with its header in place."""
    states = [("running", kvm_hang(0x100 + i)) for i in range(20)]
    result, fake, _lines = run_watch(monkeypatch, tmp_path, states, INTACT)
    assert result == "gone" and watch.POWER_OFF not in fake.commands
    # The header is only read while the processor is in the VGA BIOS.
    states = [("running", windows_running(i)) for i in range(20)]
    result, fake, _lines = run_watch(monkeypatch, tmp_path, states, OVERWRITTEN)
    assert result == "gone" and watch.READ_VGA_BIOS not in fake.commands
