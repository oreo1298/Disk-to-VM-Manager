"""Serve a read-only file descriptor over NBD with nbdkit.

Offline tools (libguestfs, qemu-img, virt-v2v) need a *path* for a physical
disk, but only the helper can open it. nbdkit's file plugin takes the fd
(``file fd=N``) and exports it read-only on a per-user Unix socket, which is
also what linked-import overlays name as their backing file.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import time
from pathlib import Path

from rehome import paths
from rehome.errors import RehomeError
from rehome.util.proc import which


def socket_for(stable_path: str) -> str:
    """Stable socket path for a source, used in overlay backing references."""
    digest = hashlib.sha256(stable_path.encode()).hexdigest()[:16]
    d = paths.runtime_dir() / "src"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(d / f"{digest}.sock")


def uri(sock: str) -> str:
    return f"nbd+unix:///?socket={sock}"


class NbdServer:
    def __init__(self, fd: int, sock: str, size: int | None = None):
        self.fd = fd
        self.sock = sock
        self.proc: subprocess.Popen | None = None

    def start(self) -> "NbdServer":
        nbdkit = which("nbdkit")
        if not nbdkit:
            raise RehomeError("nbdkit is not installed, so the physical disk cannot be read by "
                              "the inspection tools.", fix="Install nbdkit (run `rehome doctor`).")
        Path(self.sock).unlink(missing_ok=True)
        os.set_inheritable(self.fd, True)
        argv = [nbdkit, "--foreground", "--readonly", "--exit-with-parent",
                "--unix", self.sock, "file", f"fd={self.fd}"]
        self.proc = subprocess.Popen(argv, pass_fds=[self.fd], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while not os.path.exists(self.sock):
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
                raise RehomeError("nbdkit could not serve the disk.", details=err,
                                  fix="nbdkit 1.34 or newer is required (for the fd= option).")
            if time.monotonic() > deadline:
                self.stop()
                raise RehomeError("nbdkit did not start in time.")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        Path(self.sock).unlink(missing_ok=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
