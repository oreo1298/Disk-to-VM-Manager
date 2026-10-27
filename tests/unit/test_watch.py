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
        return self.dump if command == "info registers" else ""

    def execute(self, cmd, args=None):
        self.commands.append(cmd)


def run_watch(monkeypatch, tmp_path, states):
    from rehome.vm import qmp
    fake = FakeQemu(states)
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
