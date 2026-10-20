"""Human-friendly size parsing and formatting."""

from __future__ import annotations

import re

_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1000, "kib": 1024,
    "m": 1024**2, "mb": 1000**2, "mib": 1024**2,
    "g": 1024**3, "gb": 1000**3, "gib": 1024**3,
    "t": 1024**4, "tb": 1000**4, "tib": 1024**4,
}


def parse_size(text: str | int, default_unit: str = "") -> int:
    """Parse ``"4G"``, ``"512 MiB"``, ``"1.5TB"`` or a plain integer into bytes.

    Bare single-letter units are binary (``4G`` = 4 GiB), matching QEMU.
    """
    if isinstance(text, int):
        return text * _UNITS[default_unit.lower()]
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*", str(text))
    if not m:
        raise ValueError(f"not a size: {text!r}")
    num, unit = m.groups()
    unit = (unit or default_unit).lower()
    if unit not in _UNITS:
        raise ValueError(f"unknown size unit in {text!r}")
    return int(float(num) * _UNITS[unit])


def mib(text: str | int) -> int:
    """Parse a size, treating bare numbers as MiB."""
    return parse_size(text, "m") // 1024**2


def human(n: float, binary: bool = True) -> str:
    step = 1024.0 if binary else 1000.0
    suffixes = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] if binary else \
        ["B", "kB", "MB", "GB", "TB", "PB"]
    for s in suffixes:
        if abs(n) < step or s == suffixes[-1]:
            if s == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {s}"
        n /= step
    return f"{n:.1f}"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"
