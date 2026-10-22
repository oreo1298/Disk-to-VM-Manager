"""Create new VMs (the "New Virtual Machine" wizard and `rehome create`)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from rehome.errors import RehomeError
from rehome.host import info as host_info
from rehome.patch import profiles
from rehome.vm import disks, library
from rehome.vm import storage as vmstorage
from rehome.vm.config import Disk, Floppy, Nic, OsInfo, Share, VMConfig
from rehome.vm.hardware import HardwareContext, add_cdrom, apply_profile, default_disk_size


@dataclass
class NewVMSpec:
    name: str = ""
    iso: str = ""                    # installer ISO (optional)
    os: str = ""                     # libosinfo short id ("win11"), profile id, or "" = detect
    memory_mib: int | None = None
    cpus: int | None = None
    disk_size: int | None = None     # bytes; 0 = no disk
    disk_bus: str | None = None
    firmware: str | None = None      # bios / uefi / uefi-secure
    tpm: str | None = None
    network: str = "nat"             # nat / bridge / none
    bridge: str = ""
    nic_model: str | None = None
    shares: list[str] = field(default_factory=list)   # host paths
    extra_isos: list[str] = field(default_factory=list)
    floppy: str = ""
    existing_disk: str = ""          # attach an existing image instead of a new one
    disk_dir: str | None = None      # folder for the new disk: None = Preferences, "" = the VM's folder
    notes: str = ""


@dataclass
class CreatePlan:
    config: VMConfig
    disk_size: int
    os_label: str
    facts: dict
    notes: list[str]


def _facts_for_spec(spec: NewVMSpec) -> tuple[dict, str, int, str]:
    """Work out OS facts (and a forced profile, if any) from the ISO or an explicit choice."""
    from rehome.media import osinfo
    storage = 0
    if spec.os:
        if osinfo.available():
            e = osinfo.by_short_id(spec.os) or osinfo.KNOWN_EXTRA.get(spec.os)
            if e:
                return e.facts(), e.name, e.storage_bytes, ""
        try:
            prof = profiles.get(spec.os)
            facts = {k: (v[0] if isinstance(v, list) else v) for k, v in prof.match.items()
                     if not k.endswith(("_min", "_max", "_regex"))}
            facts["name"] = prof.name
            return facts, prof.name, 0, prof.id
        except KeyError:
            pass
        raise RehomeError(f"Unknown operating system \"{spec.os}\".",
                          fix="Use a libosinfo short ID such as win11, ubuntu24.04 or freedos1.3 "
                              "(see `rehome os-list`), or leave it out to detect it from the ISO.")
    if spec.iso:
        mi = osinfo.identify_media(spec.iso)
        if mi.os:
            facts = mi.os.facts()
            if mi.arch:
                facts["arch"] = {"i686": "i386", "i386": "i386"}.get(mi.arch, mi.arch)
            return facts, mi.os.name, mi.os.storage_bytes, ""
    return {"family": "other", "name": "Unknown OS"}, "Unknown operating system", storage, ""


def plan(spec: NewVMSpec, host=None) -> CreatePlan:
    host = host or host_info.gather()
    if spec.iso and not os.path.isfile(spec.iso):
        raise RehomeError(f"The installer image {spec.iso} does not exist.")
    if spec.disk_dir:
        vmstorage.check_folder(spec.disk_dir)
    facts, label, storage, forced = _facts_for_spec(spec)
    prof = profiles.get(forced) if forced else profiles.select(facts)
    if not prof.supported:
        raise RehomeError(" ".join(prof.notes) or f"{prof.name} is not supported.")
    cfg = VMConfig(name=spec.name or label)
    cfg.os = OsInfo(family=facts.get("family", "other"), name=label,
                    version=str(facts.get("version", "")), distro=facts.get("distro", ""),
                    arch=facts.get("arch", "x86_64"), osinfo_id=facts.get("osinfo_id", ""),
                    build=str(facts.get("build", "") or ""), edition=facts.get("edition", ""))
    size = spec.disk_size if spec.disk_size is not None else default_disk_size(prof, facts, storage)
    if spec.existing_disk:
        d = vmstorage.image_disk(spec.existing_disk, "disk0", "virtio", cfg)
        d.boot_index = 2 if spec.iso else 1
        cfg.disks.append(d)
    elif size:
        cfg.disks.append(Disk(id="disk0", file="disks/disk0.qcow2",
                              boot_index=2 if spec.iso else 1))
    if spec.iso:
        add_cdrom(cfg, spec.iso, boot_index=1)
    for extra in spec.extra_isos:
        add_cdrom(cfg, extra)
    if spec.floppy:
        cfg.floppies.append(Floppy(id="fd0", file=spec.floppy))
    if spec.network != "none":
        cfg.nics.append(Nic(id="net0", mode=spec.network, bridge=spec.bridge))
    ctx = HardwareContext(fresh=True)
    notes = apply_profile(cfg, prof, facts, ctx, host=host, memory_mib=spec.memory_mib,
                          cpus=spec.cpus)
    if spec.network == "none":
        cfg.nics = []              # the profile adds a default NIC
    m = cfg.machine
    if spec.firmware:
        m.firmware = spec.firmware
        if spec.firmware == "uefi-secure" and m.type == "pc":
            m.type = "q35"
    if spec.tpm:
        m.tpm = spec.tpm
    if spec.disk_bus:
        for d in cfg.disks:
            d.bus = spec.disk_bus
    if spec.nic_model:
        for n in cfg.nics:
            n.model = spec.nic_model
    for i, path in enumerate(spec.shares):
        cfg.shares.append(Share(tag=f"share{i}", path=os.path.abspath(path)))
    cfg.notes = spec.notes
    _validate(cfg, host)
    if not cfg.disks and not cfg.cdroms:
        notes.append("This VM has no disk and no installer; add one in the settings before starting it.")
    return CreatePlan(config=cfg, disk_size=size, os_label=label, facts=facts, notes=notes)


def _validate(cfg: VMConfig, host) -> None:
    m = cfg.machine
    if m.memory_mib < 16:
        raise RehomeError("A VM needs at least 16 MiB of memory.")
    if host.memory.total_mib and m.memory_mib > host.memory.max_vm_mib:
        raise RehomeError(
            f"{m.memory_mib} MiB is more memory than this computer can spare "
            f"(at most {host.memory.max_vm_mib} MiB of {host.memory.total_mib} MiB).",
            fix=f"Choose {host.memory.max_vm_mib} MiB or less.")
    if m.cpus > host.threads:
        raise RehomeError(f"This computer has {host.threads} CPU threads; a VM cannot have {m.cpus}.",
                          fix=f"Choose {host.threads} or fewer CPUs.")
    if m.tpm != "none" and m.firmware == "bios" and cfg.os.family == "windows" \
            and cfg.os.build and int(cfg.os.build) >= 22000:
        raise RehomeError("Windows 11 needs UEFI firmware.", fix="Choose UEFI with Secure Boot.")


def create(spec: NewVMSpec, host=None) -> VMConfig:
    p = plan(spec, host)
    cfg = p.config
    cfg.name = library.unique_name(cfg.name)
    library.create_dir(cfg)
    made = []
    try:
        for d in cfg.disks:
            if d.file == "disks/disk0.qcow2":          # the new disk planned above
                d.file, d.owned = vmstorage.new_disk_path(cfg, d.id, spec.disk_dir)
                made.append(cfg.resolve(d.file))
                disks.create(made[-1], p.disk_size)
        library.save(cfg)
    except BaseException:
        import shutil
        shutil.rmtree(cfg.dir, ignore_errors=True)
        for f in made:
            f.unlink(missing_ok=True)
        raise
    return cfg
