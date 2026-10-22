"""Where VM disk images live, and adding, replacing and removing a VM's disks.

New disks go into the VM's folder (``disks/``) unless a folder is chosen, per
disk or as the default in Preferences (``[storage] disk_dir`` in
``~/.config/rehome/settings.toml``), for example a larger second drive.
Disks Rehome creates outside the VM's folder are marked ``owned`` and are
deleted (or kept aside) together with the VM; disk images the user attached
are never deleted by Rehome.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from rehome.errors import RehomeError
from rehome.util.units import human
from rehome.vm import disks, library, runner
from rehome.vm.config import Disk, VMConfig

IMAGE_FORMATS = ("qcow2", "raw", "vmdk", "vdi", "vpc", "vhdx", "qed")


# --- the default folder ------------------------------------------------------------------

def default_disk_dir() -> Path | None:
    """The folder chosen in Preferences for new disks (None: inside each VM's folder)."""
    from rehome import update
    value = update.load_settings().get("storage", {}).get("disk_dir", "")
    return Path(value) if value else None


def set_default_disk_dir(folder: str | os.PathLike | None) -> None:
    from rehome import update
    from rehome.util import toml
    data = update.load_settings()
    storage = data.setdefault("storage", {})
    if folder:
        storage["disk_dir"] = str(check_folder(folder))
    else:
        storage.pop("disk_dir", None)
    toml.dump_atomic(data, update.settings_path())


def check_folder(folder: str | os.PathLike) -> Path:
    """A folder new disks can be written to (absolute), or a clear error."""
    p = Path(folder).expanduser()
    if not p.is_absolute():
        p = p.resolve()
    if not p.is_dir():
        raise RehomeError(f"The folder {p} does not exist.",
                          fix="Choose an existing folder, for example on a mounted second drive.")
    if not os.access(p, os.W_OK | os.X_OK):
        raise RehomeError(f"You cannot write to {p}.",
                          fix="Choose a folder you own, or change its permissions.")
    return p


def free_space(folder: str | os.PathLike) -> int:
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return 0


def describe(folder: Path | None, cfg: VMConfig | None = None, need: int = 0) -> str:
    """For the GUI: where a new disk would go and how much space is there.

    ``need``: the disk's full size, to warn when the folder's drive is smaller.
    """
    if folder and not Path(folder).is_dir():
        return f"{folder} (not available: is its drive connected?)"
    target = Path(folder) if folder else (cfg.dir / "disks" if cfg and cfg.path else library.library_dir())
    where = str(folder) if folder else "In the VM's folder"
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = free_space(probe)
    text = f"{where} ({human(free)} free)"
    if need and free < need:
        text += f" — less than the disk's full size ({human(need)})"
    return text


# --- paths ---------------------------------------------------------------------------------

def new_disk_path(cfg: VMConfig, disk_id: str, folder: str | os.PathLike | None = None,
                  fmt: str = "qcow2") -> tuple[str, bool]:
    """(file for vm.toml, owned) for a new disk.

    ``folder``: None = the default folder from Preferences, "" = the VM's own
    folder, else that folder. Never the name of an existing file
    (``qemu-img create`` would overwrite it).
    """
    if folder is None:
        chosen = default_disk_dir()
    else:
        chosen = Path(folder) if str(folder) else None
    if chosen is None:
        base, stem, owned = cfg.dir / "disks", disk_id, False   # deleted with the VM folder
    else:
        base, owned = check_folder(chosen), True
        stem = f"{library.slug(cfg.name)}-{cfg.short_id}-{disk_id}"
    ext = "img" if fmt == "raw" else fmt
    name, n = f"{stem}.{ext}", 1
    while (base / name).exists():
        n += 1
        name = f"{stem}-{n}.{ext}"
    return (str(base / name) if owned else f"disks/{name}"), owned


def is_inside_vm(cfg: VMConfig, d: Disk) -> bool:
    try:
        return cfg.dir.resolve() in cfg.resolve(d.file).resolve().parents
    except (OSError, ValueError):
        return False


def created_by_rehome(cfg: VMConfig, d: Disk) -> bool:
    """Rehome made this file (so it may delete it); user images and sources are never deleted."""
    return d.owned or is_inside_vm(cfg, d)


# --- operations on a VM's disks ---------------------------------------------------------

def _require_off(cfg: VMConfig, what: str) -> None:
    st = runner.state(cfg)
    if st.active:
        raise RehomeError(f"\"{cfg.name}\" must be turned off to {what}.",
                          fix="Shut the VM down first.", code="vm-running")
    if st.status == runner.SAVED:
        # The saved memory expects the disks it was using; resuming onto other disks corrupts them.
        raise RehomeError(f"\"{cfg.name}\" was saved while running, so its disks cannot change now.",
                          fix="Start it and shut it down normally, or discard the saved state.",
                          code="vm-saved")


def _require_no_checkpoints(cfg: VMConfig) -> None:
    if cfg.snapshots:
        raise RehomeError(
            f"\"{cfg.name}\" has checkpoints, which belong to its current disks.",
            fix="Delete its checkpoints first (Checkpoints tab, or `rehome checkpoint delete`), "
                "then replace the disk.", code="has-checkpoints")


def in_use(cfg: VMConfig, path: Path) -> str:
    """Who already uses this file as a disk or as an import's original ("" if nobody)."""
    for vm in [cfg, *(v for v in library.list_vms() if v.id != cfg.id)]:
        who = "this VM" if vm.id == cfg.id else f"\"{vm.name}\""
        for d in vm.disks:
            try:
                if d.file and vm.resolve(d.file).resolve() == path:
                    return f"{who} ({d.id})"
                if d.source and d.source.path and Path(d.source.path).resolve() == path:
                    return f"{who}, as the original disk it was imported from"
            except (OSError, ValueError):
                continue
    return ""


def image_disk(image: str | os.PathLike, disk_id: str, bus: str, cfg: VMConfig | None = None) -> Disk:
    """A disk entry for an existing image file, used where it is (never a physical disk)."""
    p = Path(image).expanduser().resolve()
    try:
        st = p.stat()
    except OSError as e:
        raise RehomeError(f"The disk image {p} does not exist.") from e
    if stat.S_ISBLK(st.st_mode) or str(p).startswith("/dev/"):
        raise RehomeError("A physical disk cannot be attached directly.",
                          fix="Use Import (it protects the disk and reads it through an overlay).")
    if not stat.S_ISREG(st.st_mode):
        raise RehomeError(f"{p} is not a disk image file.")
    user = in_use(cfg, p) if cfg is not None else ""
    if user:
        # Two VMs writing one image corrupt it; an import's original is never written to.
        raise RehomeError(f"{p.name} is already used by {user}.",
                          fix="Choose another image, or clone that VM to get a copy of its disk.",
                          code="image-in-use")
    fmt = disks.info(p).get("format", "raw")
    if fmt not in IMAGE_FORMATS:
        raise RehomeError(f"{p} is not a disk image QEMU can use (format {fmt}).")
    return Disk(id=disk_id, file=str(p), format=fmt, bus=bus, discard=fmt != "raw")


def new_disk(cfg: VMConfig, size: int, folder: str | os.PathLike | None = None,
             bus: str | None = None, disk_id: str | None = None) -> Disk:
    """Create a new, empty qcow2 disk (not yet added to ``cfg``)."""
    did = disk_id or cfg.next_id("disk", cfg.disks)
    file, owned = new_disk_path(cfg, did, folder)
    path = cfg.resolve(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    disks.create(path, size)
    return Disk(id=did, file=file, format="qcow2", owned=owned,
                bus=bus or (cfg.disks[0].bus if cfg.disks else "virtio"))


def _edit(cfg: VMConfig, change) -> None:
    """Apply ``change`` to a fresh copy of the config with the usual checks, then save."""
    from rehome.vm import edit
    edit.mutate(library.reload(cfg), change)


def add_disk(cfg: VMConfig, *, size: int | None = None, image: str | os.PathLike | None = None,
             folder: str | os.PathLike | None = None, bus: str | None = None) -> Disk:
    """Add a new empty disk (``size``) or an existing image file (used in place)."""
    _require_off(cfg, "add a disk")
    added: list[Disk] = []

    def change(new: VMConfig):
        b = bus or (new.disks[0].bus if new.disks else "virtio")
        if image:
            d = image_disk(image, new.next_id("disk", new.disks), b, new)
        elif size:
            d = new_disk(new, size, folder, b)
        else:
            raise RehomeError("Give a size for a new disk, or an image file.")
        added.append(d)
        if not new.disks:
            d.boot_index = 2 if any(c.boot_index == 1 for c in new.cdroms) else 1
        new.disks.append(d)
    try:
        _edit(cfg, change)
    except BaseException:
        _forget(cfg, added)
        raise
    return added[0]


def replace_disk(cfg: VMConfig, disk_id: str, *, size: int | None = None,
                 image: str | os.PathLike | None = None, folder: str | os.PathLike | None = None,
                 delete_old: bool = False) -> tuple[Disk, str]:
    """Swap a disk for an image file or a new empty disk, keeping its slot, bus and boot order.

    Returns the new disk and a note about the old file (kept, or deleted when
    ``delete_old`` and Rehome created it).
    """
    _require_off(cfg, "replace a disk")
    _require_no_checkpoints(library.reload(cfg))
    swapped: list[tuple[Disk, Disk, Path]] = []

    def change(new: VMConfig):
        old = new.disk(disk_id)
        old_path = new.resolve(old.file)
        if image:
            d = image_disk(image, old.id, old.bus, new)
        elif size:
            d = new_disk(new, size, folder, old.bus, disk_id=old.id)
        else:
            raise RehomeError("Give a size for a new disk, or an image file.")
        swapped.append((old, d, old_path))
        d.boot_index, d.cache, d.serial = old.boot_index, old.cache, old.serial
        new.disks[new.disks.index(old)] = d
    try:
        _edit(cfg, change)
    except BaseException:
        _forget(cfg, [new for _old, new, _path in swapped])
        raise
    old, d, old_path = swapped[0]
    return d, _dispose(cfg, old, old_path, delete_old)


def remove_disk(cfg: VMConfig, disk_id: str, delete_file: bool = False) -> str:
    _require_off(cfg, "remove a disk")
    removed: list[tuple[Disk, Path]] = []

    def change(new: VMConfig):
        old = new.disk(disk_id)
        removed.append((old, new.resolve(old.file)))
        new.disks.remove(old)
    _edit(cfg, change)
    old, path = removed[0]
    return _dispose(cfg, old, path, delete_file)


def _forget(cfg: VMConfig, made: list[Disk]) -> None:
    """Delete new empty disks of a change that was not saved."""
    for d in made:
        if d.format == "qcow2" and created_by_rehome(cfg, d) and not d.source:
            cfg.resolve(d.file).unlink(missing_ok=True)


def _dispose(cfg: VMConfig, old: Disk, path: Path, delete: bool) -> str:
    if delete and created_by_rehome(cfg, old) and not (old.source and old.source.mode == "linked"
                                                         and not is_inside_vm(cfg, old)):
        try:
            path.unlink()
            return f"Deleted the old disk file {path}."
        except FileNotFoundError:
            return ""
    if delete:
        return f"Kept {path}: Rehome only deletes disk files it created."
    return f"The old disk file is kept: {path}"


def flatten_disk(cfg: VMConfig, d: Disk, folder: str | os.PathLike | None = None,
                 progress=None, cancel=None) -> Path:
    """Write ``d`` as a standalone qcow2 file (no backing file) and point ``d`` at it.

    The copy goes to ``folder`` (None: the default folder for new disks, "": the VM's).
    Returns the old file, which the caller deletes once the config is saved.
    """
    old = cfg.resolve(d.file)
    file, owned = new_disk_path(cfg, d.id, folder)
    dst = cfg.resolve(file)
    try:
        disks.convert(str(old), str(dst), src_fmt="qcow2", progress=progress, cancel=cancel)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    d.file, d.owned = file, owned
    return old


def owned_files_outside(cfg: VMConfig) -> list[Path]:
    """Disk files Rehome created outside the VM's folder (deleted with the VM)."""
    return [cfg.resolve(d.file) for d in cfg.disks
            if d.owned and d.file and not is_inside_vm(cfg, d)]
