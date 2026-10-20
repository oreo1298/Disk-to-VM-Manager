"""XDG locations used by Rehome.

All paths can be redirected with ``REHOME_HOME`` (used by the test suite so
tests never touch the real user's VM library).
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = "rehome"


def _xdg(var: str, default: str) -> Path:
    base = os.environ.get("REHOME_HOME")
    if base:
        sub = {"XDG_DATA_HOME": "data", "XDG_CONFIG_HOME": "config",
               "XDG_CACHE_HOME": "cache", "XDG_STATE_HOME": "state"}[var]
        return Path(base) / sub
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value) / APP_DIR
    return Path.home() / default / APP_DIR


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def vms_dir() -> Path:
    return data_dir() / "vms"


def downloads_dir() -> Path:
    return data_dir() / "downloads"


def runtime_dir() -> Path:
    """Per-boot runtime directory for sockets and pidfiles."""
    override = os.environ.get("REHOME_RUNTIME_DIR")
    base = os.environ.get("REHOME_HOME")
    if override:
        p = Path(override)
    elif base:
        p = Path(base) / "run"
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and os.path.isdir(xdg):
            p = Path(xdg) / APP_DIR
        else:
            p = Path("/tmp") / f"{APP_DIR}-{os.getuid()}"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


# Unix socket paths are limited to 107 bytes; our longest name is 20 chars.
MAX_RUNTIME_DIR_LEN = 86


def vm_runtime_dir(vm_id: str) -> Path:
    p = runtime_dir() / "vms" / vm_id.replace("-", "")
    if len(str(p)) > MAX_RUNTIME_DIR_LEN:
        from rehome.errors import RehomeError
        raise RehomeError(
            f"The runtime directory path {p} is too long for the VM's control sockets.",
            fix="Set XDG_RUNTIME_DIR (or REHOME_RUNTIME_DIR) to a shorter path.")
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def source_root() -> Path:
    """Directory containing the ``rehome`` package (and ``data`` when run from git)."""
    return Path(__file__).resolve().parent.parent


def data_files_dir() -> Path:
    """Read-only application data (profiles, catalogs, policy files)."""
    env = os.environ.get("REHOME_DATA_DIR")
    if env:
        return Path(env)
    candidates = [
        source_root() / "data",
        Path(__file__).resolve().parent / "data",
        Path("/usr/share/rehome"),
        Path("/usr/local/share/rehome"),
    ]
    for c in candidates:
        if (c / "profiles").is_dir():
            return c
    return candidates[0]
