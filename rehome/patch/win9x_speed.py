"""The Windows 95/98 "CPU speed" bug: "While initializing device NDIS: Windows
protection error" on fast computers, emulated ones included.

NDIS.VXD (and some disk drivers) calibrate a delay loop: they run about a
million LOOP instructions and divide by the milliseconds that took. A CPU that
finishes within one millisecond makes that a division by zero. QEMU 7.2 and
later emulate LOOP fast enough for this on a fast computer, so Windows 98 can
fail even under emulation (seen with an appliance on a faster computer than the
one the VM ran on before). Windows 98 SE guards the division; 95 and 98 do not.

patcher9x's fix (MIT, data in win9x_speed_data.py): the loop runs 100 times
(drivers: 8 times) longer, the result is scaled to match, and a zero time counts
as one millisecond. Rehome finds the original code by its bytes, with the
addresses the loader relocates left out, and changes only what the fix changes.
"""

from __future__ import annotations

from rehome.patch.win9x_speed_data import DRIVERS, NDIS

# Files patcher9x patches outside VMM32.VXD, relative to the Windows folder.
TARGETS = [
    ("SYSTEM/NDIS.VXD", NDIS),
    ("SYSTEM/VMM32/NDIS.VXD", NDIS),
    ("SYSTEM/IOSUBSYS/ESDI_506.PDR", DRIVERS),
    ("SYSTEM/IOSUBSYS/SCSIPORT.PDR", DRIVERS),
    ("SYSTEM/VMM32/IOS.VXD", DRIVERS),
    ("SYSTEM/VMM32/NTKERN.VXD", DRIVERS),
]


def _bit(bitmap: bytes, i: int) -> int:
    return bitmap[i >> 3] >> (7 - (i & 7)) & 1


def _anchor(pattern: bytes, check: bytes) -> tuple[int, bytes]:
    """The longest run of checked bytes, to search for with bytes.find."""
    best, start = (0, b""), None
    for i in range(len(pattern) + 1):
        if i < len(pattern) and _bit(check, i):
            start = i if start is None else start
        elif start is not None:
            if i - start > len(best[1]):
                best = (start, pattern[start:i])
            start = None
    return best


def find(data: bytes, pattern: bytes, check: bytes, start: int = 0) -> int:
    """Offset of ``pattern`` in ``data`` comparing only the bytes ``check`` marks, or -1."""
    off, anchor = _anchor(pattern, check)
    i = data.find(anchor, start + off)
    while i >= 0:
        pos = i - off
        if pos + len(pattern) <= len(data) and all(
                data[pos + k] == pattern[k] for k in range(len(pattern)) if _bit(check, k)):
            return pos
        i = data.find(anchor, i + 1)
    return -1


def patch(data: bytearray, patches) -> tuple[list[str], list[str]]:
    """Apply every matching patch in place: (applied, already applied) descriptions."""
    applied, already = [], []
    for desc, orig, check, new, modif in patches:
        pos = find(data, orig, check)
        if pos < 0:
            if find(data, new, check) >= 0:
                already.append(desc)
            continue
        while pos >= 0:
            for k in range(len(new)):
                if _bit(modif, k):
                    data[pos + k] = new[k]
            pos = find(data, orig, check, pos + len(orig))
        applied.append(desc)
    return applied, already
