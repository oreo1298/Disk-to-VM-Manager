"""Turn a profile plus host facts into concrete virtual hardware."""

from __future__ import annotations

from dataclasses import dataclass

from rehome.host import info as host_info
from rehome.patch.profiles import Profile
from rehome.util.units import parse_size
from rehome.vm.config import CdRom, Nic, VMConfig

# A bus the guest can boot from without new drivers, for each original controller.
ORIGINAL_TO_EMULATED = {
    "nvme": "nvme", "sata": "sata", "ahci": "sata", "ide": "ide", "pata": "ide",
    "virtio": "virtio", "scsi": "scsi", "usb": "sata", "mmc": "sata", "raid": "sata",
}


@dataclass
class HardwareContext:
    fresh: bool                      # new install (True) or import (False)
    boot_mode: str = ""              # "uefi" / "bios" detected on the source ("" = unknown)
    original_bus: str = ""           # storage controller the OS was installed on
    virtio_ready: bool = False       # the guest already has virtio storage drivers
    paravirt: bool = False


def resolve_disk_bus(profile: Profile, ctx: HardwareContext, machine: str) -> str:
    want = profile.hw("disk_bus", "auto", fresh=ctx.fresh, paravirt=ctx.paravirt)
    if want != "auto":
        bus = want
    elif ctx.virtio_ready:
        bus = "virtio"
    elif ctx.original_bus:
        bus = ORIGINAL_TO_EMULATED.get(ctx.original_bus, "sata")
    else:
        bus = "sata"
    if bus == "ide" and machine == "q35":
        bus = "sata"
    if bus == "sata" and machine == "pc" and profile.hw("disk_bus") == "ide":
        bus = "ide"
    return bus


def resolve_firmware(profile: Profile, ctx: HardwareContext, facts: dict) -> str:
    fw = profile.hw("firmware", "auto", fresh=ctx.fresh)
    if fw != "auto":
        if not ctx.fresh and ctx.boot_mode == "bios" and fw.startswith("uefi"):
            # Never change how an installed system boots: a BIOS/MBR install
            # cannot boot from UEFI firmware.
            return "bios"
        if not ctx.fresh and ctx.boot_mode == "uefi" and fw == "bios":
            return "uefi"
        return fw
    if ctx.boot_mode:
        return "uefi" if ctx.boot_mode == "uefi" else "bios"
    if facts.get("efi") is True:
        return "uefi"
    return "bios"


def apply_profile(cfg: VMConfig, profile: Profile, facts: dict, ctx: HardwareContext,
                  host=None, memory_mib: int | None = None, cpus: int | None = None) -> list[str]:
    """Fill ``cfg`` from ``profile``. Returns notes for the user."""
    host = host or host_info.gather()
    notes: list[str] = list(profile.notes)
    hw = lambda k, d=None: profile.hw(k, d, fresh=ctx.fresh, paravirt=ctx.paravirt)  # noqa: E731
    m = cfg.machine
    arch = facts.get("arch") or "x86_64"
    m.arch = "i386" if arch in ("i386", "i686", "x86") else arch
    m.type = host_info.choose_machine(host, m.arch, hw("machine", "q35"))
    m.cpu_model = hw("cpu_model", "host")
    m.cpu_flags = list(hw("cpu_flags", []))
    m.accel = hw("accel", "auto")
    m.accel_reason = hw("accel_reason", "")
    m.cpu_quota_pct = int(hw("cpu_quota_pct", 0))
    m.icount = hw("icount", "")
    max_cpus = int(hw("max_cpus", 64))
    m.cpus = min(cpus or host_info.default_cpus(host, int(hw("cpus", 2))), max_cpus, host.threads)
    want_mem = memory_mib or int(hw("memory_mib", 2048))
    lo = int(hw("min_memory_mib", 0))
    hi = int(hw("max_memory_mib", 1 << 30))
    mem = max(want_mem, lo)
    if mem > hi:
        notes.append(f"Memory limited to {hi} MiB for this operating system.")
        mem = hi
    if host.memory.total_mib and mem > host.memory.max_vm_mib:
        notes.append(f"Memory reduced to {host.memory.max_vm_mib} MiB to leave room for this computer.")
        mem = host.memory.max_vm_mib
    m.memory_mib = mem
    m.firmware = resolve_firmware(profile, ctx, facts)
    m.tpm = hw("tpm", "none")
    if m.firmware == "uefi-secure" and m.type != "q35" and m.arch in ("x86_64", "i386"):
        m.type = "q35"
    m.rtc = hw("rtc", "utc")
    m.hyperv = bool(hw("hyperv", False))
    m.hpet = bool(hw("hpet", True))
    m.acpi = bool(hw("acpi", True))
    cfg.display.device = hw("display", "virtio-vga")
    cfg.display.vram_mib = int(hw("vram_mib", 0))
    cfg.sound.device = hw("sound", "ich9-intel-hda")
    cfg.usb.controller = hw("usb", "qemu-xhci")
    cfg.input.pointer = hw("pointer", "usb-tablet")
    cfg.input.keyboard = hw("keyboard", "ps2")
    cfg.guest.agent = bool(hw("agent", True))
    cfg.guest.vdagent = bool(hw("vdagent", True))
    bus = resolve_disk_bus(profile, ctx, m.type)
    for d in cfg.disks:
        d.bus = bus
    cd_bus = hw("cdrom_bus", "sata")
    if cd_bus == "ide" and m.type == "q35":
        cd_bus = "sata"
    for c in cfg.cdroms:
        c.bus = cd_bus
    nic_model = hw("nic", "e1000")
    if not cfg.nics:
        cfg.nics.append(Nic(id="net0", model=nic_model))
    else:
        for n in cfg.nics:
            n.model = nic_model
    cfg.profile = profile.id
    if m.accel == "tcg" and m.accel_reason:
        notes.append(m.accel_reason)
    return notes


def default_disk_size(profile: Profile, facts: dict, osinfo_storage: int = 0) -> int:
    size = parse_size(profile.hw("disk_size", "32G", fresh=True))
    if osinfo_storage and osinfo_storage > size:
        size = osinfo_storage
    return size


def add_cdrom(cfg: VMConfig, iso: str = "", boot_index: int = 0, bus: str | None = None) -> CdRom:
    cd = CdRom(id=cfg.next_id("cd", cfg.cdroms), file=iso, boot_index=boot_index,
               bus=bus or ("ide" if cfg.machine.type == "pc" else "sata"))
    cfg.cdroms.append(cd)
    return cd
