"""Create Windows registry hive files for tests.

``empty_hive()`` returns a minimal valid regf file (root key + security
descriptor); ``build_hive(path, tree)`` fills it with keys and values using
hivex. ``tree`` maps backslash paths to {value name: value} dicts, values
being str (REG_SZ), int (REG_DWORD), list[str] (REG_MULTI_SZ) or bytes.
"""

from __future__ import annotations

import struct
import time

import hivex

HBIN = 4096


def _filetime() -> int:
    return int((time.time() + 11644473600) * 10_000_000)


def _cell(data: bytes) -> bytes:
    size = (len(data) + 4 + 7) & ~7
    return struct.pack("<i", -size) + data + b"\x00" * (size - 4 - len(data))


def empty_hive(name: str = "SYSTEM") -> bytes:
    sk_off, nk_off = 0x20, None
    # Self-relative security descriptor with an empty DACL.
    sd = struct.pack("<BBHIIII", 1, 0, 0x8004, 0, 0, 0, 20) + struct.pack("<BBHHH", 2, 0, 8, 0, 0)
    sk_body = lambda off: b"sk" + b"\x00\x00" + struct.pack("<IIII", off, off, 1, len(sd)) + sd  # noqa: E731
    sk = _cell(sk_body(sk_off))
    nk_off = sk_off + len(sk)
    keyname = b"ROOT"
    nk = (b"nk" + struct.pack("<H", 0x2C) + struct.pack("<Q", _filetime()) + struct.pack("<I", 0)
          + struct.pack("<I", 0xFFFFFFFF)                 # parent (none for the root)
          + struct.pack("<II", 0, 0)                      # subkey counts
          + struct.pack("<II", 0xFFFFFFFF, 0xFFFFFFFF)    # subkey lists
          + struct.pack("<II", 0, 0xFFFFFFFF)             # values count, list
          + struct.pack("<I", sk_off)                     # security
          + struct.pack("<I", 0xFFFFFFFF)                 # class name
          + struct.pack("<IIIII", 0, 0, 0, 0, 0)          # max lengths + workvar
          + struct.pack("<HH", len(keyname), 0) + keyname)
    nk_cell = _cell(nk)
    cells = sk + nk_cell
    free = HBIN - 0x20 - len(cells)
    hbin = (b"hbin" + struct.pack("<III", 0, HBIN, 0) + struct.pack("<I", 0)
            + struct.pack("<Q", _filetime()) + struct.pack("<I", 0))
    assert len(hbin) == 0x20
    hbin += cells + struct.pack("<i", free) + b"\x00" * (free - 4)
    base = bytearray(HBIN)
    struct.pack_into("<4sIIQIIIIIII", base, 0, b"regf", 1, 1, _filetime(), 1, 5, 0, 1,
                     nk_off, HBIN, 1)
    fname = name.encode("utf-16-le")[:64]
    base[0x30:0x30 + len(fname)] = fname
    csum = 0
    for i in range(0, 0x1FC, 4):
        csum ^= struct.unpack_from("<I", base, i)[0]
    struct.pack_into("<I", base, 0x1FC, csum or 1)
    return bytes(base) + hbin


def _value(name: str, v):
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return {"key": name, "t": 4, "value": struct.pack("<I", v & 0xFFFFFFFF)}
    if isinstance(v, list):
        return {"key": name, "t": 7, "value": ("\x00".join(v) + "\x00\x00").encode("utf-16-le")}
    if isinstance(v, bytes):
        return {"key": name, "t": 3, "value": v}
    return {"key": name, "t": 1, "value": (str(v) + "\x00").encode("utf-16-le")}


def build_hive(path: str, tree: dict[str, dict], name: str = "SYSTEM") -> None:
    with open(path, "wb") as f:
        f.write(empty_hive(name))
    h = hivex.Hivex(path, write=True)
    for keypath, values in tree.items():
        node = h.root()
        for part in [p for p in keypath.split("\\") if p]:
            child = h.node_get_child(node, part)
            node = child if child else h.node_add_child(node, part)
        if values:
            h.node_set_values(node, [_value(k, v) for k, v in values.items()])
    h.commit(path)


def read_value(path: str, keypath: str, name: str):
    h = hivex.Hivex(path)
    node = h.root()
    for part in [p for p in keypath.split("\\") if p]:
        node = h.node_get_child(node, part)
        if not node:
            return None
    v = h.node_get_value(node, name)
    if not v:
        return None
    t = h.value_type(v)[0]
    if t == 4:
        return h.value_dword(v)
    if t in (1, 2):
        return h.value_string(v)
    if t == 7:
        return h.value_multiple_strings(v)
    return h.value_value(v)[1]


def minimal_pe(machine: str = "x86_64") -> bytes:
    """Smallest PE image that `file` (and libguestfs) recognise as a console EXE."""
    pe64 = machine == "x86_64"
    mz = bytearray(0x40)
    mz[0:2] = b"MZ"
    struct.pack_into("<I", mz, 0x3C, 0x40)
    coff = b"PE\x00\x00" + struct.pack("<HHIIIHH", 0x8664 if pe64 else 0x14C, 0, 0, 0, 0,
                                       0xF0 if pe64 else 0xE0, 0x22 if pe64 else 0x102)
    opt = bytearray(0xF0 if pe64 else 0xE0)
    struct.pack_into("<H", opt, 0, 0x20B if pe64 else 0x10B)
    struct.pack_into("<H", opt, 68, 3)   # subsystem: console
    return bytes(mz) + coff + bytes(opt) + b"\x00" * 256


def version_resource(major: int, minor: int, build: int, rev: int,
                     original_filename: str = "") -> bytes:
    """A fake VS_VERSIONINFO blob (enough for rehome.inspect.pe)."""
    fixed = struct.pack("<IIIIIIIIIIIII", 0xFEEF04BD, 0x00010000, (major << 16) | minor,
                        (build << 16) | rev, (major << 16) | minor, (build << 16) | rev,
                        0x3F, 0, 0x40004, 1, 0, 0, 0)
    blob = b"VS_VERSION_INFO".decode().encode("utf-16-le") + b"\x00\x00" + fixed
    if original_filename:
        blob += "OriginalFilename".encode("utf-16-le") + b"\x00\x00" + \
            original_filename.encode("utf-16-le") + b"\x00\x00"
    return blob
