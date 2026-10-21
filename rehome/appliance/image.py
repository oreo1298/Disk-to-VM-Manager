"""Helpers for working inside an appliance image with libguestfs."""

from __future__ import annotations

import os
import shlex
import tarfile
import tempfile
import threading
from pathlib import Path

from rehome.errors import RehomeError

# What the build scripts run with inside the image (a chroot of its root).
SCRIPT_ENV = ("export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8 "
              "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n")


def run(g, script: str, what: str) -> str:
    """Run a shell script inside the image; RehomeError with its output if it fails."""
    g.write("/tmp/rehome-step.sh", (SCRIPT_ENV + "set -e\n" + script + "\n").encode())
    out = g.sh("sh /tmp/rehome-step.sh > /tmp/rehome-step.log 2>&1; echo $?").strip()
    log = g.read_file("/tmp/rehome-step.log").decode("utf-8", "replace")
    g.rm_rf("/tmp/rehome-step.sh")
    g.rm_rf("/tmp/rehome-step.log")
    if out.splitlines()[-1:] != ["0"]:
        raise RehomeError(f"Building the appliance failed while {what}.",
                          details=log[-6000:], code="appliance-build")
    return log


def tar_in(g, members: list[tuple[Path, str]], dest: str) -> None:
    """Copy host files into the image at ``dest`` (``members``: (host path, path in image)).

    The tar stream is written into a FIFO that libguestfs reads, so nothing
    is staged on the host's disk. Symbolic links are followed.
    """
    g.mkdir_p(dest)
    with tempfile.TemporaryDirectory(prefix="rehome-tar-") as tmp:
        fifo = os.path.join(tmp, "stream.tar")
        os.mkfifo(fifo, 0o600)
        error: list[BaseException] = []

        def writer():
            try:
                with open(fifo, "wb") as f, tarfile.open(fileobj=f, mode="w|",
                                                         dereference=True,
                                                         format=tarfile.GNU_FORMAT) as tar:
                    for host, arc in members:
                        info = tar.gettarinfo(str(host), arcname=arc)
                        info.uid = info.gid = 0
                        info.uname = info.gname = "root"
                        if info.isfile():
                            with open(host, "rb") as src:
                                tar.addfile(info, src)
                        else:
                            tar.addfile(info)
            except BaseException as e:  # noqa: BLE001 - reported below
                error.append(e)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            g.tar_in(fifo, dest)
        except RuntimeError as e:
            _unblock(fifo, t)
            raise RehomeError("Copying files into the appliance image failed.",
                              details=str(e), code="appliance-build") from e
        t.join()
        if error:
            raise RehomeError("Copying files into the appliance image failed.",
                              details=str(error[0]), code="appliance-build")


def _unblock(fifo: str, t: threading.Thread) -> None:
    """Let a writer blocked on a FIFO nobody reads finish (it then fails with EPIPE)."""
    try:
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass
    t.join(timeout=5)


def tree_members(root: Path, arc_root: str, skip=lambda p: False) -> list[tuple[Path, str]]:
    """Members for ``tar_in`` copying a host directory tree."""
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if skip(rel) or p.is_symlink() and not p.exists():
            continue
        if p.is_dir() or p.is_file():
            out.append((p, f"{arc_root}/{rel}".lstrip("/") or "."))
    return out


def quote(s: str) -> str:
    return shlex.quote(s)
