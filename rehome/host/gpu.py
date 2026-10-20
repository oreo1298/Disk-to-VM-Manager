"""Host GPU enumeration and 3D acceleration (virgl / venus) usability."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PCI_VENDORS = {
    "0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA", "0x1af4": "Red Hat (virtio)",
    "0x15ad": "VMware", "0x80ee": "VirtualBox", "0x1234": "QEMU", "0x1b36": "Red Hat (QEMU)",
    "0x5143": "Qualcomm", "0x13b5": "ARM", "0x1414": "Microsoft (Hyper-V)",
}


@dataclass
class Gpu:
    card: str               # card0
    vendor_id: str
    device_id: str
    driver: str
    pci_address: str = ""
    name: str = ""
    render_node: str = ""
    render_node_access: bool = False
    boot_vga: bool = False

    @property
    def vendor(self) -> str:
        return PCI_VENDORS.get(self.vendor_id, self.vendor_id)

    @property
    def label(self) -> str:
        return self.name or f"{self.vendor} GPU ({self.vendor_id}:{self.device_id})"


@dataclass
class GpuReport:
    gpus: list[Gpu] = field(default_factory=list)
    egl_ok: bool | None = None       # None = not tested
    egl_error: str = ""


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def enumerate_gpus(sys_root: str = "/sys", dev_root: str = "/dev") -> list[Gpu]:
    gpus = []
    drm = Path(sys_root, "class/drm")
    if not drm.is_dir():
        return gpus
    for card in sorted(drm.glob("card[0-9]*")):
        if "-" in card.name:
            continue  # connector, e.g. card0-HDMI-A-1
        dev = card / "device"
        drv = ""
        try:
            drv = os.path.basename(os.readlink(dev / "driver"))
        except OSError:
            pass
        addr = ""
        try:
            addr = os.path.basename(os.path.realpath(dev))
        except OSError:
            pass
        g = Gpu(card=card.name, vendor_id=_read(dev / "vendor"),
                device_id=_read(dev / "device"), driver=drv, pci_address=addr,
                boot_vga=_read(dev / "boot_vga") == "1")
        for rn in sorted((dev / "drm").glob("renderD*")) if (dev / "drm").is_dir() else []:
            node = os.path.join(dev_root, "dri", rn.name)
            g.render_node = node
            g.render_node_access = os.access(node, os.R_OK | os.W_OK)
            break
        g.name = _lspci_name(addr)
        gpus.append(g)
    return gpus


def _lspci_name(addr: str) -> str:
    if not addr or ":" not in addr:
        return ""
    try:
        out = subprocess.run(["lspci", "-mm", "-s", addr], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # Format: 00:02.0 "VGA compatible controller" "Intel Corporation" "UHD Graphics 620" ...
    parts = [p for p in out.split('"') if p.strip()]
    if len(parts) >= 4:
        return f"{parts[2]} {parts[3]}".strip()
    return ""


def probe_egl(qemu_binary: str, render_node: str = "", timeout: float = 15) -> tuple[bool, str]:
    """Check that QEMU can initialise EGL on a render node (needed for virgl)."""
    disp = "egl-headless" + (f",rendernode={render_node}" if render_node else "")
    try:
        cp = subprocess.run(
            [qemu_binary, "-machine", "none", "-nodefaults", "-display", disp,
             "-S", "-qmp", "stdio"],
            input='{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n',
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    err = (cp.stderr or "").strip()
    if "egl" in err.lower() and ("fail" in err.lower() or "no drm" in err.lower()):
        return False, err
    if cp.returncode != 0:
        return False, err or f"exit status {cp.returncode}"
    return True, ""
