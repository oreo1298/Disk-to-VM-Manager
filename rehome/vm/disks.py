"""qemu-img helpers: create, inspect, convert and snapshot disk images."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from rehome.errors import Cancelled, RehomeError
from rehome.host.qemu import qemu_img
from rehome.util.proc import quote, run


def _img() -> str:
    p = qemu_img()
    if not p:
        raise RehomeError("qemu-img is not installed.", fix="Install the QEMU tools (run `rehome doctor`).")
    return p


def info(path: str | os.PathLike, fmt: str | None = None, backing_chain: bool = False,
         force_share: bool = True) -> dict | list:
    argv = [_img(), "info", "--output=json"]
    if force_share:
        argv.append("-U")
    if fmt:
        argv += ["-f", fmt]
    if backing_chain:
        argv.append("--backing-chain")
    argv.append(str(path))
    cp = run(argv, what="Reading the disk image", timeout=120)
    return json.loads(cp.stdout)


def info_no_backing(path: str | os.PathLike) -> dict:
    """Inspect a qcow2 without opening its backing file (which may be offline)."""
    spec = json.dumps({"driver": "qcow2", "file": {"driver": "file", "filename": str(path)},
                       "backing": None})
    cp = run([_img(), "info", "--output=json", "-U", f"json:{spec}"],
             what="Reading the disk image", timeout=60)
    return json.loads(cp.stdout)


def create(path: str | os.PathLike, size: int, fmt: str = "qcow2", backing: str | None = None,
           backing_fmt: str | None = None, preallocation: str | None = None) -> None:
    argv = [_img(), "create", "-q", "-f", fmt]
    opts = []
    if fmt == "qcow2":
        opts += ["cluster_size=65536", "lazy_refcounts=on"]
    if preallocation:
        opts.append(f"preallocation={preallocation}")
    if opts:
        argv += ["-o", ",".join(opts)]
    if backing:
        # -u: do not open the backing file (it may be a raw device served
        # only at VM runtime); the size is given explicitly.
        argv += ["-u", "-b", backing, "-F", backing_fmt or "raw"]
    argv += [str(path), str(size)]
    run(argv, what="Creating a disk image")


def resize(path: str | os.PathLike, size: int) -> None:
    run([_img(), "resize", "-q", str(path), str(size)], what="Resizing the disk image")


def convert(src: str, dst: str, src_fmt: str | None = None, dst_fmt: str = "qcow2",
            compress: bool = False, progress: Callable[[float], None] | None = None,
            cancel: Callable[[], bool] | None = None, image_opts: bool = False) -> None:
    """``qemu-img convert`` with progress reporting and cancellation."""
    argv = [_img(), "convert", "-p", "-O", dst_fmt]
    if image_opts:
        argv.append("--image-opts")
    elif src_fmt:
        argv += ["-f", src_fmt]
    if compress:
        argv.append("-c")
    if dst_fmt == "qcow2":
        argv += ["-o", "cluster_size=65536"]
    argv += ["-U", src, dst] if not image_opts else [src, dst]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
    buf = b""
    pct_re = re.compile(rb"\((\d+(?:\.\d+)?)/100%\)")
    try:
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(64)
            if not chunk:
                break
            buf = (buf + chunk)[-256:]
            m = None
            for m in pct_re.finditer(buf):
                pass
            if m and progress:
                progress(float(m.group(1)) / 100.0)
            if cancel and cancel():
                proc.terminate()
                proc.wait(timeout=10)
                Path(dst).unlink(missing_ok=True)
                raise Cancelled()
        ret = proc.wait()
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        raise
    if ret != 0:
        err = proc.stderr.read().decode(errors="replace")
        Path(dst).unlink(missing_ok=True)
        raise RehomeError("Copying the disk image failed.", details=f"$ {quote(argv)}\n{err}")
    if progress:
        progress(1.0)


def rebase_unsafe(path: str, backing: str, backing_fmt: str) -> None:
    run([_img(), "rebase", "-u", "-b", backing, "-F", backing_fmt, path],
        what="Updating the disk's source reference")


def _snap_target(path: str, no_backing: bool) -> list[str]:
    if no_backing:
        spec = json.dumps({"driver": "qcow2", "file": {"driver": "file", "filename": str(path)},
                           "backing": None})
        return [f"json:{spec}"]
    return [str(path)]


def snapshot_create(path: str, tag: str) -> None:
    run([_img(), "snapshot", "-c", tag, *_snap_target(path, True)],
        what="Creating a checkpoint")


def snapshot_apply(path: str, tag: str) -> None:
    run([_img(), "snapshot", "-a", tag, *_snap_target(path, True)],
        what="Reverting to a checkpoint")


def snapshot_delete(path: str, tag: str) -> None:
    run([_img(), "snapshot", "-d", tag, *_snap_target(path, True)],
        what="Deleting a checkpoint")


def snapshot_list(path: str) -> list[dict]:
    try:
        data = info_no_backing(path)
    except RehomeError:
        return []
    return data.get("snapshots", [])


def actual_size(path: str | os.PathLike) -> int:
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0


def wait_unlocked(path: str, timeout: float = 10) -> None:
    """Wait until no process holds a write lock on the image."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cp = run([_img(), "info", str(path)], check=False, timeout=30)
        if cp.returncode == 0:
            return
        time.sleep(0.3)
