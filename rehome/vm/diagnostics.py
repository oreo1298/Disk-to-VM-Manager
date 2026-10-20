"""Collect diagnostics for a VM ("Copy diagnostics" in the GUI)."""

from __future__ import annotations

import json
import platform

from rehome import __version__
from rehome.host import doctor
from rehome.vm import runner
from rehome.vm.config import VMConfig


def _tail(path, n=80) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(none)"


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
    for extra in ("patch.log", "import.log"):
        p = cfg.dir / "logs" / extra
        if p.exists():
            parts += ["", f"Last {extra} lines:", _tail(p, 40)]
    return "\n".join(parts)
