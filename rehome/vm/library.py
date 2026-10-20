"""The user's VM library: one directory per VM under ``~/.local/share/rehome/vms``."""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

from rehome import paths
from rehome.errors import NotFound, RehomeError
from rehome.util import toml
from rehome.vm.config import VMConfig

CONFIG_NAME = "vm.toml"


def library_dir() -> Path:
    return paths.ensure(paths.vms_dir())


def _vm_dirs():
    lib = library_dir()
    for d in sorted(lib.iterdir()):
        if d.is_dir() and (d / CONFIG_NAME).exists():
            yield d


def load_dir(d: Path) -> VMConfig:
    try:
        cfg = VMConfig.from_dict(toml.load(d / CONFIG_NAME))
    except (OSError, ValueError) as e:
        raise RehomeError(f"The configuration of the VM in {d} could not be read.",
                          fix="Check vm.toml for typos, or restore it from vm.toml.bak.",
                          details=str(e)) from e
    cfg.path = d
    return cfg


def list_vms() -> list[VMConfig]:
    vms = []
    for d in _vm_dirs():
        try:
            vms.append(load_dir(d))
        except RehomeError:
            continue
    return sorted(vms, key=lambda v: v.name.lower())


def find(ref: str) -> VMConfig:
    """Find a VM by exact name, case-insensitive name, UUID or UUID prefix."""
    vms = list_vms()
    for test in (lambda v: v.name == ref, lambda v: v.id == ref,
                 lambda v: v.name.lower() == ref.lower(),
                 lambda v: len(ref) >= 4 and v.id.startswith(ref.lower())):
        hits = [v for v in vms if test(v)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise RehomeError(f"More than one VM matches \"{ref}\".",
                              fix="Use the VM's ID instead (see `rehome list`).")
    raise NotFound(f"There is no VM called \"{ref}\".",
                   fix="Run `rehome list` to see your VMs.")


def name_taken(name: str, exclude_id: str | None = None) -> bool:
    return any(v.name.lower() == name.lower() and v.id != exclude_id for v in list_vms())


def unique_name(base: str) -> str:
    if not name_taken(base):
        return base
    n = 2
    while name_taken(f"{base} ({n})"):
        n += 1
    return f"{base} ({n})"


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return s[:40] or "vm"


def create_dir(cfg: VMConfig) -> Path:
    d = library_dir() / f"{slug(cfg.name)}-{cfg.short_id}"
    d.mkdir(parents=True, exist_ok=False)
    for sub in ("disks", "logs"):
        (d / sub).mkdir()
    cfg.path = d
    return d


def save(cfg: VMConfig) -> None:
    f = cfg.dir / CONFIG_NAME
    if f.exists():
        shutil.copy2(f, cfg.dir / (CONFIG_NAME + ".bak"))
    toml.dump_atomic(cfg.to_dict(), f)


def reload(cfg: VMConfig) -> VMConfig:
    return load_dir(cfg.dir)


@contextmanager
def locked(cfg: VMConfig):
    """Exclusive lock for read-modify-write of a VM's configuration."""
    lock_path = cfg.dir / ".lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def kept_disks_dir(name: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return paths.ensure(paths.data_dir() / "kept-disks" / f"{slug(name)}-{stamp}")


def delete(cfg: VMConfig, keep_disks: bool = False) -> Path | None:
    """Delete a stopped VM. With ``keep_disks`` the disk files are moved aside.

    Returns the directory the disks were moved to, if any. Source disks of
    linked imports are never touched (they are not inside the VM directory).
    """
    from rehome.vm import runner
    if runner.is_running(cfg):
        raise RehomeError(f"\"{cfg.name}\" is running.", fix="Shut the VM down first.")
    kept = None
    if keep_disks:
        files = [cfg.resolve(d.file) for d in cfg.disks if d.file]
        files = [f for f in files if f.exists()]
        if files:
            kept = kept_disks_dir(cfg.name)
            for f in files:
                shutil.move(str(f), kept / f.name)
            # Keep the config next to the disks so they can be re-imported.
            shutil.copy2(cfg.dir / CONFIG_NAME, kept / CONFIG_NAME)
    root = library_dir().resolve()
    target = cfg.dir.resolve()
    if root not in target.parents:
        raise RehomeError("Refusing to delete a directory outside the VM library.",
                          details=str(target))
    shutil.rmtree(target)
    try:
        shutil.rmtree(paths.vm_runtime_dir(cfg.id))
    except OSError:
        pass
    return kept


def disk_usage(cfg: VMConfig) -> int:
    total = 0
    for root, _dirs, files in os.walk(cfg.dir):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_blocks * 512
            except OSError:
                pass
    return total
