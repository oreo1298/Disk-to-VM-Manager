"""Windows 98/Me's "TLB invalidation" bug (D48): random corruption under KVM on new CPUs.

VMM.VXD's FlushMappedCacheBlock changes page table entries without flushing the
processor's cached translations, relying on how older processors happened to
behave (Henry Wong, "Windows 9x TLB invalidation bug", stuffedcow.net, 2015).
AMD Zen 2 and Intel 11th-generation Core processors and later keep stale
translations longer, so with hardware virtualization (KVM) Windows writes into the
wrong pages: crashes, garbled screens, and hangs such as Windows 98's at shutdown
with the VGA BIOS's code overwritten by text-screen cells. Emulation (TCG) keeps
no such cache, so it never showed there.

patcher9x's fix (MIT, data in win9x_tlb_data.py) reloads CR3 there, which flushes
the translations. VMM.VXD lives inside VMM32.VXD; like patcher9x, Rehome saves a
patched copy as SYSTEM\\VMM32\\VMM.VXD, which Windows loads in its place
(win9x_vmm32.py). A VMM.VXD already there is patched where it is.
"""

from __future__ import annotations

from rehome.patch.win9x_speed import find
from rehome.patch.win9x_speed import patch as apply
from rehome.patch.win9x_tlb_data import ME_DISTANCE, WIN98, WIN98_SIMPLE, WIN98_UPGRADE, WIN_ME


def patch(data: bytearray) -> tuple[list[str], list[str]]:
    """Fix VMM.VXD in place: (applied, already applied) descriptions."""
    applied, already = apply(data, WIN98)
    if not applied and not already:
        # patcher9x's smaller change, for a VMM.VXD the patches above do not match.
        applied, already = apply(data, WIN98_SIMPLE)
    upgraded, _ = apply(data, WIN98_UPGRADE)
    applied += upgraded
    if applied or already:
        return applied, already
    (desc1, orig1, check1, new1, modif1), (desc2, orig2, check2, new2, modif2) = WIN_ME
    a, b = find(data, orig1, check1), find(data, orig2, check2)
    if a >= 0 and b - a == ME_DISTANCE:
        for pos, new, modif in ((a, new1, modif1), (b, new2, modif2)):
            for k in range(len(new)):
                if modif[k >> 3] >> (7 - (k & 7)) & 1:
                    data[pos + k] = new[k]
        return ["Windows Me VMM.VXD"], []
    if find(data, new1, check1) >= 0:
        return [], ["Windows Me VMM.VXD"]
    return [], []
