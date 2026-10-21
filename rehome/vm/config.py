"""VM configuration model, persisted as ``vm.toml`` in the VM's directory."""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
import types
import typing
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 1

# --- enumerations (kept as plain strings in the file) ------------------------

DISK_BUSES = ("virtio", "scsi", "sata", "ide", "nvme", "usb", "floppy")
CD_BUSES = ("sata", "ide", "scsi", "usb")
NIC_MODELS = ("virtio-net-pci", "e1000e", "e1000", "rtl8139", "pcnet", "ne2k_pci", "ne2k_isa",
              "usb-net", "vmxnet3")
NET_MODES = ("nat", "bridge", "none")
DISPLAY_DEVICES = ("virtio-vga", "virtio-gpu-pci", "qxl-vga", "VGA", "cirrus-vga",
                   "vmware-svga", "bochs-display", "ramfb", "none")
SOUND_DEVICES = ("none", "ich9-intel-hda", "intel-hda", "AC97", "sb16", "ES1370", "usb-audio")
USB_CONTROLLERS = ("none", "qemu-xhci", "usb-ehci", "ich9-usb-ehci1", "piix3-usb-uhci",
                   "piix4-usb-uhci", "nec-usb-xhci")
POINTERS = ("usb-tablet", "virtio-tablet", "ps2", "usb-mouse")
FIRMWARE = ("bios", "uefi", "uefi-secure")
TPM = ("none", "2.0", "1.2")
RTC = ("utc", "localtime")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def random_mac() -> str:
    # 52:54:00 is QEMU's OUI; keep the locally-administered prefix QEMU uses.
    return "52:54:00:%02x:%02x:%02x" % (random.randint(0, 255), random.randint(0, 255),
                                         random.randint(0, 255))


@dataclass
class DiskSource:
    """Where an imported disk's data really lives (never written to)."""
    kind: str = "image"          # image / block / ewf
    path: str = ""               # image path or stable /dev/disk/by-id path
    format: str = "raw"          # raw / qcow2 / vmdk / vhdx / vpc / vdi / ewf
    mode: str = "linked"         # linked / copy
    size: int = 0                # virtual size in bytes
    label: str = ""              # e.g. "Samsung SSD 860 EVO 500GB (S3Z...)"
    serial: str = ""
    original_bus: str = ""       # sata / nvme / usb / ide / virtio / scsi (as on the host)
    sha256_before: str = ""
    sha256_after: str = ""


@dataclass
class Disk:
    id: str = "disk0"
    file: str = ""               # qcow2 file (relative paths are relative to the VM dir)
    format: str = "qcow2"
    bus: str = "virtio"
    boot_index: int = 0          # 0 = not bootable
    readonly: bool = False
    cache: str = "writeback"     # writeback / none / unsafe
    discard: bool = True
    serial: str = ""
    source: DiskSource | None = None


@dataclass
class CdRom:
    id: str = "cd0"
    file: str = ""               # ISO path; "" = empty drive
    bus: str = "sata"
    boot_index: int = 0


@dataclass
class Floppy:
    id: str = "fd0"
    file: str = ""


@dataclass
class Nic:
    id: str = "net0"
    model: str = "virtio-net-pci"
    mode: str = "nat"
    bridge: str = ""
    mac: str = field(default_factory=random_mac)
    port_forwards: list[str] = field(default_factory=list)   # "tcp:2222:22"


@dataclass
class Share:
    tag: str = "share"
    path: str = ""
    readonly: bool = False


@dataclass
class UsbDevice:
    vendor_id: str = ""          # "046d"
    product_id: str = ""         # "c52b"
    label: str = ""


@dataclass
class Snapshot:
    id: str = ""                 # tag inside the qcow2 files
    name: str = ""
    description: str = ""
    parent: str = ""             # id of parent snapshot ("" = root)
    created: str = ""
    has_state: bool = False      # includes RAM (taken while running)
    kind: str = "user"           # user / pre-patch / pre-tools


@dataclass
class OsInfo:
    family: str = "other"        # windows / win9x / dos / linux / bsd / macos / other
    name: str = ""               # human readable, e.g. "Windows 10 Pro 22H2"
    version: str = ""            # e.g. "10.0", "6.1", "22.04"
    distro: str = ""             # ubuntu / fedora / freebsd / reactos / freedos / msdos ...
    arch: str = "x86_64"
    osinfo_id: str = ""          # libosinfo short id / URI
    build: str = ""
    edition: str = ""


@dataclass
class Machine:
    arch: str = "x86_64"         # guest architecture
    type: str = "q35"            # q35 / pc / virt / isapc
    accel: str = "auto"          # auto / kvm / tcg
    accel_reason: str = ""       # why the profile prefers TCG, shown to users
    cpu_model: str = "host"      # host / max / qemu64 / pentium3 ...
    cpu_flags: list[str] = field(default_factory=list)   # e.g. ["-x2apic"]
    cpus: int = 2
    memory_mib: int = 2048
    firmware: str = "bios"
    tpm: str = "none"
    rtc: str = "utc"
    rtc_catchup: bool = False    # re-inject lost RTC ticks (Windows under KVM)
    hyperv: bool = False         # Hyper-V enlightenments for Windows guests (KVM only)
    hpet: bool = True
    acpi: bool = True
    smbios_vendor: str = ""
    cpu_quota_pct: int = 0       # cgroup CPU cap (0 = none); for guests that never idle
    icount: str = ""             # -icount value for timing-sensitive legacy guests


