"""Windows 98/Me's TLB invalidation fix (win9x_tlb.py) and VMM32.VXD libraries
(win9x_vmm32.py), D48."""

import random
import struct

import pytest

from rehome.patch import win9x_speed as w
from rehome.patch import win9x_tlb
from rehome.patch import win9x_vmm32 as vx
from rehome.patch.win9x_tlb_data import ME_DISTANCE, WIN98, WIN98_SIMPLE, WIN_ME
from tests.unit.test_win9x_speed import embed


class Bits:
    """An LSB-first bit writer, as DoubleSpace streams are read."""

    def __init__(self):
        self.value, self.count = 0, 0

    def put(self, value, count):
        self.value |= (value & ((1 << count) - 1)) << self.count
        self.count += count

    def data(self):
        return self.value.to_bytes((self.count + 7) // 8, "little")


def ds_compress(data: bytes) -> bytes:
    """A DoubleSpace encoder written from Clive Turvey's description of the format
    (VxDLib): the decoder follows patcher9x's code, so each checks the other. Copies
    stay within a 512-byte sector, with a sector break between sectors."""
    out, i, seen = Bits(), 0, {}
    while i < len(data):
        if i and i % 512 == 0:
            out.put(0x7FFF, 15)
        end = min(len(data), (i // 512 + 1) * 512)
        best, back = 0, 0
        for j in reversed(seen.get(data[i:i + 2], [])):
            if i - j > 4414:
                break
            n = 0
            while i + n < end and n < 512 and data[j + n] == data[i + n]:
                n += 1
            if n > best:
                best, back = n, i - j
        if best >= 2:
            if back < 64:
                out.put(back << 2, 8)
            elif back < 320:
                out.put((back - 64) << 3 | 3, 11)
            else:
                out.put((back - 320) << 3 | 7, 15)
            z = (best - 1).bit_length() - 1
            out.put(1 << z | (best - 1 - (1 << z)) << (z + 1), 2 * z + 1)
            step = best
        else:
            b = data[i]
            out.put((b & 0x7F) << 2 | (1 if b & 0x80 else 2), 9)
            step = 1
        for k in range(i, i + step):
            seen.setdefault(data[k:k + 2], []).append(k)
        i += step
    out.put(0, 8)
    return out.data()


def sample(seed=3, size=vx.CHUNK) -> bytes:
    """Bytes with every kind of code: high and low raw bytes, copies from near, middle and
    far back, the longest copy (512), and copies that end at sector breaks."""
    rnd = random.Random(seed)
    data = bytearray(rnd.randbytes(700))
    while len(data) < size:
        back = rnd.choice([3, 40, 100, 300, 1000, 4000])
        length = rnd.choice([2, 3, 9, 70, 300, 512])
        if back <= len(data):
            for _ in range(length):
                data.append(data[-back])
        data += rnd.randbytes(rnd.randrange(1, 30))
    return bytes(data[:size])


def test_doublespace_round_trip():
    data = sample()
    packed = ds_compress(data)
    assert len(packed) < len(data)                  # the copies were used
    assert vx.ds_decompress(packed) == data
    assert vx.ds_decompress(ds_compress(data[:1000]), 1000) == data[:1000]


def test_a_damaged_chunk_is_refused():
    bad = Bits()
    bad.put(5 << 2, 8)                              # a copy from before the start
    bad.put(1, 1)
    with pytest.raises(vx.FormatError):
        vx.ds_decompress(bad.data())
    with pytest.raises(vx.FormatError):             # cut short
        vx.ds_decompress(ds_compress(sample())[:100])


def le_vxd(offset: int, body: bytes) -> bytes:
    """An LE header (data pages and non-resident names at file offsets, as in a W3
    library at ``offset``) and its body."""
    le = bytearray(vx.LE_HEADER)
    le[:2] = b"LE"
    struct.pack_into("<I", le, 0x80, offset + vx.LE_HEADER + 16)
    struct.pack_into("<I", le, 0x88, offset + vx.LE_HEADER + len(body) - 8)
    return bytes(le) + body


def w3_library(vxds: dict[str, bytes]) -> bytes:
    """A DOS program, a W3 header listing ``vxds`` and their LE images."""
    dos = bytearray(0x40)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    table = 16 + 16 * len(vxds)
    offset = len(dos) + table
    header = bytearray(b"W3\x00\x04" + struct.pack("<H", len(vxds)) + bytes(10))
    images = b""
    for name, body in vxds.items():
        image = le_vxd(offset + len(images), body)
        header += name.encode().ljust(8) + struct.pack("<II", offset + len(images), vx.LE_HEADER)
        images += image
    return bytes(dos) + bytes(header) + images


def w4_library(w3: bytes) -> bytes:
    """``w3`` compressed in 8 KB chunks as Setup writes VMM32.VXD; an incompressible
    full chunk is stored as it is."""
    pos = struct.unpack_from("<I", w3, 0x3C)[0]
    plain = w3[pos:]
    chunks = []
    for i in range(0, len(plain), vx.CHUNK):
        part = plain[i:i + vx.CHUNK]
        packed = ds_compress(part)
        chunks.append(part if len(part) == vx.CHUNK and len(packed) >= vx.CHUNK else packed)
    head = b"W4\x00\x04" + struct.pack("<HH", vx.CHUNK, len(chunks)) + b"DS" + bytes(6)
    start = pos + len(head) + 4 * len(chunks)
    offsets = []
    for c in chunks:
        offsets.append(start)
        start += len(c)
    return w3[:pos] + head + struct.pack(f"<{len(chunks)}I", *offsets) + b"".join(chunks)


def test_vmm_is_taken_from_a_compressed_library():
    vmm, _ = embed(WIN98[0], seed=5, at=3000)
    rnd = random.Random(9)
    w3 = w3_library({"VMM": bytes(vmm), "IOS": sample(4, 5000), "NOISE": rnd.randbytes(9000)})
    w4 = w4_library(w3)
    assert len(w4) < len(w3) and vx.to_w3(w4) == w3
    assert vx.names(w4) == ["VMM", "IOS", "NOISE"]
    got = vx.extract(w4, "vmm")
    assert got == vx.extract(w3, "VMM")
    # A file of its own: DOS stub, the LE header with offsets from its own start, the body.
    assert got[:2] == b"MZ" and struct.unpack_from("<I", got, 0x3C)[0] == len(vx.DOS_STUB)
    le = got[len(vx.DOS_STUB):]
    assert le[:2] == b"LE" and le[vx.LE_HEADER:] == bytes(vmm)
    assert struct.unpack_from("<I", le, 0x80)[0] == len(vx.DOS_STUB) + vx.LE_HEADER + 16
    assert vx.extract(w4, "VCACHE") is None
    with pytest.raises(vx.FormatError):
        vx.extract(b"MZ" + bytes(100), "VMM")


def test_vmm_of_windows_98_is_fixed_once():
    patch = WIN98[0]
    desc, orig, check, new, modif = patch
    data, code = embed(patch)
    before = bytes(data)
    assert win9x_tlb.patch(data) == ([desc], [])
    changed = [k for k in range(len(data)) if data[k] != before[k]]
    assert changed and all(4321 <= k < 4321 + len(new) and w._bit(modif, k - 4321)
                           for k in changed)
    assert all(data[4321 + k] == new[k] for k in range(len(new)) if w._bit(modif, k))
    assert win9x_tlb.patch(data) == ([], [desc])     # already fixed


def test_the_simple_fix_when_the_full_one_does_not_match():
    main, simple = WIN98[0], WIN98_SIMPLE[0]
    # A byte the full fix compares and the simple one does not.
    k = next(i for i in range(len(main[1])) if w._bit(main[2], i) and not w._bit(simple[2], i))
    data, _ = embed(simple, seed=7)
    data[4321 + k] ^= 0xFF
    assert win9x_tlb.patch(data) == ([simple[0]], [])


def test_vmm_of_windows_me_needs_both_blocks():
    (d1, orig1, check1, new1, modif1), (d2, orig2, check2, new2, modif2) = WIN_ME
    data, _ = embed(WIN_ME[0], seed=11, at=1000)
    rnd = random.Random(12)
    second = bytes(orig2[k] if w._bit(check2, k) else rnd.randrange(256) for k in range(len(orig2)))
    alone = bytearray(data + bytes(ME_DISTANCE + len(orig2)))
    assert win9x_tlb.patch(bytearray(alone)) == ([], [])     # the second block is missing
    alone[1000 + ME_DISTANCE:1000 + ME_DISTANCE + len(orig2)] = second
    assert win9x_tlb.patch(alone) == (["Windows Me VMM.VXD"], [])
    assert win9x_tlb.patch(alone) == ([], ["Windows Me VMM.VXD"])


def test_windows_95_is_left_alone():
    assert win9x_tlb.patch(bytearray(random.Random(1).randbytes(20000))) == ([], [])
