"""Client for the privileged helper (pkexec + rehome-helper).

The GUI and CLI never run as root. When they need a physical disk or a USB
device, the helper opens it (read-only and exclusive for disks) and passes
the file descriptor back over a socketpair.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
from pathlib import Path

from rehome.errors import Cancelled, RehomeError, UnsafeSource
from rehome.host import tools
from rehome.util.proc import which

log = logging.getLogger(__name__)

SOURCE_HELPER = Path(__file__).resolve().parent.parent / "helper" / "rehome-helper"


def helper_command() -> list[str]:
    env = os.environ.get("REHOME_HELPER")
    if os.geteuid() == 0:
        # Already privileged (CLI under sudo, containers, tests): no pkexec.
        path = env or tools.helper_installed() or str(SOURCE_HELPER)
        return ["/usr/bin/python3", "-I", path] if path.endswith("rehome-helper") \
            and not os.access(path, os.X_OK) else [path]
    installed = tools.helper_installed()
    if not installed:
        raise RehomeError(
            "Rehome's permission helper is not installed, so physical disks and USB devices "
            "cannot be opened.",
            fix="Install Rehome with install.sh or your distribution's package. Disk images "
                "work without it.", code="no-helper")
    pkexec = which("pkexec")
    if not pkexec:
        raise RehomeError("pkexec (polkit) is not installed.",
                          fix="Install polkit (run `rehome doctor`).", code="no-pkexec")
    return [pkexec, installed]


def _call(args: list[str], want_fd: bool, timeout: float = 300) -> tuple[dict, int | None]:
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        argv = helper_command() + args
        log.debug("helper: %s", argv)
        proc = subprocess.Popen(argv, stdin=theirs.fileno(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, close_fds=True)
        theirs.close()
        fd = None
        msg = b""
        if want_fd:
            ours.settimeout(timeout)
            try:
                msg, fds, _flags, _addr = socket.recv_fds(ours, 65536, 1)
            except socket.timeout:
                proc.kill()
                raise Cancelled("Timed out waiting for permission.")
            fd = fds[0] if fds else None
        out, err = proc.communicate(timeout=timeout)
    finally:
        ours.close()
    text = (msg or out or b"").decode(errors="replace").strip().splitlines()
    reply: dict = {}
    for line in reversed(text):
        try:
            reply = json.loads(line)
            break
        except ValueError:
            continue
    if proc.returncode in (126, 127) and not reply:
        # pkexec: 126 = authorization dismissed/denied, 127 = not authorized.
        if fd is not None:
            os.close(fd)
        raise Cancelled("Permission was not granted.")
    if not reply.get("ok"):
        if fd is not None:
            os.close(fd)
        code = reply.get("code", "helper-failed")
        message = reply.get("error") or "The permission helper failed."
        if code in ("host-disk", "mounted", "in-use", "busy"):
            raise UnsafeSource(message, code=code, fix=_unsafe_fix(code))
        raise RehomeError(message, code=code,
                          details=(err or b"").decode(errors="replace")[-2000:])
    if want_fd and fd is None:
        raise RehomeError("The permission helper did not return the device.")
    return reply, fd


def _unsafe_fix(code: str) -> str:
    return {
        "host-disk": "Rehome never imports the disk your computer is running from. Boot from "
                     "another system, or image the disk to a file first.",
        "mounted": "Unmount every partition of this disk (for example in your file manager), "
                   "then try again.",
        "in-use": "Stop whatever uses the disk (RAID array, LVM volume group, opened encrypted "
                  "volume), then try again.",
        "busy": "Close programs that use this disk and unmount its partitions, then try again.",
    }.get(code, "")


def open_disk_readonly(path: str) -> int:
    _reply, fd = _call(["open-disk", path], want_fd=True)
    return fd  # type: ignore[return-value]


def open_usb(bus: int, devnum: int) -> int:
    _reply, fd = _call(["open-usb", str(bus), str(devnum)], want_fd=True)
    return fd  # type: ignore[return-value]


def smart(path: str) -> dict:
    reply, _ = _call(["smart", path], want_fd=False, timeout=180)
    return reply


def check_disk(path: str) -> dict:
    reply, _ = _call(["check-disk", path], want_fd=False, timeout=60)
    return reply
