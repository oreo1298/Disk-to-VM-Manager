"""Collect diagnostics for a VM ("Copy diagnostics" in the GUI)."""

from __future__ import annotations

import json
import platform
import re
import time

from rehome import __version__
from rehome.host import doctor
from rehome.vm import runner
from rehome.vm.config import VMConfig


def _tail(path, n=80) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(none)"


def cpu_summary(dump: str) -> str:
    """One line from QEMU's "info registers" (x86, first CPU): the mode, the code it runs,
    and whether it is halted, e.g. "V86 mode at C000:00002007, running, interrupts on"."""
    def reg(name):
        m = re.search(rf"\b{name}\s*=\s*([0-9a-fA-F]+)", dump)
        return int(m.group(1), 16) if m else None
    eip, efl, cr0, cs = reg("EIP"), reg("EFL"), reg("CR0"), reg("CS")
    if eip is None or efl is None or cr0 is None or cs is None:
        return "(no x86 register dump)"
    mode = ("real mode" if not cr0 & 1 else "V86 mode" if efl & 1 << 17
            else "protected mode")
    halted = re.search(r"\bHLT=1\b", dump) is not None
    return (f"{mode} at {cs:04X}:{eip:08X}, {'halted' if halted else 'running'}, "
            f"interrupts {'on' if efl & 1 << 9 else 'off'}")


def cpu_state(cfg: VMConfig, samples: int = 2, interval: float = 1.0) -> str:
    """Where a running guest's processor is, sampled ``samples`` times (for hangs: halted
    for good, or looping somewhere), with the last full register dump."""
    dumps = []
    with runner.qmp(cfg) as q:
        for i in range(samples):
            if i:
                time.sleep(interval)
            dumps.append(q.hmp("info registers").replace("\r", ""))
    return "\n".join([cpu_summary(d) for d in dumps] + ["", dumps[-1].strip()])


def collect(cfg: VMConfig) -> str:
    st = runner.state(cfg)
    rep = doctor.run(deep=False)
    problems = [f"- [{c.status}] {c.title}: {c.detail}" for c in rep.problems]
    cfg_dict = cfg.to_dict()
    # Strip things that could identify the user beyond what is needed.
    for d in cfg_dict.get("disks", []):
        src = d.get("source")
        if src:
            src.pop("serial", None)
    parts = [
        f"Rehome {__version__} diagnostics for \"{cfg.name}\"",
        f"Host: {rep.facts.get('system', {}).get('distro')} ({platform.machine()}), "
        f"kernel {platform.release()}",
        f"QEMU: {rep.facts.get('qemu', {}).get('version')}  accel: {rep.facts.get('accel', {}).get('accel')}",
        f"State: {st.status}",
        "",
        "Host problems:" if problems else "Host problems: none",
        *problems,
        "",
        "Configuration:",
        json.dumps(cfg_dict, indent=2),
        "",
        "Last QEMU log lines:",
        _tail(cfg.dir / "logs" / "qemu.log"),
    ]
    insp = cfg.dir / "inspection.json"
    if insp.exists():
        try:
            oses = json.loads(insp.read_text()).get("oses", [])
            for o in oses:
                keep = {k: o.get(k) for k in ("family", "distro", "name", "version", "build", "arch",
                                               "boot_mode", "bootloader", "original_bus", "hal",
                                               "hibernated", "encryption", "hypervisor_tools")}
                keep["details"] = {k: v for k, v in (o.get("details") or {}).items()
                                   if not isinstance(v, (dict, list)) or len(str(v)) < 400}
                parts += ["", "Detected system:", json.dumps(keep, indent=2),
                          *(f"  note: {n}" for n in o.get("notes", []) + o.get("warnings", []))]
        except (OSError, ValueError):
            pass
    if st.active:
        # Boot errors of DOS-era systems (and BIOS messages) are on a text screen.
        try:
            text = runner.screen_text(cfg).rstrip()
        except Exception:  # noqa: BLE001 - graphics mode or QMP busy
            text = ""
        parts += ["", "Screen text:", text or "(graphics mode; take a screenshot with "
                  f"`rehome screenshot \"{cfg.name}\" screen.png`)"]
        try:
            parts += ["", "Processor (two samples, a second apart):", cpu_state(cfg)]
        except Exception as e:  # noqa: BLE001 - QMP busy, or not an x86 guest
            parts += ["", f"Processor: not available ({e})"]
    serial = cfg.dir / "logs" / "serial.log"
    if serial.exists() and serial.stat().st_size:
        parts += ["", "Last serial console lines:", _tail(serial, 30)]
    for extra in ("watch.log", "patch.log", "import.log"):
        p = cfg.dir / "logs" / extra
        if p.exists():
            parts += ["", f"Last {extra} lines:", _tail(p, 40)]
    return "\n".join(parts)
