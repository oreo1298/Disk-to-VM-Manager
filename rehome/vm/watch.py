"""Turn a Windows 95/98/ME VM off once Windows has finished shutting down (D45).

Windows 9x switches the computer off only through "Advanced Power Management
support" (APM), which is often missing, and which hung one reporter's Windows 98
in QEMU. Without it, Windows stops at "It's now safe to turn off your computer"
and the VM stays on. This watcher starts with such VMs (rehome/vm/qemu_cmd.py
adds it as a sidecar with its own QMP socket). Every few seconds it asks QEMU
where the processor is. Once Windows has left it stopped for good, it turns the
VM off as the guest would through ACPI, so QEMU, the app and the appliance all
see an ordinary shutdown by the guest.

"Stopped for good": back in real mode, with interrupts off, and halted or on
the same instruction, several times in a row, while the VM runs (a paused VM is
not judged). Nothing but a reset changes that. Windows' own error screens run in
protected mode, and a DOS prompt has interrupts on, so neither turns the VM off.

The VM's logs/watch.log, which diagnostics include, gets a line when the
processor enters or leaves real mode or turns interrupts off there, with a few
lines of detail while they are off, and how it ended. Not every sample: in a
running Windows the place changes every time, and an appliance can run for
months from a USB stick.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

INTERVAL = 2.0          # seconds between samples
CONFIRM = 3             # samples in a row that must agree
LOG_LIMIT = 200         # changes of state written per run, then only the ending
DETAIL = 10             # places written each time interrupts are off in real mode
# SLP_EN with sleep type 0 in PM1a_CNT: QEMU's PIIX4 and ICH9 power management (at the
# I/O base SeaBIOS gives them) turn that into a shutdown by the guest. SeaBIOS's own
# APM power-off writes the same.
POWER_OFF = "o /w 0x604 0x2000"


def parse(dump: str) -> dict | None:
    """The first CPU's state from QEMU's "info registers" (x86)."""
    def reg(name):
        m = re.search(rf"\b{name}\s*=\s*([0-9a-fA-F]+)", dump)
        return int(m.group(1), 16) if m else None
    eip, efl, cr0, cs = reg("EIP"), reg("EFL"), reg("CR0"), reg("CS")
    if None in (eip, efl, cr0, cs):
        return None
    return {"real": not cr0 & 1, "v86": bool(cr0 & 1 and efl & 1 << 17),
            "cs": cs, "eip": eip, "halted": re.search(r"\bHLT=1\b", dump) is not None,
            "interrupts": bool(efl & 1 << 9)}


def describe(s: dict) -> str:
    mode = "real mode" if s["real"] else "V86 mode" if s["v86"] else "protected mode"
    return (f"{mode} at {s['cs']:04X}:{s['eip']:08X}, {'halted' if s['halted'] else 'running'}, "
            f"interrupts {'on' if s['interrupts'] else 'off'}")


def kind(s: dict) -> str:
    """The state the log follows: where Windows (or DOS) runs, not the exact place."""
    if not s["real"]:
        return "protected"          # protected or V86 mode: Windows or a DOS extender runs
    return "real" if s["interrupts"] else "real, interrupts off"


def stopped_for_good(samples: list[dict]) -> bool:
    """The last CONFIRM samples show a processor nothing but a reset can wake."""
    last = samples[-CONFIRM:]
    if len(last) < CONFIRM:
        return False
    for s in last:
        if not s["real"] or s["interrupts"]:
            return False
    halted = all(s["halted"] for s in last)
    same_place = len({(s["cs"], s["eip"]) for s in last}) == 1
    return halted or same_place


def watch(qmp_path: str, log, interval: float = INTERVAL, wait: float = 120.0) -> str:
    """Watch until QEMU goes away ("gone") or the guest stopped for good ("off")."""
    from rehome.errors import RehomeError
    from rehome.vm.qmp import QMP
    deadline = time.monotonic() + wait
    while not os.path.exists(qmp_path):
        if time.monotonic() > deadline:
            log("QEMU did not start")
            return "gone"
        time.sleep(0.5)
    try:
        q = QMP(qmp_path, timeout=10).connect(retries=wait)
    except RehomeError as e:
        log(f"cannot connect to QEMU: {e}")
        return "gone"
    samples: list[dict] = []
    last_kind = last_text = ""
    written = detail = 0
    try:
        while True:
            try:
                running = q.status() == "running"
                s = parse(q.hmp("info registers")) if running else None
            except RehomeError:
                return "gone"
            if not running:                 # paused, or being saved: judge it afresh later
                samples = []
                time.sleep(interval)
                continue
            if s is None:
                log("no x86 registers; not watching")
                return "gone"
            text = describe(s)
            if kind(s) != last_kind:
                detail = 0
                if written < LOG_LIMIT:
                    log(text)
                elif written == LOG_LIMIT:
                    log("(further changes are not written)")
                written += 1
            elif kind(s) == "real, interrupts off" and text != last_text and detail < DETAIL \
                    and written <= LOG_LIMIT:
                log(text)
                detail += 1
            last_kind, last_text = kind(s), text
            samples = (samples + [s])[-CONFIRM:]
            if stopped_for_good(samples):
                log(f"the guest has stopped for good ({text}): turning the VM off")
                reply = q.hmp(POWER_OFF)
                if reply.strip():           # this QEMU cannot write I/O ports from its monitor
                    log(f"port write failed ({reply.strip()}); quitting QEMU instead")
                    q.execute("quit")
                return "off"
            time.sleep(interval)
    finally:
        q.close()


def main(argv: list[str]) -> int:
    """`python -c ... watch.main(argv)`: watch.main([vm_id, qmp_socket, log_file])."""
    vm_id, qmp_path, log_path = argv

    def log(msg: str) -> None:
        with open(log_path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    log(f"watching {vm_id} for the end of Windows' shutdown")
    watch(qmp_path, log)
    return 0


def sidecar_argv(vm_id: str, qmp_path: str, log_path: str) -> list[str]:
    """The watcher's command: Rehome's Python and package, wherever they are installed."""
    root = str(Path(__file__).resolve().parents[2])
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from rehome.vm import watch; "
            "sys.exit(watch.main(sys.argv[2:]))")
    return [sys.executable, "-c", code, root, vm_id, qmp_path, log_path]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
