"""E01 (Expert Witness / EnCase) images through libewf's tools."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

from rehome import paths
from rehome.errors import RehomeError
from rehome.util.proc import run, which


def _tool(name: str) -> str:
    p = which(name)
    if not p:
        raise RehomeError("E01 images need the libewf tools, which are not installed.",
                          fix="Install libewf / ewf-tools (run `rehome doctor`).")
    return p


def media_size(path: str) -> int:
    cp = run([_tool("ewfinfo"), path], what="Reading the E01 image", timeout=120)
    m = re.search(r"Media size:\s*.*?\((\d+) bytes\)", cp.stdout)
    if not m:
        m = re.search(r"Media size:\s*(\d+)", cp.stdout)
    return int(m.group(1)) if m else 0


def mountpoint_for(path: str) -> Path:
    digest = hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16]
    return paths.runtime_dir() / "ewf" / digest


def ensure_mounted(path: str) -> str:
    """Mount the E01 with ewfmount (FUSE, read-only); return the raw image path."""
    mp = mountpoint_for(path)
    raw = mp / "ewf1"
    if raw.exists():
        return str(raw)
    mp.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([_tool("ewfmount"), os.path.abspath(path), str(mp)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 30
    while not raw.exists():
        if proc.poll() not in (None, 0):
            err = proc.stderr.read().decode(errors="replace")
            raise RehomeError("The E01 image could not be opened.", details=err,
                              fix="Make sure FUSE is available (fusermount3) and all segment "
                                  "files (.E01, .E02, ...) are in the same folder.")
        if time.monotonic() > deadline:
            raise RehomeError("Opening the E01 image took too long.")
        time.sleep(0.1)
    return str(raw)


def unmount(path: str) -> None:
    mp = mountpoint_for(path)
    if not (mp / "ewf1").exists():
        return
    for tool in ("fusermount3", "fusermount", "umount"):
        t = which(tool)
        if t and subprocess.run([t, "-u", str(mp)] if "fuse" in tool else [t, str(mp)],
                                capture_output=True).returncode == 0:
            return


def export_raw(path: str, dest: str, progress=None) -> None:
    """Convert an E01 to a raw image (used by Copy imports)."""
    raw = ensure_mounted(path)
    try:
        from rehome.vm import disks
        disks.convert(raw, dest, src_fmt="raw", dst_fmt="qcow2", progress=progress)
    finally:
        unmount(path)
