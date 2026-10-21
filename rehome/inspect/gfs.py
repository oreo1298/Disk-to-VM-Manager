"""libguestfs handle management and file helpers."""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass

from rehome.errors import RehomeError

log = logging.getLogger(__name__)


@dataclass
class Drive:
    path: str                   # file path or nbd+unix:///?socket=... URI
    format: str = "raw"
    readonly: bool = True
    label: str = ""             # informational only (libguestfs labels are restricted)
    discard: bool = False       # pass TRIM through (fstrim frees space in the image file)


def backend() -> str:
    """How Rehome talks to libguestfs: "bindings", "ctypes" (C library) or "" (unavailable).

    The Python bindings are preferred; without them (Arch ships none) Rehome
    calls libguestfs' C library directly. REHOME_GUESTFS=ctypes forces that.
    """
    if os.environ.get("REHOME_GUESTFS") != "ctypes":
        try:
            import guestfs  # type: ignore  # noqa: F401
            return "bindings"
        except ImportError:
            pass
    from rehome.inspect import guestfs_ctypes
    return "ctypes" if guestfs_ctypes.available() else ""


def new_handle():
    """A libguestfs handle (``guestfs.GuestFS(python_return_dict=True)`` or equivalent)."""
    kind = backend()
    if kind == "bindings":
        import guestfs  # type: ignore
        return guestfs.GuestFS(python_return_dict=True)
    if kind == "ctypes":
        from rehome.inspect import guestfs_ctypes
        return guestfs_ctypes.GuestFS()
    raise RehomeError("libguestfs is not installed, so the installed system cannot be identified "
                      "or patched.", fix="Install libguestfs (run `rehome doctor`).",
                      code="no-libguestfs")


def launch(drives: list[Drive], network: bool = False, memsize: int = 0):
    # Unprivileged users cannot use the libvirt backend's system connection;
    # the direct backend runs the appliance as a child QEMU process.
    os.environ.setdefault("LIBGUESTFS_BACKEND", "direct")
    g = new_handle()
    if os.environ.get("REHOME_GUESTFS_TRACE"):
        g.set_trace(1)
    if memsize:
        g.set_memsize(memsize)
    try:
        g.set_network(network)
    except RuntimeError:
        pass
    if os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK):
        try:
            g.set_smp(min(4, os.cpu_count() or 1))
        except RuntimeError:
            pass
    for d in drives:
        if d.path.startswith("nbd+unix://"):
            sock = d.path.split("socket=", 1)[1]
            g.add_drive_opts("", protocol="nbd", server=[f"unix:{sock}"], format=d.format,
                             readonly=d.readonly)
        elif d.discard:
            g.add_drive_opts(d.path, format=d.format, readonly=d.readonly, discard="besteffort")
        else:
            g.add_drive_opts(d.path, format=d.format, readonly=d.readonly)
    try:
        g.launch()
    except RuntimeError as e:
        msg = str(e)
        fix = "Run `rehome doctor` to check libguestfs."
        if "kernel" in msg.lower() and "read" in msg.lower():
            fix = "libguestfs cannot read your kernel image. Run `rehome doctor` for the fix."
        raise RehomeError("The disk inspection appliance (libguestfs) failed to start.",
                          fix=fix, details=msg, code="guestfs-launch") from e
    return g


@contextlib.contextmanager
def session(drives: list[Drive], **kw):
    g = launch(drives, **kw)
    try:
        yield g
    finally:
        try:
            g.umount_all()
        except RuntimeError:
            pass
        try:
            g.shutdown()
        finally:
            g.close()


def ci_path(g, path: str) -> str | None:
    """Resolve a path case-insensitively (FAT/NTFS); None if it does not exist."""
    try:
        p = g.case_sensitive_path(path)
    except RuntimeError:
        return None
    try:
        return p if g.exists(p) else None
    except RuntimeError:
        return None


def read(g, path: str, limit: int = 4 * 2**20) -> bytes | None:
    p = ci_path(g, path)
    if not p or not g.is_file(p):
        return None
    try:
        size = g.filesize(p)
        if size > limit:
            return g.pread(p, limit, 0)
        return g.read_file(p)
    except RuntimeError:
        return None


def download_bytes(g, path: str, limit: int = 1024 * 2**20) -> bytes | None:
    """Fetch a (possibly large) file through a temporary file on the host."""
    import tempfile
    p = ci_path(g, path)
    if not p or not g.is_file(p) or g.filesize(p) > limit:
        return None
    with tempfile.NamedTemporaryFile(prefix="rehome-gfs-") as tmp:
        try:
            g.download(p, tmp.name)
        except RuntimeError:
            return None
        return open(tmp.name, "rb").read()


def read_text(g, path: str, limit: int = 2**20) -> str | None:
    data = read(g, path, limit)
    if data is None:
        return None
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


@contextlib.contextmanager
def mounted(g, device: str, readonly: bool = True, at: str = "/"):
    """Mount a single filesystem (temporarily)."""
    try:
        if readonly:
            g.mount_ro(device, at)
        else:
            g.mount(device, at)
    except RuntimeError as e:
        raise RehomeError(f"Could not mount {device}.", details=str(e), code="mount-failed") from e
    try:
        yield g
    finally:
        try:
            g.umount(at)
        except RuntimeError:
            g.umount_all()


@contextlib.contextmanager
def os_mounted(g, root: str, readonly: bool = True):
    """Mount an inspected OS's filesystems at their mountpoints."""
    mps = g.inspect_get_mountpoints(root)
    for mp in sorted(mps, key=len):
        try:
            if readonly:
                g.mount_ro(mps[mp], mp)
            else:
                g.mount(mps[mp], mp)
        except RuntimeError as e:
            if mp == "/":
                raise RehomeError(f"The system volume {mps[mp]} could not be mounted.",
                                  details=str(e), code="mount-failed") from e
            log.debug("could not mount %s at %s: %s", mps[mp], mp, e)
    try:
        yield g
    finally:
        g.umount_all()
