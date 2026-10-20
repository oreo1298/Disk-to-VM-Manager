"""Subprocess helpers with consistent error reporting."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from functools import lru_cache
from typing import Iterable, Sequence

from rehome.errors import RehomeError

log = logging.getLogger(__name__)

# Locations of helper binaries that distros keep outside $PATH.
EXTRA_DIRS = (
    "/usr/libexec", "/usr/lib/qemu", "/usr/libexec/qemu", "/usr/lib",
    "/usr/local/libexec", "/usr/sbin", "/sbin",
)


@lru_cache(maxsize=None)
def which(name: str, extra: tuple[str, ...] = ()) -> str | None:
    """Find an executable in $PATH, then in well-known libexec dirs."""
    found = shutil.which(name)
    if found:
        return found
    for d in extra + EXTRA_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def quote(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(c)) for c in cmd)


def run(cmd: Sequence[str], *, check: bool = True, timeout: float | None = 120,
        input: str | bytes | None = None, env: dict | None = None,
        text: bool = True, cwd: str | None = None,
        what: str | None = None, fix: str | None = None) -> subprocess.CompletedProcess:
    """Run a command, capturing output.

    On failure raises :class:`RehomeError` whose ``details`` contain the full
    command line, exit status and stderr.
    """
    cmd = [str(c) for c in cmd]
    log.debug("run: %s", quote(cmd))
    full_env = None
    if env is not None:
        full_env = os.environ.copy()
        full_env.update(env)
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input,
                            env=full_env, text=text, cwd=cwd)
    except FileNotFoundError as e:
        raise RehomeError(
            f"{what or 'A required program'} is not installed ({cmd[0]}).",
            fix=fix or "Run `rehome doctor` to see which packages to install.",
            details=str(e), code="missing-program") from e
    except subprocess.TimeoutExpired as e:
        raise RehomeError(
            f"{what or 'A command'} took too long and was stopped.",
            fix=fix, details=f"$ {quote(cmd)}\ntimeout after {timeout}s",
            code="timeout") from e
    if check and cp.returncode != 0:
        raise RehomeError(
            f"{what or 'A command'} failed.", fix=fix,
            details=f"$ {quote(cmd)}\nexit status {cp.returncode}\n"
                    f"{_tail(cp.stderr)}{_tail(cp.stdout, 'stdout')}",
            code="command-failed")
    return cp


def output(cmd: Sequence[str], **kw) -> str:
    """Run a command and return stdout ('' if it cannot be run)."""
    try:
        cp = run(cmd, check=False, **kw)
    except RehomeError:
        return ""
    return cp.stdout if cp.returncode == 0 else ""


def _tail(s, label="stderr", limit=4000) -> str:
    if not s:
        return ""
    if isinstance(s, bytes):
        s = s.decode(errors="replace")
    s = s.strip()
    if len(s) > limit:
        s = "…" + s[-limit:]
    return f"--- {label} ---\n{s}\n"


def is_root() -> bool:
    return os.geteuid() == 0


def first_existing(paths: Iterable[str]) -> str | None:
    for p in paths:
        if os.path.exists(p):
            return p
    return None
