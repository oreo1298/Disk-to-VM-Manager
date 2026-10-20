"""SHA-256 integrity verification of import sources, with a JSON-lines log."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable

from rehome.errors import Cancelled

CHUNK = 4 * 2**20


def sha256_fd(fd: int, size: int | None = None, progress: Callable[[float], None] | None = None,
              cancel: Callable[[], bool] | None = None) -> str:
    """Hash a file descriptor with positional reads (does not move the offset)."""
    h = hashlib.sha256()
    if size is None:
        size = os.lseek(fd, 0, os.SEEK_END)
    off = 0
    last = 0.0
    while off < size:
        data = os.pread(fd, min(CHUNK, size - off), off)
        if not data:
            break
        h.update(data)
        off += len(data)
        if cancel and cancel():
            raise Cancelled()
        now = time.monotonic()
        if progress and (now - last > 0.2 or off >= size):
            progress(off / size if size else 1.0)
            last = now
    return h.hexdigest()


def sha256_path(path: str, **kw) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        return sha256_fd(fd, **kw)
    finally:
        os.close(fd)


def hash_source(src, progress=None, cancel=None, fd_provider=None) -> str:
    """Hash an import source (rehome.disk.sources.Source).

    Image files are hashed byte-for-byte as stored (for E01 the container file
    itself); physical disks through a read-only fd from the helper.
    """
    if src.kind == "block":
        if fd_provider is None:
            from rehome.disk import privileged
            fd_provider = privileged.open_disk_readonly
        fd = fd_provider(src.stable_path or src.path)
        try:
            return sha256_fd(fd, src.size or None, progress=progress, cancel=cancel)
        finally:
            os.close(fd)
    return sha256_path(src.path, progress=progress, cancel=cancel)


def log_event(log_path: Path, event: str, **data) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "event": event, **data}
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
