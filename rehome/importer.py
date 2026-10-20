"""Disk-to-VM import: analyse sources, show what was found, then build the VM.

    analysis = analyze(["/dev/sdb"])                  # read-only, no VM yet
    # ... show analysis.summary(), let the user override ...
    cfg = run(analysis, ImportOptions(mode="linked"), progress)

The source is never written: inspection attaches it read-only, and the VM's
writable layer is a qcow2 overlay (or a standalone copy).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field

from rehome import jobs
from rehome.disk import hashing, overlay, smart
from rehome.disk import sources as srcmod
from rehome.disk.sources import Source
from rehome.errors import Cancelled, RehomeError, UnsafeSource
from rehome.host import info as host_info
from rehome.inspect.engine import inspect
from rehome.inspect.gfs import Drive
from rehome.inspect.model import DetectedOS, InspectionReport
from rehome.patch import profiles
from rehome.patch.profiles import Profile
from rehome.vm import library
from rehome.vm.config import ImportInfo, OsInfo, VMConfig, now_iso
from rehome.vm.hardware import HardwareContext, add_cdrom, apply_profile

IMPORT_STAGES = ["Checking the source", "Hashing the source", "Creating the VM's disk",
                 "Identifying the system", "Applying fixes", "Verifying the source"]


@dataclass
class ImportOptions:
    name: str = ""
    mode: str = "linked"                 # linked / copy
    verify_hash: bool = False
    keys: list[str] = field(default_factory=list)   # BitLocker recovery keys / LUKS passphrases
    os_index: int = 0                    # which detected OS to boot (multi-boot disks)
    overrides: dict = field(default_factory=dict)   # profile, firmware, disk_bus, memory_mib, cpus, family...
    discard_hibernation: bool | None = None
    patch: bool = True
    allow_failing_disk: bool = False
    smart_check: bool = True


@dataclass
class Analysis:
    sources: list[Source]
    report: InspectionReport
    health: dict = field(default_factory=dict)          # source path -> smart.Health
    os_index: int = 0
    profile: Profile | None = None
    preview: VMConfig | None = None
    notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)   # must be resolved before importing
    blocker_codes: list[str] = field(default_factory=list)  # failing-disk / no-os / unsupported
    questions: list[str] = field(default_factory=list)  # need a user decision (hibernation, key)

    def block(self, code: str, message: str) -> None:
        self.blockers.append(message)
        self.blocker_codes.append(code)

    @property
    def os(self) -> DetectedOS | None:
        oses = self.report.oses
        return oses[self.os_index] if 0 <= self.os_index < len(oses) else None

    def to_dict(self) -> dict:
        return {"sources": [s.to_dict() for s in self.sources],
                "report": self.report.to_dict(),
                "health": {k: {"verdict": v.verdict, "summary": v.summary, "details": v.details}
                           for k, v in self.health.items()},
                "os_index": self.os_index,
                "profile": self.profile.id if self.profile else None,
                "preview": self.preview.to_dict() if self.preview else None,
                "notes": self.notes, "blockers": self.blockers, "questions": self.questions}


def facts_with_overrides(o: DetectedOS | None, overrides: dict) -> dict:
    facts = o.facts() if o else {"family": "other"}
    for k in ("family", "distro", "version", "build", "edition", "arch"):
        if overrides.get(k):
            facts[k] = overrides[k]
    return facts


def choose_profile(o: DetectedOS | None, overrides: dict) -> Profile:
    if overrides.get("profile"):
        return profiles.get(overrides["profile"])
    return profiles.select(facts_with_overrides(o, overrides))


def _drives(sources: list[Source], access: overlay.SourceAccess) -> list[Drive]:
    out = []
    for s in sources:
        key = s.stable_path or s.path
        path = access.paths.get(key, s.path)
        fmt = "raw" if s.kind in ("block", "ewf") else s.format
        out.append(Drive(path=path, format=fmt, readonly=True, label=f"src{len(out)}"))
    return out


def analyze(paths: list[str], keys: list[str] | None = None, smart_check: bool = True,
            allow_failing_disk: bool = False, progress: jobs.Progress | None = None,
            fd_provider=None) -> Analysis:
    progress = progress or jobs.Progress(title="Analysing")
    progress.stage("Checking the source")
    if not paths:
        raise RehomeError("Choose at least one disk or disk image to import.")
    sources = [srcmod.describe(p) for p in paths]
    for s in sources:
        if not s.safe and s.problem_code in ("host-disk", "mounted", "in-use"):
            raise UnsafeSource(" ".join(s.problems), code=s.problem_code,
                               fix={"host-disk": "Rehome never imports the disk the running system is on.",
                                    "mounted": "Unmount the disk's partitions (for example in your file "
                                               "manager), then try again.",
                                    "in-use": "Stop what uses the disk (RAID, LVM, opened encrypted "
                                              "volume), then try again."}[s.problem_code])
        if not s.safe:
            raise RehomeError(" ".join(s.problems), code=s.problem_code)
    health = {}
    a = Analysis(sources=sources, report=InspectionReport(), health=health)
    if smart_check:
        for s in sources:
            if s.kind != "block":
                continue
            progress.note(f"Checking the health of {s.display}")
            h = smart.check(s.path)
            health[s.path] = h
            if h.blocks_import and not allow_failing_disk:
                a.block("failing-disk",
                        f"{s.display}: {h.summary} Image it to a file with ddrescue first, then "
                        f"import the image:\n  " + "\n  ".join(smart.ddrescue_advice(s.path)))
    if a.blockers:
        # Do not read a failing disk any further than SMART already did.
        return a
    progress.stage("Identifying the system")
    with overlay.SourceAccess(sources, fd_provider=fd_provider) as access:
        a.report = inspect(_drives(sources, access), keys=keys or [],
                           progress=lambda msg: progress.note(msg))
    refresh(a, {})
    return a


def refresh(a: Analysis, overrides: dict, host=None) -> None:
    """(Re)compute profile, preview hardware, notes and questions for the analysis."""
    o = a.os
    a.notes, a.questions = list(a.report.notes), []
    keep = [(c, m) for c, m in zip(a.blocker_codes, a.blockers) if c == "failing-disk"]
    a.blocker_codes, a.blockers = [c for c, _ in keep], [m for _, m in keep]
    a.profile = choose_profile(o, overrides)
    if o is None and not (overrides.get("family") or overrides.get("profile")):
        a.block("no-os", "No operating system was identified. You can still import the disk: "
                         "choose the operating system family manually.")
    if a.profile and not a.profile.supported:
        a.block("unsupported", " ".join(a.profile.notes) or f"{a.profile.name} cannot be imported.")
    if o and o.hibernated:
        a.questions.append("hibernation")
    if a.report.encrypted_locked and o is None:
        a.questions.append("key")
    if o and "bitlocker" in [v.encryption for d in a.report.disks for v in d.volumes]:
        a.notes.append("BitLocker is on: the virtual TPM cannot unlock the drive, so Windows will "
                       "ask for the recovery key at the first boot.")
    if a.report.multi_boot:
        a.notes.append("Several operating systems were found; the VM boots the one you choose, "
                       "through the disk's own boot menu if it has one.")
    if o:
        a.notes += o.notes
        a.notes += o.warnings
    a.preview = build_config(a, ImportOptions(overrides=overrides), host=host)


def build_config(a: Analysis, opts: ImportOptions, host=None) -> VMConfig:
    o = a.os
    prof = choose_profile(o, opts.overrides)
    facts = facts_with_overrides(o, opts.overrides)
    cfg = VMConfig(name=opts.name or (o.label if o else "Imported system"))
    cfg.os = OsInfo(family=facts.get("family", "other"), name=o.label if o else "",
                    version=str(facts.get("version", "")), distro=facts.get("distro", ""),
                    arch=facts.get("arch", "x86_64"), osinfo_id=o.osinfo if o else "",
                    build=str(facts.get("build", "")), edition=facts.get("edition", ""))
    from rehome.vm.config import Disk
    for i, s in enumerate(a.sources):
        cfg.disks.append(Disk(id=f"disk{i}", file=f"disks/disk{i}.qcow2",
                              boot_index=1 if o and i == o.disk_index else 0))
    if not any(d.boot_index for d in cfg.disks) and cfg.disks:
        cfg.disks[0].boot_index = 1
    host_bus = a.sources[o.disk_index].transport if o and o.disk_index < len(a.sources) else ""
    original_bus = (o.original_bus if o else "") or {"sata": "sata", "nvme": "nvme", "ata": "ide",
                                                      "usb": "sata"}.get(host_bus, "")
    ctx = HardwareContext(fresh=False, boot_mode=o.boot_mode if o else "",
                          original_bus=original_bus,
                          virtio_ready=bool(o and o.virtio_ready and o.family == "linux"))
    notes = apply_profile(cfg, prof, facts, ctx, host=host or host_info.gather(),
                          memory_mib=opts.overrides.get("memory_mib"),
                          cpus=opts.overrides.get("cpus"))
    m = cfg.machine
    if o and o.rtc and prof.hw("rtc") is not None:
        m.rtc = o.rtc   # keep the guest's own clock convention (dual-boot setups)
    ov = opts.overrides
    if ov.get("firmware"):
        m.firmware = ov["firmware"]
    if ov.get("machine"):
        m.type = ov["machine"]
    if ov.get("accel"):
        m.accel = ov["accel"]
    if ov.get("disk_bus"):
        for d in cfg.disks:
            d.bus = ov["disk_bus"]
    if ov.get("tpm"):
        m.tpm = ov["tpm"]
    if m.firmware == "uefi-secure" and m.type == "pc":
        m.type = "q35"
    add_cdrom(cfg, "")   # empty drive for guest tools / driver discs
    cfg.notes = "\n".join(notes)
    return cfg


def run(a: Analysis, opts: ImportOptions, progress: jobs.Progress | None = None,
        fd_provider=None) -> VMConfig:
    progress = progress or jobs.Progress(title="Importing")
    progress.stages = [jobs.Stage(n, w) for n, w in zip(IMPORT_STAGES, (1, 6, 6, 2, 4, 6))]
    for code, message in zip(a.blocker_codes, a.blockers):
        if code == "failing-disk" and opts.allow_failing_disk:
            continue
        if code == "no-os" and (opts.overrides.get("family") or opts.overrides.get("profile")):
            continue
        raise RehomeError("The import cannot continue: " + message, code=code)
    o = a.os
    if o and o.hibernated and opts.discard_hibernation is None and opts.patch:
        raise RehomeError(
            "Windows on this disk is hibernated (or was shut down with Fast Startup).",
            fix="Choose whether to discard the hibernation state in the VM (the unsaved session is "
                "lost; the original disk is not changed), or boot Windows on the original computer "
                "and shut it down fully first.", code="needs-hibernation-choice")
    if opts.mode not in ("linked", "copy"):
        raise RehomeError(f"Unknown import mode \"{opts.mode}\".")
    a.os_index = opts.os_index
    cfg = build_config(a, opts)
    cfg.name = library.unique_name(cfg.name)
    library.create_dir(cfg)
    log = cfg.dir / "logs" / "import.log"
    try:
        hashing.log_event(log, "start", mode=opts.mode, sources=[s.to_dict() for s in a.sources],
                          os=o.facts() if o else None)
        (cfg.dir / "inspection.json").write_text(json.dumps(a.report.to_dict(), indent=2))
        with overlay.SourceAccess(a.sources, fd_provider=fd_provider) as access:
            if opts.verify_hash:
                progress.stage("Hashing the source")
                for i, s in enumerate(a.sources):
                    digest = _hash(s, progress, i, len(a.sources), fd_provider)
                    cfg.disks[i].source = overlay.to_disk_source(s, opts.mode)
                    cfg.disks[i].source.sha256_before = digest
                    hashing.log_event(log, "hash-before", source=s.stable_path or s.path,
                                      sha256=digest)
            progress.stage("Creating the VM's disk")
            for i, s in enumerate(a.sources):
                before = cfg.disks[i].source.sha256_before if cfg.disks[i].source else ""
                if opts.mode == "linked":
                    d = overlay.create_linked(cfg, f"disk{i}", s)
                else:
                    key = s.stable_path or s.path

                    def prog(frac, i=i):
                        progress.update((i + frac) / len(a.sources))
                    d = overlay.create_copy(cfg, f"disk{i}", s, access.paths.get(key, s.path),
                                            progress=prog, cancel=progress.cancel_cb())
                d.bus, d.boot_index = cfg.disks[i].bus, cfg.disks[i].boot_index
                d.source.sha256_before = before
                d.source.original_bus = (o.original_bus if o and i == o.disk_index else "") \
                    or d.source.original_bus
                cfg.disks[i] = d
                hashing.log_event(log, "disk", id=d.id, mode=opts.mode, file=d.file)
        cfg.import_info = ImportInfo(created=now_iso(), mode=opts.mode,
                                     profile=cfg.profile, inspection="inspection.json")
        library.save(cfg)
        if opts.patch and o and a.profile and a.profile.patches:
            progress.stage("Applying fixes")
            from rehome.patch import engine
            try:
                results = engine.run(cfg, o, opts, progress=progress, fd_provider=fd_provider)
            except Cancelled:
                raise
            except RehomeError as e:
                # The VM exists and the source is untouched; report instead of discarding.
                results = []
                cfg = library.reload(cfg)
                cfg.import_info.notes.append(f"Automatic fixes could not be applied: {e.message}")
                library.save(cfg)
                hashing.log_event(log, "patch-failed", error=e.message, details=e.details)
            failed = [r for r in results if r.status == "failed"]
            if failed:
                cfg = library.reload(cfg)
                cfg.import_info.notes += [f"{r.title}: {r.message}" for r in failed]
                library.save(cfg)
        if opts.verify_hash:
            progress.stage("Verifying the source")
            verify(cfg, progress=progress, fd_provider=fd_provider)
        hashing.log_event(log, "done", vm=cfg.id)
        return library.reload(cfg)
    except BaseException as e:
        hashing.log_event(log, "failed", error=str(e))
        # Nothing of value exists yet (only the overlay/copy); never leave a half VM behind.
        shutil.rmtree(cfg.dir, ignore_errors=True)
        raise


def _hash(s: Source, progress: jobs.Progress, i: int, n: int, fd_provider) -> str:
    def prog(frac):
        progress.update((i + frac) / n, f"SHA-256 of {s.display}")
    return hashing.hash_source(s, progress=prog, cancel=progress.cancel_cb(), fd_provider=fd_provider)


def verify(cfg: VMConfig, progress: jobs.Progress | None = None, fd_provider=None) -> list[dict]:
    """Re-hash the sources of a linked/copied import and compare with the stored hash."""
    progress = progress or jobs.Progress(title="Verifying")
    results = []
    disks = [d for d in cfg.disks if d.source and d.source.sha256_before]
    if not disks:
        raise RehomeError("No source hash was recorded for this VM.",
                          fix="Enable integrity verification when importing.")
    log = cfg.dir / "logs" / "import.log"
    for i, d in enumerate(disks):
        s = d.source
        src = Source(kind=s.kind, path=s.path, stable_path=s.path, format=s.format, size=s.size)

        def prog(frac, i=i):
            progress.update((i + frac) / len(disks), f"SHA-256 of {s.label or s.path}")
        if s.kind in ("image", "ewf") and not os.path.exists(s.path):
            raise RehomeError(f"The source {s.path} is not available.")
        digest = hashing.hash_source(src, progress=prog, cancel=progress.cancel_cb(),
                                     fd_provider=fd_provider)
        ok = digest == s.sha256_before
        s.sha256_after = digest
        results.append({"disk": d.id, "source": s.path, "before": s.sha256_before,
                        "after": digest, "unchanged": ok})
        hashing.log_event(log, "hash-after", source=s.path, sha256=digest, unchanged=ok)
    library.save(cfg)
    bad = [r for r in results if not r["unchanged"]]
    if bad:
        raise RehomeError("The source's checksum changed since the import!",
                          details=json.dumps(bad, indent=2),
                          fix="Something else wrote to the source disk. Rehome itself only reads it.")
    return results
