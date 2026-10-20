"""Synchronous QMP client with file-descriptor passing.

Thread-safe for concurrent command execution from one connection (a lock
serialises request/response pairs). Events are queued and can be consumed
with :meth:`events` / :meth:`wait_event`, or delivered to a callback.
"""

from __future__ import annotations

import collections
import itertools
import json
import socket
import threading
import time
from typing import Any, Callable

from rehome.errors import RehomeError


class QmpError(RehomeError):
    def __init__(self, cmd: str, err: dict):
        self.cls = err.get("class", "GenericError")
        self.desc = err.get("desc", "")
        super().__init__(f"QEMU rejected the request ({cmd}): {self.desc}",
                         details=json.dumps(err), code="qmp-error")


class QmpClosed(RehomeError):
    def __init__(self, msg: str = "The connection to the VM was closed (the VM probably stopped)."):
        super().__init__(msg, code="qmp-closed")


class QMP:
    def __init__(self, path: str, timeout: float = 30.0,
                 on_event: Callable[[dict], None] | None = None):
        self.path = path
        self.timeout = timeout
        self.on_event = on_event
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=1000)
        self._ids = itertools.count(1)
        self.greeting: dict = {}

    # --- connection ---------------------------------------------------------

    def connect(self, retries: float = 0.0) -> "QMP":
        deadline = time.monotonic() + retries
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(self.timeout)
                s.connect(self.path)
                break
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as e:
                s.close()
                if time.monotonic() >= deadline:
                    raise QmpClosed(f"Could not connect to the VM's control socket ({e}).") from e
                time.sleep(0.1)
        self._sock = s
        self.greeting = self._read_message()
        if "QMP" not in self.greeting:
            raise QmpClosed("Unexpected greeting from QEMU.")
        self.execute("qmp_capabilities")
        return self

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        return self.connect() if self._sock is None else self

    def __exit__(self, *exc):
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # --- I/O ----------------------------------------------------------------

    def _read_message(self, timeout: float | None = None) -> dict:
        assert self._sock is not None
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            while b"\n" not in self._buf:
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    raise
                except OSError as e:
                    raise QmpClosed() from e
                if not chunk:
                    raise QmpClosed()
                self._buf += chunk
            line, _, self._buf = self._buf.partition(b"\n")
            return json.loads(line)
        finally:
            if timeout is not None:
                self._sock.settimeout(self.timeout)

    def _dispatch_event(self, msg: dict) -> None:
        self._events.append(msg)
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:  # never let a UI callback break QMP
                pass

    def execute(self, cmd: str, args: dict | None = None, fds: list[int] | None = None,
                timeout: float | None = None) -> Any:
        if self._sock is None:
            raise QmpClosed()
        req_id = f"r{next(self._ids)}"
        msg: dict = {"execute": cmd, "id": req_id}
        if args:
            msg["arguments"] = args
        data = json.dumps(msg).encode() + b"\n"
        with self._lock:
            try:
                if fds:
                    socket.send_fds(self._sock, [data], fds)
                else:
                    self._sock.sendall(data)
            except OSError as e:
                self.close()
                raise QmpClosed() from e
            deadline = time.monotonic() + (timeout or self.timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RehomeError(f"The VM did not answer in time ({cmd}).", code="qmp-timeout")
                try:
                    reply = self._read_message(timeout=remaining)
                except socket.timeout:
                    continue
                if "event" in reply:
                    self._dispatch_event(reply)
                    continue
                if reply.get("id") not in (req_id, None):
                    continue
                if "error" in reply:
                    raise QmpError(cmd, reply["error"])
                return reply.get("return")

    def poll_events(self, timeout: float = 0.0) -> list[dict]:
        """Read any pending events (blocking up to ``timeout`` for the first)."""
        if self._sock is None:
            raise QmpClosed()
        with self._lock:
            try:
                while True:
                    msg = self._read_message(timeout=max(timeout, 0.001))
                    if "event" in msg:
                        self._dispatch_event(msg)
                    timeout = 0.001
            except socket.timeout:
                pass
        out = list(self._events)
        self._events.clear()
        return out

    def wait_event(self, name: str, timeout: float = 30.0,
                   match: Callable[[dict], bool] | None = None) -> dict:
        deadline = time.monotonic() + timeout
        pending: list[dict] = []
        try:
            while time.monotonic() < deadline:
                for ev in self.poll_events(timeout=min(0.5, deadline - time.monotonic())):
                    if ev.get("event") == name and (match is None or match(ev)):
                        return ev
                    pending.append(ev)
            raise RehomeError(f"Timed out waiting for the VM ({name}).", code="qmp-timeout")
        finally:
            # Put back events we did not consume.
            self._events.extend(pending)

    # --- helpers ------------------------------------------------------------

    def status(self) -> str:
        return self.execute("query-status")["status"]

    def wait_job(self, job_id: str, timeout: float = 3600,
                 progress: Callable[[int, int], None] | None = None) -> None:
        """Wait for a QMP job to conclude, reporting progress; raises on failure."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = {j["id"]: j for j in self.execute("query-jobs")}
            job = jobs.get(job_id)
            if job is None:
                return
            if progress:
                progress(job.get("current-progress", 0), job.get("total-progress", 0))
            if job["status"] == "concluded":
                err = job.get("error")
                self.execute("job-dismiss", {"id": job_id})
                if err:
                    raise RehomeError(f"The VM operation failed: {err}", code="job-failed")
                return
            self.poll_events(timeout=0.2)
        raise RehomeError("The VM operation took too long.", code="job-timeout")

    def send_fd(self, fd: int, fdname: str) -> None:
        self.execute("getfd", {"fdname": fdname}, fds=[fd])

    def add_fd(self, fd: int, fdset_id: int | None = None, opaque: str = "") -> int:
        args: dict = {}
        if fdset_id is not None:
            args["fdset-id"] = fdset_id
        if opaque:
            args["opaque"] = opaque
        ret = self.execute("add-fd", args, fds=[fd])
        return ret["fdset-id"]

    def screendump(self, filename: str, device: str | None = None, fmt: str = "png") -> None:
        args = {"filename": filename, "format": fmt}
        if device:
            args["device"] = device
        try:
            self.execute("screendump", args)
        except QmpError as e:
            if "format" in e.desc:
                args.pop("format")
                self.execute("screendump", args)
            else:
                raise

    def hmp(self, command: str) -> str:
        return self.execute("human-monitor-command", {"command-line": command})
