"""Validated changes to VM settings (used by `rehome set` and the GUI)."""

from __future__ import annotations

import os
from typing import Callable

from rehome.errors import RehomeError
from rehome.host import info as host_info
from rehome.util.units import mib, parse_size
from rehome.vm import config as C
from rehome.vm import disks, library, runner
from rehome.vm.config import CdRom, Disk, Nic, Share, VMConfig

# Fields that can change while the VM runs (they take effect immediately or
# do not affect the running QEMU process).
LIVE_FIELDS = {"name", "notes"}


def hardware_signature(cfg: VMConfig) -> dict:
    d = cfg.to_dict()
    for k in ("name", "notes", "snapshots", "current_snapshot", "import", "created"):
        d.pop(k, None)
    return d


def validate(cfg: VMConfig, host=None) -> list[str]:
    """Return a list of problems that must be fixed before saving."""
    host = host or host_info.gather()
    errs: list[str] = []
    m = cfg.machine
    if not cfg.name.strip():
        errs.append("The VM needs a name.")
    if library.name_taken(cfg.name, exclude_id=cfg.id):
        errs.append(f"Another VM is already called \"{cfg.name}\".")
    if m.memory_mib < 16:
        errs.append("Memory must be at least 16 MiB.")
    if host.memory.total_mib and m.memory_mib > host.memory.max_vm_mib:
        errs.append(f"Memory may be at most {host.memory.max_vm_mib} MiB on this computer.")
    if m.cpus < 1 or m.cpus > host.threads:
        errs.append(f"CPUs must be between 1 and {host.threads}.")
    if m.firmware not in C.FIRMWARE:
        errs.append(f"Unknown firmware \"{m.firmware}\".")
    if m.firmware == "uefi-secure" and m.type != "q35" and m.arch in ("x86_64", "i386"):
        errs.append("Secure Boot needs the Q35 machine type.")
    if m.tpm not in C.TPM:
        errs.append(f"Unknown TPM version \"{m.tpm}\".")
    if m.rtc not in C.RTC:
        errs.append("The clock must be UTC or local time.")
    ids = [d.id for d in cfg.disks] + [c.id for c in cfg.cdroms]
    if len(ids) != len(set(ids)):
        errs.append("Disk and CD drive IDs must be unique.")
    for d in cfg.disks:
        if d.bus not in C.DISK_BUSES:
            errs.append(f"{d.id}: unknown bus \"{d.bus}\".")
    for cd in cfg.cdroms:
        if cd.bus not in C.CD_BUSES:
            errs.append(f"{cd.id}: unknown bus \"{cd.bus}\".")
    for n in cfg.nics:
        if n.model not in C.NIC_MODELS:
            errs.append(f"{n.id}: unknown network card \"{n.model}\".")
        if n.mode not in C.NET_MODES:
            errs.append(f"{n.id}: unknown network mode \"{n.mode}\".")
        if n.mode == "bridge" and not n.bridge:
            errs.append(f"{n.id}: choose a bridge for bridged networking.")
    if cfg.display.device not in C.DISPLAY_DEVICES:
        errs.append(f"Unknown display device \"{cfg.display.device}\".")
    if cfg.sound.device not in C.SOUND_DEVICES:
        errs.append(f"Unknown sound device \"{cfg.sound.device}\".")
    if cfg.usb.controller not in C.USB_CONTROLLERS:
        errs.append(f"Unknown USB controller \"{cfg.usb.controller}\".")
    if cfg.input.pointer in ("usb-tablet", "usb-mouse") and cfg.usb.controller == "none":
        errs.append("A USB tablet needs a USB controller.")
    sata = sum(1 for d in cfg.disks if d.bus in ("sata",) or (d.bus == "ide" and m.type == "q35")) + \
        sum(1 for c in cfg.cdroms if c.bus in ("sata",) or (c.bus == "ide" and m.type == "q35"))
    if m.type == "q35" and sata > 6:
        errs.append("The Q35 SATA controller has 6 ports; move some disks to VirtIO or SCSI.")
    ide = sum(1 for d in cfg.disks if d.bus == "ide") + sum(1 for c in cfg.cdroms if c.bus == "ide")
    if m.type == "pc" and ide > 4:
        errs.append("The IDE controller has 4 slots; move some disks to SATA or VirtIO.")
    for s in cfg.shares:
        if not os.path.isdir(s.path):
            errs.append(f"The shared folder {s.path} does not exist.")
    return errs


