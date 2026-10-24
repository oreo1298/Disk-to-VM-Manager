"""The maintenance console's bigger jobs (root): backups to and restores from USB
drives, CD/DVD images from USB drives, the maintenance password.

The console (console.py) asks the questions; this module does the work, so it can
be tested without a screen. Files made here for the VM are given to the "rehome"
user, who owns the VM library.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from rehome.appliance import runtime
from rehome.errors import RehomeError
from rehome.util import toml
from rehome.util.units import human

log = logging.getLogger(__name__)

MIB, GIB = 2**20, 2**30
FAT_LIMIT = 4 * GIB - 1                 # the largest file FAT32 can hold
BACKUP_DIR = "Rehome backups"           # on the USB drive
MARGIN = 256 * MIB                      # left free on the appliance's data partition
OWNER = "rehome"


def give_to_owner(path: Path) -> None:
    subprocess.run(["chown", "-R", f"{OWNER}:{OWNER}", str(path)], check=False,
                   capture_output=True)


def _free(path: Path) -> int:
    return shutil.disk_usage(path).free


# --- backups ------------------------------------------------------------------------------

def backup_size(cfg) -> int:
    """About how big a backup of the VM is: its disks as they are now (without
    checkpoints), plus its UEFI variables and TPM."""
    from rehome.vm import disks
    total = 0
    for d in cfg.disks:
        f = cfg.resolve(d.file)
        try:
            total += disks.measure(f, d.format)
        except RehomeError:
            total += disks.actual_size(f)
    for extra in ("nvram.qcow2", "nvram.json"):
        if (cfg.dir / extra).exists():
            total += (cfg.dir / extra).stat().st_size
    if (cfg.dir / "tpm").is_dir():
        total += sum(f.stat().st_size for f in (cfg.dir / "tpm").rglob("*") if f.is_file())
    return total + MIB                  # the archive's headers and configuration


def backup(cfg, drive: Path, fstype: str, progress=None, work_dir: Path | None = None) -> Path:
    """Back the VM up to a USB drive mounted at ``drive``: a ``.rehome`` file (what
    `rehome export` makes) in its "Rehome backups" folder.

    The disks are converted in ``work_dir`` (on the appliance's data partition) when it
    has room, so the drive needs room for the backup only once.
    """
    from rehome.vm import lifecycle, library
    need = backup_size(cfg)
    if fstype == "vfat" and need > FAT_LIMIT:
        raise RehomeError(
            f"The backup needs about {human(need)}, but this drive uses FAT32, which "
            "cannot hold files over 4 GB.",
            fix="Use a drive formatted as exFAT or NTFS (most new USB drives over 32 GB).")
    folder = drive / BACKUP_DIR
    folder.mkdir(exist_ok=True)
    work = work_dir if work_dir is not None and _free(work_dir) > need + MARGIN else None
    if _free(folder) < need + (0 if work else need) + 16 * MIB:
        raise RehomeError(f"The backup needs about {human(need)}; the drive has "
                          f"{human(_free(folder))} free.",
                          fix="Use a bigger drive, or delete old backups from it.")
    dest = folder / f"{library.slug(cfg.name)}-{time.strftime('%Y%m%d-%H%M%S')}.rehome"
    try:
        lifecycle.export(cfg, str(dest), progress=progress, work_dir=work)
        os.sync()
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


def backups_on(drive: Path) -> list[Path]:
    """Backups (and other Rehome exports) on a USB drive, newest first."""
    found = []
    for d in (drive / BACKUP_DIR, drive):
        try:
            found += [p for p in d.iterdir() if p.suffix.lower() == ".rehome" and p.is_file()]
        except OSError:
            continue
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def backup_info(path: Path) -> dict:
    """``name``, ``exported`` (ISO time, "" if unknown) and ``bytes`` (unpacked) of a backup."""
    try:
        with tarfile.open(path) as tar:
            members = tar.getmembers()
            names = {m.name for m in members}
            if "vm.toml" not in names:
                raise RehomeError(f"{path.name} is not a Rehome backup.")
            vm = toml.loads(tar.extractfile("vm.toml").read().decode())
            meta = json.loads(tar.extractfile("export.json").read()) \
                if "export.json" in names else {}
    except (OSError, tarfile.TarError, ValueError, UnicodeDecodeError) as e:
        raise RehomeError(f"{path.name} could not be read.", details=str(e)) from e
    return {"name": meta.get("name") or vm.get("name", "VM"),
            "exported": meta.get("exported", ""),
            "bytes": sum(m.size for m in members if m.isfile())}


def restore(archive: Path, conf: dict, config_path: Path = runtime.CONFIG):
    """Replace the appliance's VM with the one in ``archive`` (a backup, or any Rehome
    export). The new VM is complete before the old one is deleted. Returns its config."""
    from rehome.vm import library, lifecycle, runner
    info = backup_info(archive)
    if info["bytes"] + MARGIN > _free(library.library_dir()):
        raise RehomeError(f"The backup needs {human(info['bytes'])}; this drive has "
                          f"{human(_free(library.library_dir()))} free.",
                          fix="Delete checkpoints (or CD/DVD images) first.")
    try:
        old = library.find(conf["appliance"]["vm"])
    except RehomeError:
        old = None                      # gone: the backup takes its place
    if old is not None and runner.is_running(old):
        raise RehomeError(f"\"{old.name}\" is running.", fix="Shut it down first.")
    new = lifecycle.import_bundle(str(archive))
    try:
        # CD/DVD and floppy images are not in backups: the ones the appliance has stay.
        media = {}
        for dev in list(new.cdroms) + list(new.floppies):
            src = old.dir / dev.file if old is not None and dev.file else None
            if src is not None and src.parent == old.dir / "media" and src.is_file():
                (new.dir / "media").mkdir(exist_ok=True)
                shutil.copy2(src, new.dir / "media" / src.name)
                media[dev.id] = f"media/{src.name}"
        runtime.fit_vm(new, network=conf.get("network", {}).get("mode", "nat"), media=media)
        library.save(new)
        conf["appliance"].update(vm=new.id, name=new.name)
        toml.dump_atomic(conf, config_path)
    except BaseException:
        shutil.rmtree(new.dir, ignore_errors=True)
        raise
    if old is not None:
        try:
            library.delete(old)
        except (RehomeError, OSError) as e:
            log.warning("the replaced VM could not be deleted: %s", e)
    if new.name != info["name"] and not library.name_taken(info["name"], new.id):
        new.name = info["name"]         # imported as "Name (2)" next to the old one
        library.save(new)
        conf["appliance"]["name"] = new.name
        toml.dump_atomic(conf, config_path)
    give_to_owner(new.dir)
    return new


# --- CD/DVD images ---------------------------------------------------------------------------

def images_on(drive: Path, suffixes=(".iso",), depth: int = 2, limit: int = 40) -> list[Path]:
    """Disc images on a USB drive (its top folders too), sorted by path."""
    found: list[Path] = []

    def walk(d: Path, level: int):
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entries:
            if len(found) >= limit:
                return
            if p.name.startswith("."):
                continue
            if p.is_file() and p.suffix.lower() in suffixes:
                found.append(p)
            elif p.is_dir() and level < depth:
                walk(p, level + 1)
    walk(drive, 1)
    return found


def copy_image(src: Path, cfg, progress=None) -> Path:
    """Copy a disc image into the VM's media folder (the USB drive can be unplugged after)."""
    size = src.stat().st_size
    media = cfg.dir / "media"
    media.mkdir(exist_ok=True)
    dest = media / src.name
    have = dest.stat().st_size if dest.exists() else 0
    if size + MARGIN > _free(media) + have:
        raise RehomeError(f"{src.name} needs {human(size)}; this drive has "
                          f"{human(_free(media))} free.",
                          fix="Delete checkpoints or other CD/DVD images first.")
    tmp = media / f".{src.name}.copying"
    try:
        with open(src, "rb") as s, open(tmp, "wb") as d:
            done = 0
            while chunk := s.read(4 * MIB):
                d.write(chunk)
                done += len(chunk)
                if progress and size:
                    progress(done / size)
            d.flush()
            os.fsync(d.fileno())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    give_to_owner(media)
    return dest


