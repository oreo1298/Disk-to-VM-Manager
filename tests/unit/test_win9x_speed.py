"""Windows 95/98 fast-CPU fix (rehome/patch/win9x_speed.py, patterns from patcher9x)."""

import random

from rehome.patch import win9x_speed as w
from rehome.patch.win9x_speed_data import DRIVERS, NDIS


def embed(patch, seed=1, at=4321):
    """A fake driver file with ``patch``'s original code at ``at``: the bytes the search
    ignores (relocated addresses, encoding variants) are random."""
    desc, orig, check, new, modif = patch
    rnd = random.Random(seed)
    data = bytearray(rnd.randbytes(at + len(orig) + 999))
    code = bytes(orig[k] if w._bit(check, k) else rnd.randrange(256) for k in range(len(orig)))
    data[at:at + len(orig)] = code
    return data, code


def test_ndis_of_windows_98_is_patched_and_relocations_kept():
    patch = NDIS[0]
    desc, orig, check, new, modif = patch
    data, code = embed(patch)
    before = bytes(data)
    assert w.find(data, orig, check) == 4321
    applied, already = w.patch(data, [patch])
    assert applied == [desc] and already == []
    for k in range(len(orig)):
        want = new[k] if w._bit(modif, k) else code[k]
        assert data[4321 + k] == want, k
    assert data[:4321] == before[:4321] and data[4321 + len(orig):] == before[4321 + len(orig):]
    # The new loop: mov ecx,6400000h (100 x 100000h); a zero time becomes one millisecond.
    assert bytes(data[4321 + 62:4321 + 67]) == bytes.fromhex("b900004006")
    assert bytes(data[4321 + 75:4321 + 83]) == bytes.fromhex("29d07501406a0a59")
    # A second run finds it done.
    assert w.patch(data, [patch]) == ([], [desc])


def test_every_pattern_matches_itself_only():
    for i, patch in enumerate(NDIS + DRIVERS):
        data, _ = embed(patch, seed=i)
        applied, _ = w.patch(data, NDIS + DRIVERS)
        assert applied == [patch[0]], (i, applied)
    plain = bytearray(random.Random(7).randbytes(200_000))
    assert w.patch(plain, NDIS + DRIVERS) == ([], [])


def test_every_occurrence_is_patched():
    patch = DRIVERS[0]
    data, _ = embed(patch, at=100)
    second, _ = embed(patch, seed=2, at=0)
    data += second[:len(patch[1])]
    applied, _ = w.patch(data, [patch])
    assert applied == [patch[0]]
    assert w.find(data, patch[1], patch[2]) == -1
