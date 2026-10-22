"""Clone, export and import VMs."""

from __future__ import annotations

import json
import shutil
import tarfile
import uuid
from pathlib import Path

from rehome.errors import RehomeError
from rehome.util import toml
from rehome.vm import disks, library, runner, storage
from rehome.vm.config import VMConfig, now_iso, random_mac

EXPORT_FORMAT = "rehome-export-1"


def _copy_file(src: Path, dst: Path) -> None:
    """Copy using reflinks where the filesystem supports them (btrfs, xfs)."""
    try:
        import fcntl
        FICLONE = 0x40049409
        with open(src, "rb") as s, open(dst, "wb") as d:
            fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
        shutil.copystat(src, dst)
        return
    except (OSError, ImportError):
        pass
    shutil.copy2(src, dst)


def clone(cfg: VMConfig, new_name: str, progress=None, folder: str | None = None) -> VMConfig:
    """Full clone. Linked imports stay linked to the same (read-only) source.

    Disk copies go to ``folder`` (None: the default folder for new disks, "": the VM's);
    the small overlays of linked imports stay in the VM's folder.
    """
    if runner.state(cfg).active:
        raise RehomeError(f"\"{cfg.name}\" must be turned off to clone it.",
                          fix="Shut the VM down (or save it) first.")
    new = VMConfig.from_dict(cfg.to_dict())
    new.id = str(uuid.uuid4())
    new.name = library.unique_name(new_name)
    new.created = now_iso()
    new.snapshots = []
    new.current_snapshot = ""
    for n in new.nics:
        n.mac = random_mac()
    if folder:
        storage.check_folder(folder)
    library.create_dir(new)
    made = []
    try:
        total = len(cfg.disks) or 1
        for i, d in enumerate(new.disks):
            src = cfg.resolve(d.file)
            overlay = d.source is not None and d.source.mode == "linked"
            d.file, d.owned = storage.new_disk_path(new, d.id, "" if overlay else folder, d.format)
            dst = new.resolve(d.file)
            dst.parent.mkdir(parents=True, exist_ok=True)
            made.append(dst)
            _copy_file(src, dst)
            if d.format == "qcow2":
                for s in disks.snapshot_list(str(dst)):
                    disks.snapshot_delete(str(dst), s["name"])
            if progress:
                progress((i + 1) / total)
        for extra in ("nvram.qcow2", "nvram.json"):
            if (cfg.dir / extra).exists():
                _copy_file(cfg.dir / extra, new.dir / extra)
        if (new.dir / "nvram.qcow2").exists():
            for s in disks.snapshot_list(str(new.dir / "nvram.qcow2")):
                disks.snapshot_delete(str(new.dir / "nvram.qcow2"), s["name"])
        if (cfg.dir / "tpm").is_dir():
            shutil.copytree(cfg.dir / "tpm", new.dir / "tpm")
        library.save(new)
    except BaseException:
        shutil.rmtree(new.dir, ignore_errors=True)
        for f in made:
            f.unlink(missing_ok=True)
        raise
    return new


def export(cfg: VMConfig, dest: str, compress: bool = False, progress=None) -> Path:
    """Export to a ``.rehome`` archive with standalone disks (sources flattened)."""
    if runner.state(cfg).active:
        raise RehomeError(f"\"{cfg.name}\" must be turned off to export it.")
    dest_path = Path(dest)
    if dest_path.is_dir():
        dest_path = dest_path / f"{library.slug(cfg.name)}.rehome"
    if dest_path.exists():
        raise RehomeError(f"{dest_path} already exists.", fix="Choose another file name.")
    work = dest_path.with_name(dest_path.name + ".work")
    (work / "disks").mkdir(parents=True)
    try:
        out = VMConfig.from_dict(cfg.to_dict())
        n = len(cfg.disks) or 1
        for i, d in enumerate(out.disks):
            src = cfg.resolve(d.file)
            name = f"{d.id}.qcow2"
            src_spec, image_opts = str(src), False
            if d.source and d.source.mode == "linked" and d.source.kind == "block":
                raise RehomeError(
                    "This VM still reads a physical disk directly (a linked import), which "
                    "cannot be exported.",
                    fix="Convert it to a standalone copy first (VM settings → Disks → "
                        "Make standalone), then export.")

            def prog(frac, i=i):
                if progress:
                    progress((i + frac) / n)
            disks.convert(src_spec, str(work / "disks" / name), src_fmt=d.format,
                          compress=compress, progress=prog, image_opts=image_opts)
            d.file = f"disks/{name}"
            d.format, d.owned = "qcow2", False
            if d.source:
                d.source.mode = "copy"
        out.snapshots, out.current_snapshot = [], ""
        meta = {"format": EXPORT_FORMAT, "exported": now_iso(), "name": cfg.name}
        (work / "export.json").write_text(json.dumps(meta, indent=2))
        toml.dump_atomic(out.to_dict(), work / "vm.toml")
        for extra in ("nvram.qcow2", "nvram.json"):
            if (cfg.dir / extra).exists():
                shutil.copy2(cfg.dir / extra, work / extra)
        if (cfg.dir / "tpm").is_dir():
            shutil.copytree(cfg.dir / "tpm", work / "tpm")
        with tarfile.open(dest_path, "w") as tar:
            for p in sorted(work.rglob("*")):
                tar.add(p, arcname=str(p.relative_to(work)), recursive=False)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return dest_path


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise RehomeError("The export file contains unsafe paths and was not imported.",
                              details=m.name)
        yield m


def import_bundle(archive: str, name: str | None = None) -> VMConfig:
    path = Path(archive)
    if not path.is_file():
        raise RehomeError(f"{archive} does not exist.")
    with tarfile.open(path) as tar:
        names = tar.getnames()
        if "vm.toml" not in names:
            raise RehomeError(f"{archive} is not a Rehome export.",
                              fix="Choose a .rehome file created by `rehome export`.")
        cfg = VMConfig.from_dict(toml.loads(tar.extractfile("vm.toml").read().decode()))
        cfg.id = str(uuid.uuid4())
        cfg.name = library.unique_name(name or cfg.name)
        cfg.created = now_iso()
        library.create_dir(cfg)
        try:
            members = [m for m in _safe_members(tar) if m.name not in ("vm.toml", "export.json")]
            try:
                tar.extractall(cfg.dir, members=members, filter="data")
            except TypeError:  # Python < 3.12 without extraction filters
                tar.extractall(cfg.dir, members=members)
            for d in cfg.disks:
                if not cfg.resolve(d.file).exists():
                    raise RehomeError(f"The export is missing the disk {d.file}.")
            library.save(cfg)
        except BaseException:
            shutil.rmtree(cfg.dir, ignore_errors=True)
            raise
    return cfg


def make_standalone(cfg: VMConfig, progress=None, cancel=None, folder: str | None = None) -> None:
    """Flatten linked-import disks into standalone images (source can be removed).

    The copies go to ``folder`` (None: the default folder for new disks, "": the VM's).
    """
    if runner.state(cfg).active:
        raise RehomeError("Turn the VM off first.")
    linked = [d for d in cfg.disks if d.source and d.source.mode == "linked"]
    if not linked:
        return
    from rehome.disk import overlay
    if folder:
        storage.check_folder(folder)
    if any(d.source.kind == "block" for d in linked):
        overlay.flatten_with_sources(cfg, linked, progress=progress, cancel=cancel, folder=folder)
    else:
        overlay._flatten(cfg, linked, progress, cancel, folder)
