"""Offline Windows registry access through libguestfs' hivex API.

Paths use backslashes relative to the hive root, e.g.
``ControlSet001\\Services\\storahci``. Writes are recorded in
:attr:`Hive.changes` so the patch log can show old and new values.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from rehome.errors import RehomeError
from rehome.inspect.gfs import ci_path

REG_NONE, REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ, REG_QWORD = 0, 1, 2, 3, 4, 7, 11


@dataclass
class Change:
    op: str          # set / delete-value / add-key / delete-key
    path: str
    name: str = ""
    old: object = None
    new: object = None

    def describe(self) -> str:
        if self.op == "set":
            return f"{self.path}\\{self.name}: {self.old!r} → {self.new!r}"
        if self.op == "delete-key":
            return f"deleted key {self.path}"
        if self.op == "add-key":
            return f"created key {self.path}"
        return f"deleted value {self.path}\\{self.name}"


def decode(t: int, raw: bytes):
    if t in (REG_SZ, REG_EXPAND_SZ):
        return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
    if t == REG_DWORD and len(raw) >= 4:
        return struct.unpack("<I", raw[:4])[0]
    if t == REG_QWORD and len(raw) >= 8:
        return struct.unpack("<Q", raw[:8])[0]
    if t == REG_MULTI_SZ:
        return [s for s in raw.decode("utf-16-le", errors="replace").split("\x00") if s]
    return raw


def encode(value, t: int | None = None) -> tuple[int, bytes]:
    if t is None:
        t = REG_DWORD if isinstance(value, int) else REG_MULTI_SZ if isinstance(value, list) \
            else REG_BINARY if isinstance(value, (bytes, bytearray)) else REG_SZ
    if t in (REG_SZ, REG_EXPAND_SZ):
        return t, (str(value) + "\x00").encode("utf-16-le")
    if t == REG_DWORD:
        return t, struct.pack("<I", int(value) & 0xFFFFFFFF)
    if t == REG_QWORD:
        return t, struct.pack("<Q", int(value))
    if t == REG_MULTI_SZ:
        return t, ("\x00".join(value) + "\x00\x00").encode("utf-16-le")
    return t, bytes(value)


class Hive:
    def __init__(self, g, path: str, write: bool = False):
        real = ci_path(g, path)
        if not real:
            raise RehomeError(f"The registry hive {path} was not found.", code="no-hive")
        self.g = g
        self.path = real
        self.write = write
        self.changes: list[Change] = []
        self._open = False

    def __enter__(self):
        self.g.hivex_open(self.path, write=self.write)
        self._open = True
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.write and self.changes and exc_type is None:
                self.g.hivex_commit(None)
        finally:
            self.g.hivex_close()
            self._open = False
        return False

    # --- navigation ------------------------------------------------------------------

    def node(self, path: str, create: bool = False) -> int | None:
        g = self.g
        node = g.hivex_root()
        walked = []
        for part in [p for p in path.split("\\") if p]:
            child = g.hivex_node_get_child(node, part)
            if not child:
                # hivex lookups are case-insensitive already; create if asked.
                if not create:
                    return None
                child = g.hivex_node_add_child(node, part)
                self.changes.append(Change("add-key", "\\".join(walked + [part])))
            walked.append(part)
            node = child
        return node

    def exists(self, path: str) -> bool:
        return self.node(path) is not None

    def subkeys(self, path: str) -> list[str]:
        n = self.node(path)
        if n is None:
            return []
        return [self.g.hivex_node_name(c["hivex_node_h"]) for c in self.g.hivex_node_children(n)]

    def values(self, path: str) -> dict:
        n = self.node(path)
        if n is None:
            return {}
        out = {}
        for v in self.g.hivex_node_values(n):
            h = v["hivex_value_h"]
            out[self.g.hivex_value_key(h)] = decode(self.g.hivex_value_type(h),
                                                   self.g.hivex_value_value(h))
        return out

    def get(self, path: str, name: str, default=None):
        n = self.node(path)
        if n is None:
            return default
        v = self.g.hivex_node_get_value(n, name)
        if not v:
            return default
        return decode(self.g.hivex_value_type(v), self.g.hivex_value_value(v))

    # --- modification ----------------------------------------------------------------

    def set(self, path: str, name: str, value, t: int | None = None) -> bool:
        """Set a value; returns True if it changed."""
        old = self.get(path, name)
        t, raw = encode(value, t)
        if old == decode(t, raw):
            return False
        n = self.node(path, create=True)
        self.g.hivex_node_set_value(n, name, t, raw)
        self.changes.append(Change("set", path, name, old, decode(t, raw)))
        return True

    def delete_key(self, path: str) -> bool:
        n = self.node(path)
        if n is None:
            return False
        self.g.hivex_node_delete_child(n)
        self.changes.append(Change("delete-key", path))
        return True


def control_set(g, root: str) -> str:
    try:
        return g.inspect_get_windows_current_control_set(root)
    except RuntimeError:
        return "ControlSet001"


def hive_paths(g, root: str) -> tuple[str, str]:
    """(SYSTEM, SOFTWARE) hive paths of an inspected Windows root."""
    try:
        return (g.inspect_get_windows_system_hive(root), g.inspect_get_windows_software_hive(root))
    except RuntimeError:
        sysroot = g.inspect_get_windows_systemroot(root)
        return (f"{sysroot}/System32/config/SYSTEM", f"{sysroot}/System32/config/SOFTWARE")
