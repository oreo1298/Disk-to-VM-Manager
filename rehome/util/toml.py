"""Minimal TOML writer for the subset Rehome stores (reading uses tomllib).

Supports: str, int, float, bool, None (omitted), lists of scalars, nested
tables (dict) and arrays of tables (list of dict).
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _str(k)


def _str(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_scalar(x) for x in v if x is not None) + "]"
    if isinstance(v, dict):
        items = [f"{_key(k)} = {_scalar(x)}" for k, x in v.items() if x is not None]
        return "{ " + ", ".join(items) + " }" if items else "{}"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _is_table_array(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(isinstance(x, dict) for x in v)


def dumps(data: dict) -> str:
    lines: list[str] = []
    _emit_table(data, [], lines)
    return "\n".join(lines).lstrip("\n") + "\n"


def _emit_table(d: dict, path: list[str], lines: list[str]) -> None:
    simple = [(k, v) for k, v in d.items()
              if v is not None and not isinstance(v, dict) and not _is_table_array(v)]
    for k, v in simple:
        lines.append(f"{_key(k)} = {_scalar(v)}")
    for k, v in d.items():
        if isinstance(v, dict):
            sub = path + [k]
            lines.append("")
            lines.append("[" + ".".join(_key(p) for p in sub) + "]")
            _emit_table(v, sub, lines)
    for k, v in d.items():
        if _is_table_array(v):
            sub = path + [k]
            for item in v:
                lines.append("")
                lines.append("[[" + ".".join(_key(p) for p in sub) + "]]")
                _emit_table(item, sub, lines)


def load(path: str | os.PathLike) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def loads(text: str) -> dict:
    return tomllib.loads(text)


def dump_atomic(data: dict, path: str | os.PathLike) -> None:
    """Write TOML atomically (write to temp file, fsync, rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(data)
    # Round-trip check: never persist something we cannot read back.
    tomllib.loads(text)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
