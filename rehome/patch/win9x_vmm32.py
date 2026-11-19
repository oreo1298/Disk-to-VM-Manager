"""Windows 95/98/Me's VMM32.VXD: a library of the system's VxDs (D48).

VMM32.VXD is a DOS program followed by a VxD library. W3 (uncompressed, as in
Windows 3.x's WIN386.EXE) lists each VxD's name and the offset of its LE
header. W4, which Setup writes on Windows 95 and later, is that W3 image
compressed in 8 KB chunks with the compression of DoubleSpace and MRCI. When
Windows starts, a VxD file in SYSTEM\\VMM32 takes the place of the copy in
VMM32.VXD, so a fix to one of them is a patched copy saved there.

A port of patcher9x's pe/pew.c and pe/ds_decompress.c (MIT, Copyright (c) 2022
Jaroslav Hensl; the notice is in win9x_tlb_data.py), with Clive Turvey's
description of the formats (VxDLib) for reference.
"""

from __future__ import annotations

import struct

CHUNK = 8192                    # W4 chunk size; the loader accepts no other
LE_HEADER = 0xC4                # size of an LE header
# patcher9x's DOS stub for a VxD on its own: "This program cannot be run in DOS mode."
DOS_STUB = bytes.fromhex(
    "4d5a80001f03000004000000ffff0000b80000000000000040000000000000000000000000000000"
    "0000000000000000000000000000000000000000800000000e1fba0e00b409cd21b8014ccd215468"
    "69732070726f6772616d2063616e6e6f742062652072756e20696e20444f53206d6f64652e0d0a24"
    "0000000000000000")


class FormatError(ValueError):
    """Not a VxD library, or a damaged one."""


def _count(data: bytes, pos: int) -> tuple[int, int]:
    """A copy's length: z zero bits, a one, z more bits v; 2**z + v + 1 (2 to 512)."""
    w = int.from_bytes(data[pos >> 3:(pos >> 3) + 5], "little") >> (pos & 7)
    zeros = 0
    while zeros < 9 and not w >> zeros & 1:
        zeros += 1
    if zeros == 9:
        raise FormatError("bad length in a compressed chunk")
    value = w >> (zeros + 1) & ((1 << zeros) - 1)
    return (1 << zeros) + value + 1, pos + 2 * zeros + 1


def ds_decompress(data: bytes, size: int = CHUNK) -> bytes:
    """One chunk compressed as DoubleSpace does: LSB-first codes for a byte (9 bits), a
    copy from 1-63, 64-319 or 320-4414 bytes back (8, 11 or 15 bits and a length), a
    sector break (4415) and the end (0)."""
    out = bytearray()
    pos = 0
    limit = len(data) * 8
    while len(out) < size:
        if pos >= limit:
            raise FormatError("a compressed chunk ends early")
        w = int.from_bytes(data[pos >> 3:(pos >> 3) + 4], "little") >> (pos & 7)
        if w & 3 == 1:
            out.append(0x80 | w >> 2 & 0x7F)
            pos += 9
            continue
        if w & 3 == 2:
            out.append(w >> 2 & 0x7F)
            pos += 9
            continue
        if w & 3 == 0:
            back = w >> 2 & 0x3F
            pos += 8
            if not back:
                break                           # the end of the chunk
        elif w & 7 == 3:
            back = (w >> 3 & 0xFF) + 64
            pos += 11
        else:
            back = (w >> 3 & 0xFFF) + 320
            pos += 15
            if back == 4415:
                continue                        # a 512-byte boundary
        length, pos = _count(data, pos)
        if back > len(out) or len(out) + length > size:
            raise FormatError("a copy outside a compressed chunk")
        for _ in range(length):                 # may overlap what it copies
            out.append(out[-back])
    return bytes(out)


def _header(image: bytes) -> tuple[int, bytes]:
    """(offset, magic) of the library after the DOS program."""
    if image[:2] != b"MZ" or len(image) < 0x40:
        raise FormatError("not a DOS program")
    pos = struct.unpack_from("<I", image, 0x3C)[0]
    if pos + 16 > len(image):
        raise FormatError("no VxD library")
    return pos, image[pos:pos + 2]


def to_w3(image: bytes) -> bytes:
    """VMM32.VXD as a W3 library, decompressing a W4 one."""
    pos, magic = _header(image)
    if magic == b"W3":
        return image
    if magic != b"W4":
        raise FormatError(f"not a VxD library ({magic!r})")
    chunk_size, count = struct.unpack_from("<HH", image, pos + 4)
    if chunk_size != CHUNK or image[pos + 8:pos + 10] != b"DS":
        raise FormatError("W4 library with an unknown compression")
    offsets = list(struct.unpack_from(f"<{count}I", image, pos + 16)) + [len(image)]
    parts = [image[:pos]]
    for start, end in zip(offsets, offsets[1:]):
        chunk = image[start:end]
        # A chunk that compression would not make smaller is stored as it is.
        parts.append(chunk if len(chunk) == CHUNK else ds_decompress(chunk))
    return b"".join(parts)


def names(image: bytes) -> list[str]:
    """The VxDs in a W3 or W4 library."""
    return [name for name, _off in _entries(to_w3(image))[0]]


def _entries(w3: bytes) -> tuple[list[tuple[str, int]], int]:
    pos, magic = _header(w3)
    if magic != b"W3":
        raise FormatError("not a W3 library")
    count = struct.unpack_from("<H", w3, pos + 4)[0]
    entries = []
    for i in range(count):
        name, offset, _size = struct.unpack_from("<8sII", w3, pos + 16 + 16 * i)
        entries.append((name.decode("ascii", "replace").rstrip(" \0").upper(), offset))
    return entries, pos


def extract(image: bytes, name: str) -> bytes | None:
    """VxD ``name`` (e.g. "VMM") from a W3 or W4 library as a file of its own, as
    SYSTEM\\VMM32\\<name>.VXD holds it; None if the library has no such VxD."""
    w3 = to_w3(image)
    entries, _pos = _entries(w3)
    offsets = sorted(off for _n, off in entries) + [len(w3)]
    for vxd, offset in entries:
        if vxd != name.upper():
            continue
        end = next(o for o in offsets if o > offset)
        le = bytearray(w3[offset:offset + LE_HEADER])
        if le[:2] != b"LE" or end - offset < LE_HEADER:
            raise FormatError(f"{name} is not an LE executable")
        # Offsets "from the top of the file" now count from the new file's start.
        for field in (0x80, 0x88):              # data pages, non-resident names
            value = struct.unpack_from("<I", le, field)[0]
            if value:
                struct.pack_into("<I", le, field, value + len(DOS_STUB) - offset)
        return DOS_STUB + bytes(le) + w3[offset + LE_HEADER:end]
    return None
