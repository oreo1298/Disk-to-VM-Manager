"""Linked overlays and standalone copies of import sources.

The source is never opened for writing:

* **Linked:** ``disks/<id>.qcow2`` is a qcow2 overlay whose backing file is
  the source. Images are referenced by path; physical disks by the nbdkit
  socket that :class:`SourceAccess` serves (at VM runtime QEMU gets the fd
  directly through an fdset instead).
* **Copy:** ``qemu-img convert`` into a standalone qcow2.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Callable, Iterable

from rehome.disk import ewf, nbd
from rehome.disk.sources import Source
from rehome.errors import RehomeError
from rehome.vm import disks
from rehome.vm.config import Disk, DiskSource, VMConfig


def backing_ref(src: Source) -> tuple[str, str]:
    """(backing reference, backing format) for an overlay of ``src``."""
    if src.kind == "block":
        return nbd.uri(nbd.socket_for(src.stable_path or src.path)), "raw"
    if src.kind == "ewf":
        return str(ewf.mountpoint_for(src.path) / "ewf1"), "raw"
    return src.path, src.format


def to_disk_source(src: Source, mode: str, original_bus: str = "") -> DiskSource:
    return DiskSource(kind=src.kind, path=src.stable_path or src.path, format=src.format,
                      mode=mode, size=src.size, label=src.display, serial=src.serial,
                      original_bus=original_bus or src.transport)


class SourceAccess(contextlib.AbstractContextManager):
    """Make sources readable by offline tools for the duration of a ``with`` block.

    Physical disks: open via the helper and serve over NBD at the stable
    socket path the overlay refers to. E01: mount with ewfmount.
    """

    def __init__(self, sources: Iterable[Source | DiskSource], fd_provider=None):
        self.sources = list(sources)
        self.fd_provider = fd_provider
        self._servers: list[nbd.NbdServer] = []
        self._fds: list[int] = []
        self._ewf: list[str] = []
        self.paths: dict[str, str] = {}   # source path -> path/URI readable now
        self.fds: dict[str, int] = {}     # physical disk path -> the exclusive fd held

    def fd_provider_for_held(self, fallback=None):
        """An fd provider that dups the fds held here.

        A physical disk is opened exclusively (O_EXCL), so while this object
        holds it, the helper cannot open it a second time (e.g. for hashing).
        """
        def provide(path: str) -> int:
            if path in self.fds:
                return os.dup(self.fds[path])
            provider = fallback
            if provider is None:
                from rehome.disk import privileged
                provider = privileged.open_disk_readonly
            return provider(path)
        return provide

    def __enter__(self):
        try:
            for s in self.sources:
                path = getattr(s, "stable_path", "") or s.path
                if s.kind == "block":
                    provider = self.fd_provider
                    if provider is None:
                        from rehome.disk import privileged
                        provider = privileged.open_disk_readonly
                    fd = provider(path)
                    self._fds.append(fd)
                    self.fds[path] = fd
                    sock = nbd.socket_for(path)
                    self._servers.append(nbd.NbdServer(fd, sock).start())
                    self.paths[path] = nbd.uri(sock)
                elif s.kind == "ewf":
                    self.paths[path] = ewf.ensure_mounted(s.path)
                    self._ewf.append(s.path)
                else:
                    self.paths[path] = s.path
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        for srv in self._servers:
            srv.stop()
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        for p in self._ewf:
            ewf.unmount(p)
        self._servers, self._fds, self._ewf = [], [], []
        self.fds = {}
        return False


def for_config(cfg: VMConfig, fd_provider=None) -> SourceAccess:
    srcs = [d.source for d in cfg.disks if d.source and d.source.mode == "linked"]
    return SourceAccess(srcs, fd_provider=fd_provider)


def create_linked(cfg: VMConfig, disk_id: str, src: Source) -> Disk:
    rel = f"disks/{disk_id}.qcow2"
    ref, fmt = backing_ref(src)
    disks.create(cfg.resolve(rel), src.size, backing=ref, backing_fmt=fmt)
    return Disk(id=disk_id, file=rel, format="qcow2", source=to_disk_source(src, "linked"))


def create_copy(cfg: VMConfig, disk_id: str, src: Source, readable_path: str,
                progress: Callable[[float], None] | None = None,
                cancel: Callable[[], bool] | None = None) -> Disk:
    rel = f"disks/{disk_id}.qcow2"
    fmt = "raw" if src.kind in ("block", "ewf") else src.format
    disks.convert(readable_path, str(cfg.resolve(rel)), src_fmt=fmt, progress=progress,
                  cancel=cancel)
    return Disk(id=disk_id, file=rel, format="qcow2", source=to_disk_source(src, "copy"))


def flatten_with_sources(cfg: VMConfig, linked: list[Disk], progress=None, cancel=None) -> None:
    """Turn linked disks into standalone copies (source readable via SourceAccess)."""
    from rehome.vm import library
    with SourceAccess([d.source for d in linked]):
        for i, d in enumerate(linked):
            path = cfg.resolve(d.file)
            tmp = Path(str(path) + ".standalone")

            def prog(frac, i=i):
                if progress:
                    progress((i + frac) / len(linked))
            disks.convert(str(path), str(tmp), src_fmt="qcow2", progress=prog, cancel=cancel)
            os.replace(tmp, path)
            d.source.mode = "copy"
    cfg.snapshots, cfg.current_snapshot = [], ""
    library.save(cfg)


def check_backing_reachable(cfg: VMConfig) -> list[str]:
    """Problems that would stop a linked VM from starting (missing image files...)."""
    problems = []
    for d in cfg.disks:
        s = d.source
        if not s or s.mode != "linked":
            continue
        if s.kind == "image" and not os.path.exists(s.path):
            problems.append(f"{d.id}: the source image {s.path} is not available.")
        if s.kind == "block" and not os.path.exists(os.path.realpath(s.path)):
            problems.append(f"{d.id}: the source disk {s.path} is not connected.")
        if s.kind == "ewf" and not os.path.exists(s.path):
            problems.append(f"{d.id}: the E01 image {s.path} is not available.")
    return problems


def assert_backing_reachable(cfg: VMConfig) -> None:
    problems = check_backing_reachable(cfg)
    if problems:
        raise RehomeError(" ".join(problems),
                          fix="Reconnect the drive or image the VM was imported from (linked "
                              "imports need it), or re-import it in Copy mode.",
                          code="source-missing")
