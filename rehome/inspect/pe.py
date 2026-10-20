"""Minimal PE/NE file version reader (VS_VERSIONINFO) for Windows files.

Used to tell Windows 95 from 98/Me (KERNEL32.DLL), to detect the HAL type
of Windows 2000/XP (HAL.DLL OriginalFilename) and similar probes. It scans
for the fixed-size VS_FIXEDFILEINFO block instead of walking the resource
tree, which works for PE (32/64-bit) and NE (16-bit) executables alike.
"""

from __future__ import annotations

import re
import struct

FIXED_SIG = struct.pack("<I", 0xFEEF04BD)


def file_version(data: bytes) -> tuple[int, int, int, int] | None:
    i = data.find(FIXED_SIG)
    while i != -1:
        if i + 52 <= len(data):
            sig, struc_ver, ms, ls = struct.unpack_from("<IIII", data, i)
            if struc_ver in (0x00010000, 0x00000000):
                return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
        i = data.find(FIXED_SIG, i + 4)
    return None


def string_value(data: bytes, key: str) -> str | None:
    """Value of a StringFileInfo entry (UTF-16LE in PE, ASCII in NE files)."""
    for enc, nul in (("utf-16-le", b"\x00\x00"), ("latin-1", b"\x00")):
        needle = key.encode(enc) + nul
        i = data.find(needle)
        if i == -1:
            continue
        j = i + len(needle)
        # Skip padding to the next 32-bit boundary (PE) / NULs (NE).
        while j < len(data) and data[j:j + len(nul)] == nul:
            j += len(nul)
        if enc == "utf-16-le" and (j % 2):
            j += 1
        end = data.find(nul, j)
        while enc == "utf-16-le" and end != -1 and (end - j) % 2:
            end = data.find(nul, end + 1)
        if end == -1:
            continue
        try:
            return data[j:end].decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return None


def machine(data: bytes) -> str | None:
    """CPU architecture of a PE image."""
    if data[:2] != b"MZ" or len(data) < 0x40:
        return None
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[off:off + 4] != b"PE\x00\x00":
        return None
    m = struct.unpack_from("<H", data, off + 4)[0]
    return {0x14C: "i386", 0x8664: "x86_64", 0xAA64: "aarch64", 0x1C0: "arm",
            0x1C4: "arm"}.get(m)


def version_str(v: tuple[int, int, int, int] | None) -> str:
    return ".".join(map(str, v)) if v else ""


def find_ascii(data: bytes, pattern: bytes) -> str | None:
    m = re.search(pattern, data)
    return m.group(1).decode("latin-1") if m else None
