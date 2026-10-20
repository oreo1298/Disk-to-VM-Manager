"""Apply a profile's patches to an imported VM's overlays.

Order:
 1. A checkpoint ("Before automatic fixes") is taken on every overlay so
    the whole run can be rolled back, and the VM settings are saved too.
 2. External patches (virt-v2v) run on the overlays.
 3. One libguestfs session applies the disk patches, in profile order.
 4. Config patches adjust the virtual hardware.
Every patch is logged (logs/patch.log, JSON lines) with what it changed.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass, field

from rehome import jobs
from rehome.disk import hashing, overlay
from rehome.errors import Cancelled, RehomeError
from rehome.inspect import gfs
from rehome.inspect.model import DetectedOS, InspectionReport
from rehome.patch import profiles, registry
from rehome.patch.registry import CONFIG, DISK, EXTERNAL, PatchResult
from rehome.vm import library, snapshots
from rehome.vm.config import VMConfig

log = logging.getLogger(__name__)
PRE_PATCH_NAME = "Before automatic fixes"


@dataclass
class PatchContext:
    cfg: VMConfig
    os: DetectedOS
    profile: profiles.Profile
    options: object                      # importer.ImportOptions (discard_hibernation, keys, ...)
    g: object = None                     # libguestfs handle (disk patches)
    root: str = ""                       # appliance device of the OS root
    report: InspectionReport | None = None
    state: dict = field(default_factory=dict)
    progress: jobs.Progress | None = None

    def param(self, key: str, default=None):
        return self.profile.patch_params.get(key, default)

    def note(self, msg: str) -> None:
        if self.progress:
            self.progress.note(msg)


def patch_list(profile: profiles.Profile, o: DetectedOS) -> list[str]:
    ids = list(profile.patches)
    if o.boot_mode == "uefi" and o.family in ("windows", "linux") and "uefi.fallback_loader" not in ids:
        ids.insert(0, "uefi.fallback_loader")
    return ids


def _log(cfg: VMConfig, result: PatchResult) -> None:
    hashing.log_event(cfg.dir / "logs" / "patch.log", "patch", **asdict(result))


def run(cfg: VMConfig, o: DetectedOS, options, progress: jobs.Progress | None = None,
        only: list[str] | None = None, fd_provider=None) -> list[PatchResult]:
    profile = profiles.get(cfg.profile) if cfg.profile else profiles.select(o.facts())
    ids = only or patch_list(profile, o)
    specs = [registry.get(i) for i in ids]
    report = None
    insp = cfg.dir / "inspection.json"
    if insp.exists():
        report = InspectionReport.from_dict(json.loads(insp.read_text()))
    ctx = PatchContext(cfg=cfg, os=o, profile=profile, options=options, report=report,
                       progress=progress)
    results: list[PatchResult] = []

    # 1. Checkpoint + settings backup for rollback.
    if not any(s.kind == "pre-patch" for s in cfg.snapshots):
        snap = snapshots.create(cfg, PRE_PATCH_NAME, "Taken automatically before Rehome "
                                "changed anything inside the imported system.", kind="pre-patch")
        cfg.import_info.patch_snapshot = snap.id
        shutil.copy2(cfg.dir / library.CONFIG_NAME, cfg.dir / "vm.toml.pre-patch")
        library.save(cfg)

    def record(r: PatchResult):
        results.append(r)
        _log(cfg, r)

    # 2. External tools (virt-v2v) work on the overlays directly.
    for spec in [s for s in specs if s.kind == EXTERNAL]:
        _run_one(spec, ctx, record)

    # 3. Disk patches in one appliance session.
    disk_specs = [s for s in specs if s.kind == DISK]
    if disk_specs:
        if progress:
            progress.note("Starting the patching appliance")
        drives = [gfs.Drive(str(cfg.resolve(d.file)), d.format, readonly=False, label=d.id)
                  for d in cfg.disks]
        with overlay.for_config(cfg, fd_provider=fd_provider):
            with gfs.session(drives) as g:
                ctx.g = g
                _unlock(ctx)
                ctx.root = _find_root(ctx)
                for i, spec in enumerate(disk_specs):
                    if progress:
                        progress.update(i / len(disk_specs), spec.title)
                    _run_one(spec, ctx, record)
                ctx.g = None

    # 4. Hardware/config adjustments.
    for spec in [s for s in specs if s.kind == CONFIG]:
        _run_one(spec, ctx, record)
    cfg.import_info.patched = True
    library.save(cfg)
    summary = cfg.dir / "logs" / "patch-summary.txt"
    with open(summary, "w") as f:
        for r in results:
            f.write(f"[{r.status}] {r.title}: {r.message}\n")
            for c in r.changes:
                f.write(f"    - {c}\n")
    return results


def _run_one(spec, ctx: PatchContext, record) -> None:
    if spec.families and ctx.os.family not in spec.families:
        record(PatchResult(spec.id, spec.title, "not-needed", "Not used for this system."))
        return
    if ctx.progress:
        ctx.progress.check()
        ctx.progress.note(spec.title)
    try:
        r = spec.fn(ctx)
        if r is None:
            r = PatchResult(spec.id, spec.title, "applied")
        r.id, r.title = spec.id, spec.title
    except Cancelled:
        raise
    except RehomeError as e:
        r = PatchResult(spec.id, spec.title, "failed", e.message)
    except Exception as e:  # noqa: BLE001 - one broken patch must not stop the others
        log.exception("patch %s failed", spec.id)
        r = PatchResult(spec.id, spec.title, "failed", str(e))
    record(r)


def _unlock(ctx: PatchContext) -> None:
    keys = list(getattr(ctx.options, "keys", []) or [])
    if not keys:
        return
    g = ctx.g
    for dev, fs in g.list_filesystems().items():
        if fs not in ("crypto_LUKS", "BitLocker"):
            continue
        for key in keys:
            try:
                kind = "bitlk" if fs == "BitLocker" else None
                name = f"rehome_rw{len(ctx.state.setdefault('unlocked', []))}"
                if kind:
                    g.cryptsetup_open(dev, key, name, readonly=False, crypttype=kind)
                else:
                    g.cryptsetup_open(dev, key, name, readonly=False)
                ctx.state["unlocked"].append(dev)
                break
            except RuntimeError:
                continue


def _find_root(ctx: PatchContext) -> str:
    """Locate the OS root in this session (device names match the inspection's)."""
    g = ctx.g
    if ctx.os.family in ("windows", "linux", "bsd"):
        try:
            roots = g.inspect_os()
        except RuntimeError:
            roots = []
        if ctx.os.root in roots:
            return ctx.os.root
        for r in roots:
            if g.inspect_get_type(r) == {"bsd": "freebsd"}.get(ctx.os.family, ctx.os.family):
                return r
        return roots[0] if roots else ctx.os.root
    return ctx.os.root


def rollback(cfg: VMConfig) -> None:
    """Undo every automatic fix: revert the overlays and restore the VM settings."""
    snap = next((s for s in cfg.snapshots if s.kind == "pre-patch"), None)
    if snap is None:
        raise RehomeError("There is no pre-patch checkpoint to roll back to.")
    from rehome.vm import runner
    if runner.state(cfg).active:
        raise RehomeError("Turn the VM off before undoing the automatic fixes.")
    snapshots.revert(cfg, snap.id)
    backup = cfg.dir / "vm.toml.pre-patch"
    if backup.exists():
        restored = library.load_dir(cfg.dir)
        pre = VMConfig.from_dict(__import__("tomllib").loads(backup.read_text()))
        pre.path = cfg.dir
        pre.snapshots, pre.current_snapshot = restored.snapshots, restored.current_snapshot
        if pre.import_info:
            pre.import_info.patched = False
        library.save(pre)
    hashing.log_event(cfg.dir / "logs" / "patch.log", "rollback", snapshot=snap.id)