@dataclass
class Display:
    device: str = "virtio-vga"
    vram_mib: int = 0            # 0 = device default
    gl: bool = False             # virgl 3D acceleration
    console: str = "dbus"        # dbus / spice / vnc / none / kiosk (appliances)


@dataclass
class Sound:
    device: str = "ich9-intel-hda"
    backend: str = "auto"        # auto / pipewire / pa / alsa / none


@dataclass
class Input:
    pointer: str = "usb-tablet"
    keyboard: str = "ps2"        # ps2 / usb / virtio


@dataclass
class Usb:
    controller: str = "qemu-xhci"
    devices: list[UsbDevice] = field(default_factory=list)


@dataclass
class Guest:
    agent: bool = True           # qemu-guest-agent channel
    vdagent: bool = True         # clipboard via qemu-vdagent (spice-vdagent in the guest)
    tools_installed: bool = False
    paravirt: bool = False       # switched to virtio devices after tools install


@dataclass
class ImportInfo:
    created: str = ""
    mode: str = "linked"
    profile: str = ""
    inspection: str = ""         # path (relative) to inspection.json
    patched: bool = False
    patch_snapshot: str = ""     # snapshot id taken before patching
    first_boot_ok: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class DirectBoot:
    """Boot a kernel/initrd directly (used by unattended Linux installs)."""
    kernel: str = ""
    initrd: str = ""
    append: str = ""
    once: bool = True            # removed automatically after the next start


@dataclass
class Passthrough:
    """Reserved for GPU/PCI pass-through (not implemented in v1)."""
    pci: list[str] = field(default_factory=list)   # host PCI addresses, e.g. "0000:01:00.0"


@dataclass
class VMConfig:
    name: str = "New VM"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema: int = SCHEMA
    created: str = field(default_factory=now_iso)
    notes: str = ""
    profile: str = ""
    os: OsInfo = field(default_factory=OsInfo)
    machine: Machine = field(default_factory=Machine)
    display: Display = field(default_factory=Display)
    sound: Sound = field(default_factory=Sound)
    input: Input = field(default_factory=Input)
    usb: Usb = field(default_factory=Usb)
    guest: Guest = field(default_factory=Guest)
    disks: list[Disk] = field(default_factory=list)
    cdroms: list[CdRom] = field(default_factory=list)
    floppies: list[Floppy] = field(default_factory=list)
    nics: list[Nic] = field(default_factory=list)
    shares: list[Share] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    current_snapshot: str = ""
    import_info: ImportInfo | None = None
    direct_boot: DirectBoot | None = None
    passthrough: Passthrough = field(default_factory=Passthrough)

    # Set by the library when loaded; not persisted.
    path: Path | None = field(default=None, compare=False, repr=False,
                              metadata={"persist": False})

    @property
    def dir(self) -> Path:
        if self.path is None:
            raise ValueError("VM has no directory yet")
        return self.path

    @property
    def short_id(self) -> str:
        return self.id.split("-")[0]

    def resolve(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.dir / q

    def disk(self, disk_id: str) -> Disk:
        for d in self.disks:
            if d.id == disk_id:
                return d
        raise KeyError(disk_id)

    def snapshot(self, snap_id: str) -> Snapshot:
        for s in self.snapshots:
            if s.id == snap_id or s.name == snap_id:
                return s
        raise KeyError(snap_id)

    def next_id(self, prefix: str, items) -> str:
        used = {i.id for i in items}
        n = 0
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"

    # --- (de)serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        d = _to_plain(self)
        d["import"] = d.pop("import_info", None)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "VMConfig":
        data = dict(data)
        if "import" in data:
            data["import_info"] = data.pop("import")
        schema = data.get("schema", SCHEMA)
        if schema > SCHEMA:
            raise ValueError(f"vm.toml schema {schema} is newer than this Rehome supports")
        return _from_plain(cls, data)


def _to_plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        out = {}
        for f in dataclasses.fields(obj):
            if not f.metadata.get("persist", True):
                continue
            v = _to_plain(getattr(obj, f.name))
            if v is None:
                continue
            out[f.name] = v
        return out
    if isinstance(obj, list):
        return [_to_plain(x) for x in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


_HINTS: dict[type, dict] = {}


def _hints(cls) -> dict:
    if cls not in _HINTS:
        _HINTS[cls] = typing.get_type_hints(cls)
    return _HINTS[cls]


def _from_plain(tp: Any, value: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if value is None:
            return None
        return _from_plain(args[0], value)
    if origin is list:
        (item_tp,) = typing.get_args(tp)
        return [_from_plain(item_tp, v) for v in (value or [])]
    if dataclasses.is_dataclass(tp) and isinstance(value, dict):
        hints = _hints(tp)
        kwargs = {}
        names = {f.name for f in dataclasses.fields(tp) if f.metadata.get("persist", True)}
        for k, v in value.items():
            if k in names:
                kwargs[k] = _from_plain(hints[k], v)
        return tp(**kwargs)
    return value