def images_in_use(cfg) -> set[Path]:
    return {cfg.resolve(dev.file) for dev in list(cfg.cdroms) + list(cfg.floppies) if dev.file}


# --- the maintenance password -----------------------------------------------------------------

def set_password(password: str, conf: dict, root: Path = Path("/"),
                 config_path: Path = runtime.CONFIG) -> None:
    """From the next time it opens, the maintenance console asks for this password
    (a login as root, also on serial consoles). ``root``: the system's files (tests)."""
    if not password or "\n" in password:
        raise RehomeError("The password must not be empty.")
    r = subprocess.run(["chpasswd"], input=f"root:{password}\n", text=True,
                       capture_output=True, check=False)
    if r.returncode:
        raise RehomeError("The password could not be set.", details=r.stderr)
    unit = root / runtime.PASSWORD_UNIT
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(runtime.PASSWORD_UNIT_TEXT)
    (root / runtime.ROOT_PROFILE).write_text(runtime.ROOT_PROFILE_TEXT)
    _reload_units(root)
    conf.setdefault("appliance", {})["password"] = True
    toml.dump_atomic(conf, config_path)


def remove_password(conf: dict, root: Path = Path("/"), config_path: Path = runtime.CONFIG) -> None:
    """The maintenance console opens without a password again (root cannot log in)."""
    subprocess.run(["passwd", "-l", "root"], capture_output=True, check=False)
    (root / runtime.PASSWORD_UNIT).unlink(missing_ok=True)
    _reload_units(root)
    conf.setdefault("appliance", {})["password"] = False
    toml.dump_atomic(conf, config_path)


def has_password(root: Path = Path("/")) -> bool:
    return (root / runtime.PASSWORD_UNIT).exists()


def _reload_units(root: Path) -> None:
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, check=False)