def save_settings(new: VMConfig) -> VMConfig:
    """Persist edited settings after validation, respecting the VM's state."""
    current = library.reload(new)
    if runner.state(current).active and hardware_signature(new) != hardware_signature(current):
        raise RehomeError(f"\"{current.name}\" is running, so its hardware cannot be changed.",
                          fix="Shut the VM down, then change the settings.")
    problems = validate(new)
    if problems:
        raise RehomeError("The settings are not valid: " + " ".join(problems))
    with library.locked(new):
        fresh = library.reload(new)
        # Keep fields managed elsewhere (checkpoints) from the on-disk copy.
        new.snapshots, new.current_snapshot = fresh.snapshots, fresh.current_snapshot
        library.save(new)
    return new


def mutate(cfg: VMConfig, fn: Callable[[VMConfig], list[str] | None]) -> list[str]:
    new = VMConfig.from_dict(cfg.to_dict())
    new.path = cfg.path
    notes = fn(new) or []
    save_settings(new)
    return notes


def set_boot_first(cfg: VMConfig, device_id: str) -> None:
    devices = [*cfg.disks, *cfg.cdroms]
    if device_id not in {d.id for d in devices}:
        raise RehomeError(f"There is no device called {device_id}.")
    order = [d for d in devices if d.id == device_id] + \
        sorted([d for d in devices if d.id != device_id and d.boot_index],
               key=lambda d: d.boot_index)
    for d in devices:
        d.boot_index = 0
    for i, d in enumerate(order, start=1):
        d.boot_index = i


def _split(spec: str, sep: str = "=") -> tuple[str, str]:
    if sep not in spec:
        raise RehomeError(f"Expected KEY{sep}VALUE, got \"{spec}\".")
    a, b = spec.split(sep, 1)
    return a, b


