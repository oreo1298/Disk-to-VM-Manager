"""Results of identifying what is installed on a set of disks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Volume:
    device: str                 # appliance device name, e.g. /dev/sda2
    fstype: str = ""
    label: str = ""
    size: int = 0
    part_type: str = ""         # MBR id (e.g. "0x07") or GPT type GUID
    bootable: bool = False      # MBR active flag
    encryption: str = ""        # bitlocker / luks / veracrypt / ""
    unlocked: bool = False


@dataclass
class DiskLayout:
    index: int                  # position in the drive list
    device: str                 # /dev/sda in the appliance
    scheme: str = ""            # gpt / msdos / none
    size: int = 0
    volumes: list[Volume] = field(default_factory=list)
    has_esp: bool = False
    has_bios_boot: bool = False
    mbr_loader: str = ""        # windows / grub / grub-legacy / lilo / syslinux / veracrypt / freebsd / dos / none


@dataclass
class DetectedOS:
    root: str                   # appliance device of the root/system volume ("" for DOS probes)
    family: str                 # windows / win9x / dos / linux / bsd / macos / other
    distro: str = ""            # windows / reactos / ubuntu / fedora / freebsd / freedos / msdos / win98 ...
    name: str = ""              # friendly name, e.g. "Windows 10 Pro 22H2"
    product_name: str = ""      # as reported by the OS
    version: str = ""           # NT version "10.0", distro version "24.04", DOS "6.22"
    build: str = ""             # Windows build number
    edition: str = ""           # client / server (Windows); EditionID in details
    arch: str = ""              # x86_64 / i386 / aarch64
    hostname: str = ""
    osinfo: str = ""            # libosinfo short id
    boot_mode: str = ""         # uefi / bios
    bootloader: str = ""
    disk_index: int = 0         # which source disk holds the root
    original_bus: str = ""      # storage controller the install booted from
    boot_drivers: dict = field(default_factory=dict)   # driver -> start value (Windows)
    kernel: str = ""            # Linux kernel version
    initramfs_tool: str = ""    # dracut / mkinitcpio / initramfs-tools / booster
    virtio_ready: bool | None = None
    rtc: str = ""               # utc / localtime as configured in the guest
    hibernated: bool = False
    fast_startup: bool = False
    encryption: list[str] = field(default_factory=list)
    hal: str = ""               # Windows 2000/XP HAL kind
    hypervisor_tools: list[str] = field(default_factory=list)
    gpu_drivers: list[str] = field(default_factory=list)
    mountpoints: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def facts(self) -> dict:
        """Facts used to select an OS profile."""
        f = {"family": self.family, "distro": self.distro, "version": self.version,
             "build": self.build, "edition": self.edition, "arch": self.arch,
             "name": self.name, "boot": self.boot_mode}
        if self.kernel:
            f["kernel"] = self.kernel
        if self.osinfo:
            f["osinfo_id"] = self.osinfo
        return {k: v for k, v in f.items() if v not in ("", None)}

    @property
    def label(self) -> str:
        return self.name or self.product_name or self.distro or self.family


@dataclass
class InspectionReport:
    disks: list[DiskLayout] = field(default_factory=list)
    oses: list[DetectedOS] = field(default_factory=list)
    encrypted_locked: list[str] = field(default_factory=list)  # devices we could not open
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    libguestfs_version: str = ""

    @property
    def primary(self) -> DetectedOS | None:
        return self.oses[0] if self.oses else None

    @property
    def multi_boot(self) -> bool:
        return len([o for o in self.oses if o.family != "other"]) > 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "InspectionReport":
        r = cls(problems=d.get("problems", []), notes=d.get("notes", []),
                encrypted_locked=d.get("encrypted_locked", []),
                libguestfs_version=d.get("libguestfs_version", ""))
        for dd in d.get("disks", []):
            vols = [Volume(**v) for v in dd.pop("volumes", [])]
            r.disks.append(DiskLayout(volumes=vols, **dd))
        for o in d.get("oses", []):
            r.oses.append(DetectedOS(**o))
        return r
