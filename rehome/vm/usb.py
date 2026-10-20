"""USB pass-through ("USB redirection") to running VMs.

The privileged helper opens /dev/bus/usb/BBB/DDD and hands the fd over;
QEMU receives it with QMP ``add-fd`` and uses it via
``usb-host,hostdevice=/dev/fdset/N``. No root, no udev rule changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rehome.errors import RehomeError
from rehome.vm import runner
from rehome.vm.config import VMConfig

SYS_USB = Path("/sys/bus/usb/devices")


@dataclass
class UsbHostDevice:
    bus: int
    devnum: int
    vendor_id: str
    product_id: str
    manufacturer: str = ""
    product: str = ""
    device_class: str = ""
    speed: str = ""

    @property
    def key(self) -> str:
        return f"{self.bus:03d}-{self.devnum:03d}"

    @property
    def qom_id(self) -> str:
        return f"usbhost-{self.bus}-{self.devnum}"

    @property
    def label(self) -> str:
        name = " ".join(x for x in (self.manufacturer, self.product) if x)
        return name or f"USB device {self.vendor_id}:{self.product_id}"


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def list_host_devices() -> list[UsbHostDevice]:
    out = []
    if not SYS_USB.is_dir():
        return out
    for d in sorted(SYS_USB.iterdir()):
        if ":" in d.name or not (d / "devnum").exists():
            continue
        cls = _read(d / "bDeviceClass")
        if cls == "09":          # hubs (including root hubs)
            continue
        try:
            dev = UsbHostDevice(bus=int(_read(d / "busnum")), devnum=int(_read(d / "devnum")),
                                vendor_id=_read(d / "idVendor"), product_id=_read(d / "idProduct"),
                                manufacturer=_read(d / "manufacturer"), product=_read(d / "product"),
                                device_class=cls, speed=_read(d / "speed"))
        except ValueError:
            continue
        out.append(dev)
    return out


def attached(cfg: VMConfig) -> set[str]:
    """qom ids of host USB devices attached to the running VM."""
    if not runner.is_running(cfg):
        return set()
    with runner.qmp(cfg) as q:
        try:
            items = q.execute("qom-list", {"path": "/machine/peripheral"})
        except Exception:
            return set()
    return {i["name"] for i in items if i["name"].startswith("usbhost-")}


def attach(cfg: VMConfig, dev: UsbHostDevice, fd_provider=None) -> None:
    if cfg.usb.controller == "none":
        raise RehomeError("This VM has no USB controller.",
                          fix="Turn the VM off and choose a USB controller in its settings.")
    if fd_provider is None:
        from rehome.disk import privileged
        fd_provider = privileged.open_usb
    fd = fd_provider(dev.bus, dev.devnum)
    try:
        with runner.qmp(cfg) as q:
            fdset = q.add_fd(fd, opaque=dev.qom_id)
            try:
                q.execute("device_add", {"driver": "usb-host", "id": dev.qom_id,
                                         "hostdevice": f"/dev/fdset/{fdset}", "bus": "usb.0"})
            except Exception as e:
                q.execute("remove-fd", {"fdset-id": fdset})
                raise RehomeError(f"{dev.label} could not be connected to the VM.",
                                  details=str(e)) from e
    finally:
        os.close(fd)


def detach(cfg: VMConfig, dev: UsbHostDevice) -> None:
    with runner.qmp(cfg) as q:
        q.execute("device_del", {"id": dev.qom_id})
        try:
            for fs in q.execute("query-fdsets"):
                if any(f.get("opaque") == dev.qom_id for f in fs.get("fds", [])):
                    q.execute("remove-fd", {"fdset-id": fs["fdset-id"]})
        except Exception:
            pass