def apply_cli_changes(cfg: VMConfig, args) -> list[str]:
    log: list[str] = []
    created_files: list[str] = []

    def change(new: VMConfig):
        m = new.machine
        if args.name:
            new.name = args.name
            log.append(f"name → {args.name}")
        if args.notes is not None:
            new.notes = args.notes
            log.append("notes updated")
        if args.memory:
            m.memory_mib = mib(args.memory)
            log.append(f"memory → {m.memory_mib} MiB")
        if args.cpus:
            m.cpus = args.cpus
            log.append(f"CPUs → {args.cpus}")
        if args.cpu_model:
            m.cpu_model = args.cpu_model
            log.append(f"CPU model → {args.cpu_model}")
        if args.firmware:
            m.firmware = args.firmware
            if args.firmware == "uefi-secure" and m.type == "pc":
                m.type = "q35"
            log.append(f"firmware → {args.firmware}")
        if args.tpm:
            m.tpm = args.tpm
            log.append(f"TPM → {args.tpm}")
        if args.machine:
            m.type = args.machine
            log.append(f"machine → {args.machine}")
        if args.accel:
            m.accel = args.accel
            log.append(f"accelerator → {args.accel}")
        if args.rtc:
            m.rtc = args.rtc
            log.append(f"clock → {args.rtc}")
        for spec in args.disk_bus:
            did, bus = _split(spec)
            new.disk(did).bus = bus
            log.append(f"{did} bus → {bus}")
        for spec in args.nic_model:
            nid, model = _split(spec)
            _nic(new, nid).model = model
            log.append(f"{nid} model → {model}")
        for spec in args.net:
            nid, mode = _split(spec)
            nic = _nic(new, nid, create=True)
            if mode.startswith("bridge"):
                nic.mode, nic.bridge = "bridge", mode.partition(":")[2]
            else:
                nic.mode = mode
            log.append(f"{nid} network → {mode}")
        for spec in args.forward:
            nid, fwd = _split(spec)
            _nic(new, nid).port_forwards.append(fwd)
            log.append(f"{nid} forwards {fwd}")
        if args.display:
            new.display.device = args.display
            log.append(f"display → {args.display}")
        if getattr(args, "console", None):
            new.display.console = args.console
            log.append(f"console → {args.console}")
        if args.gl:
            new.display.gl = args.gl == "on"
            log.append(f"3D acceleration → {args.gl}")
        if args.sound:
            new.sound.device = args.sound
            log.append(f"sound → {args.sound}")
        if args.usb:
            new.usb.controller = args.usb
            log.append(f"USB controller → {args.usb}")
        if args.pointer:
            new.input.pointer = args.pointer
            log.append(f"pointer → {args.pointer}")
        for spec in args.add_disk:
            size_s, _, bus = spec.partition(":")
            did = new.next_id("disk", new.disks)
            path = f"disks/{did}.qcow2"
            disks.create(new.resolve(path), parse_size(size_s))
            created_files.append(str(new.resolve(path)))
            new.disks.append(Disk(id=did, file=path, bus=bus or (new.disks[0].bus if new.disks else "virtio")))
            log.append(f"added {did} ({size_s})")
        for spec in args.attach_disk:
            img, bus = spec, ""
            if ":" in spec and spec.rsplit(":", 1)[1] in C.DISK_BUSES:
                img, bus = spec.rsplit(":", 1)
            img = os.path.abspath(img)
            if not os.path.exists(img):
                raise RehomeError(f"{img} does not exist.")
            fmt = disks.info(img).get("format", "raw")
            did = new.next_id("disk", new.disks)
            new.disks.append(Disk(id=did, file=img, format=fmt, bus=bus or "virtio",
                                  discard=fmt != "raw"))
            log.append(f"attached {img} as {did}")
        for did in args.remove_disk:
            d = new.disk(did)
            new.disks.remove(d)
            log.append(f"removed {did} (the file {d.file} was kept)")
        for spec in args.resize_disk:
            did, size_s = _split(spec)
            d = new.disk(did)
            disks.resize(new.resolve(d.file), parse_size(size_s))
            log.append(f"{did} resized to {size_s}")
        for iso in args.add_cd:
            cid = new.next_id("cd", new.cdroms)
            new.cdroms.append(CdRom(id=cid, file=os.path.abspath(iso) if iso else "",
                                    bus="ide" if m.type == "pc" else "sata"))
            log.append(f"added CD drive {cid}")
        for cid in args.remove_cd:
            new.cdroms = [c for c in new.cdroms if c.id != cid]
            log.append(f"removed CD drive {cid}")
        for spec in args.add_share:
            path, _, tag = spec.partition(":")
            tag = tag or os.path.basename(os.path.abspath(path)) or "share"
            new.shares.append(Share(tag=tag, path=os.path.abspath(path)))
            log.append(f"shared {path} as \"{tag}\"")
        for tag in args.remove_share:
            new.shares = [s for s in new.shares if s.tag != tag]
            log.append(f"removed share {tag}")
        if args.boot:
            set_boot_first(new, args.boot)
            log.append(f"boots from {args.boot} first")

    try:
        mutate(cfg, change)
    except BaseException:
        for f in created_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        raise
    return log


def _nic(cfg: VMConfig, nid: str, create: bool = False) -> Nic:
    for n in cfg.nics:
        if n.id == nid:
            return n
    if create:
        n = Nic(id=nid, model=cfg.nics[0].model if cfg.nics else "virtio-net-pci")
        cfg.nics.append(n)
        return n
    raise RehomeError(f"There is no network card called {nid}.")
