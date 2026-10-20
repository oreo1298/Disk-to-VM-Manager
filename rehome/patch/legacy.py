"""DOS, Windows 9x/Me and BSD: mostly hardware choices, a few file tweaks."""

from __future__ import annotations

import re

from rehome.inspect.gfs import ci_path, mounted, read_text
from rehome.patch.registry import CONFIG, PatchResult, patch


@patch("dos.report", "DOS hardware", kind=CONFIG, families=("dos",))
def dos_report(ctx) -> PatchResult:
    m = ctx.cfg.machine
    return PatchResult("", "", "info",
                       f"DOS runs on an emulated PC with IDE disks, a Sound Blaster 16 and an NE2000 "
                       f"network card ({m.memory_mib} MiB RAM). DOS cannot turn the VM off: use Turn Off.")


@patch("win9x.report", "Windows 9x hardware", kind=CONFIG, families=("win9x",))
def win9x_report(ctx) -> PatchResult:
    m = ctx.cfg.machine
    msg = (f"Uses hardware Windows 9x has built-in drivers for: Intel PIIX IDE, Cirrus Logic VGA, "
           f"Sound Blaster 16 and AMD PCnet networking, {m.memory_mib} MiB RAM.")
    if m.accel == "tcg":
        msg += " It runs under emulation to avoid the fast-CPU \"Windows protection error\"."
    if m.cpu_quota_pct:
        msg += f" Windows 9x never idles the CPU, so the VM is capped at {m.cpu_quota_pct}% of one core."
    return PatchResult("", "", "info", msg)


@patch("win9x.ram_limit", "Limit memory for Windows 9x", kind=CONFIG, families=("win9x",))
def ram_limit(ctx) -> PatchResult:
    limit = int(ctx.profile.hw("max_memory_mib", 512))
    m = ctx.cfg.machine
    if m.memory_mib > limit:
        old = m.memory_mib
        m.memory_mib = limit
        return PatchResult("", "", "applied", f"Windows 9x is unstable with more than {limit} MiB.",
                           [f"memory {old} → {limit} MiB"])
    return PatchResult("", "", "not-needed", f"{m.memory_mib} MiB is within the safe limit.")


@patch("win9x.vcache_limit", "Cap the disk cache (vcache)", families=("win9x",))
def vcache_limit(ctx) -> PatchResult:
    """Windows 98/Me crash with "insufficient memory" when vcache grows past ~512 MiB."""
    if ctx.cfg.machine.memory_mib < 512:
        return PatchResult("", "", "not-needed", "Not needed below 512 MiB.")
    g = ctx.g
    windir = ctx.os.details.get("windir", "/WINDOWS")
    with mounted(g, ctx.root, readonly=False):
        path = ci_path(g, f"{windir}/SYSTEM.INI")
        if not path:
            return PatchResult("", "", "skipped", "SYSTEM.INI not found.")
        text = read_text(g, path) or ""
        if re.search(r"^\s*MaxFileCache\s*=", text, re.M | re.I):
            return PatchResult("", "", "not-needed", "MaxFileCache is already set.")
        if re.search(r"^\[vcache\]", text, re.M | re.I):
            text = re.sub(r"(^\[vcache\][^\n]*\r?\n)", r"\1MaxFileCache=262144\r\n", text, count=1,
                          flags=re.M | re.I)
        else:
            text = text.rstrip("\r\n") + "\r\n\r\n[vcache]\r\nMaxFileCache=262144\r\n"
        g.write(path, text.encode("latin-1"))
    return PatchResult("", "", "applied", "Set [vcache] MaxFileCache=262144 in SYSTEM.INI.",
                       [f"{path}: [vcache] MaxFileCache=262144"])


@patch("bsd.report", "BSD disk controller", kind=CONFIG, families=("bsd",))
def bsd_report(ctx) -> PatchResult:
    bus = ctx.cfg.disks[0].bus if ctx.cfg.disks else "?"
    return PatchResult("", "", "info",
                       f"BSD systems name disks after their controller (ada, nda, vtbd), so the VM "
                       f"emulates the original controller ({bus}). Nothing inside the system was "
                       "changed.")
