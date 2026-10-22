"""What computer the appliance is running on (inside the appliance).

The report is written on first boot and shown in the maintenance console;
``start_checks`` runs before every VM start and adapts the VM to the
computer (memory, processors) and explains what will not work well.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rehome.util.units import human
from rehome.vm.config import VMConfig

RESERVED_MIB = 768          # left to the appliance's own Linux


def _read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return default


def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                              check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class Report:
    cpu: str = ""
    cpu_vendor: str = ""
    cpus: int = 0
    virtualization: str = ""           # vmx / svm / ""
    kvm: bool = False
    memory_mib: int = 0
    firmware: str = ""                 # uefi / bios
    product: str = ""                  # e.g. "LENOVO ThinkPad T480"
    gpus: list[str] = field(default_factory=list)
    displays: list[str] = field(default_factory=list)   # connected outputs, e.g. "card1-eDP-1"
    sound: list[str] = field(default_factory=list)
    network: list[str] = field(default_factory=list)    # "enp3s0 (wired)", "wlan0 (Wi-Fi)"
    disks: list[str] = field(default_factory=list)
    kernel: str = ""

    def to_text(self) -> str:
        lines = [
            f"Computer:   {self.product or 'unknown'} ({self.firmware.upper() or '?'} firmware)",
            f"Processor:  {self.cpu} ({self.cpus} threads)",
            "Virtualization: " + ("available (KVM)" if self.kvm else
                                  "turned off in the firmware settings" if not self.virtualization
                                  else "supported by the processor but not usable"),
            f"Memory:     {human(self.memory_mib * 2**20)}",
        ]
        lines += [f"Graphics:   {g}" for g in self.gpus or ["none found"]]
        lines += [f"Display:    {d}" for d in self.displays or ["no connected display found"]]
        lines += [f"Sound:      {s}" for s in self.sound or ["none found"]]
        lines += [f"Network:    {n}" for n in self.network or ["none found"]]
        lines += [f"Drive:      {d}" for d in self.disks]
        lines.append(f"Linux:      {self.kernel}")
        return "\n".join(lines)


def gather() -> Report:
    r = Report()
    cpuinfo = _read("/proc/cpuinfo")
    m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    r.cpu = m.group(1).strip() if m else "unknown"
    m = re.search(r"^vendor_id\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    r.cpu_vendor = m.group(1).strip() if m else ""
    r.cpus = os.cpu_count() or 1
    flags = re.search(r"^flags\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    fl = set(flags.group(1).split()) if flags else set()
    r.virtualization = "vmx" if "vmx" in fl else "svm" if "svm" in fl else ""
    r.kvm = os.path.exists("/dev/kvm")
    m = re.search(r"^MemTotal:\s*(\d+) kB", _read("/proc/meminfo"), re.MULTILINE)
    r.memory_mib = int(m.group(1)) // 1024 if m else 0
    r.firmware = "uefi" if os.path.isdir("/sys/firmware/efi") else "bios"
    r.product = " ".join(x for x in (_read("/sys/class/dmi/id/sys_vendor"),
                                     _read("/sys/class/dmi/id/product_name"),
                                     _read("/sys/class/dmi/id/product_version")) if x
                         and x.lower() not in ("to be filled by o.e.m.", "system product name",
                                               "system version", "default string"))
    r.gpus = _gpus()
    drm = Path("/sys/class/drm")
    if drm.is_dir():
        for c in sorted(drm.iterdir()):
            if "-" in c.name and _read(c / "status") == "connected":
                modes = _read(c / "modes").splitlines()
                r.displays.append(c.name + (f" ({modes[0]})" if modes else ""))
    for line in _read("/proc/asound/cards").splitlines():
        m = re.match(r"\s*\d+ \[.*?\]: (.+)", line)
        if m:
            r.sound.append(m.group(1).strip())
    net = Path("/sys/class/net")
    if net.is_dir():
        for n in sorted(net.iterdir()):
            if n.name == "lo" or not (n / "device").exists():
                continue
            kind = "Wi-Fi" if (n / "wireless").exists() or (n / "phy80211").exists() else "wired"
            r.network.append(f"{n.name} ({kind})")
    for b in sorted(Path("/sys/block").glob("*")):
        if b.name.startswith(("loop", "ram", "zram", "sr", "dm-")):
            continue
        size = int(_read(b / "size", "0") or 0) * 512
        model = _read(b / "device" / "model")
        if size:
            r.disks.append(f"{b.name}: {model or 'disk'} ({human(size, binary=False)})")
    r.kernel = os.uname().release
    return r


def screen_size(drm: Path = Path("/sys/class/drm")) -> tuple[int, int] | None:
    """The preferred mode of the first connected display (e.g. the laptop's panel)."""
    for c in sorted(drm.glob("card*-*")):
        if _read(c / "status") != "connected":
            continue
        m = re.match(r"(\d+)x(\d+)", _read(c / "modes"))
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def has_gpu_acceleration(dri: Path = Path("/dev/dri")) -> bool:
    """A GPU driver with rendering is loaded (not just a plain framebuffer)."""
    return any(dri.glob("renderD*"))


def _gpus() -> list[str]:
    out = []
    text = _run(["lspci", "-nnk"])
    blocks = re.split(r"\n(?=\S)", text)
    for b in blocks:
        first = b.splitlines()[0] if b else ""
        if re.search(r"\b(VGA compatible controller|3D controller|Display controller)\b", first):
            name = first.split(": ", 1)[-1]
            drv = re.search(r"Kernel driver in use:\s*(\S+)", b)
            out.append(name + (f" [driver: {drv.group(1)}]" if drv else " [no driver]"))
    return out


def save(report: Report, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hardware.json").write_text(json.dumps(asdict(report), indent=1))
    (directory / "hardware.txt").write_text(report.to_text() + "\n")


# --- before each start -------------------------------------------------------------------

@dataclass
class Checks:
    notes: list[str] = field(default_factory=list)       # shown before the VM starts
    changed: bool = False


def start_checks(cfg: VMConfig, report: Report | None = None, data_free: int | None = None,
                 virtual_total: int | None = None) -> Checks:
    """Adapt ``cfg`` (in memory) to this computer and explain what is limited."""
    r = report or gather()
    c = Checks()
    if not r.kvm:
        if r.virtualization:
            c.notes.append("Virtualization is supported by this processor but not usable; "
                           "the VM runs much slower. Check the firmware settings.")
        else:
            c.notes.append("Virtualization (Intel VT-x / AMD-V) is turned off in this "
                           "computer's firmware settings, so the VM runs much slower. Turn it on "
                           "in the firmware setup (often under CPU, Advanced or Security).")
    usable = max(256, r.memory_mib - RESERVED_MIB) if r.memory_mib else 0
    if usable and cfg.machine.memory_mib > usable:
        c.notes.append(f"This computer has {human(r.memory_mib * 2**20)} of memory; the VM gets "
                       f"{human(usable * 2**20)} instead of {human(cfg.machine.memory_mib * 2**20)}.")
        cfg.machine.memory_mib = usable
        c.changed = True
    if cfg.machine.cpus > r.cpus:
        cfg.machine.cpus = r.cpus
        c.changed = True
    if data_free is not None and virtual_total is not None and virtual_total > data_free:
        c.notes.append(f"The VM's disks may grow to {human(virtual_total)}, but only "
                       f"{human(data_free)} is free on this drive.")
    return c
