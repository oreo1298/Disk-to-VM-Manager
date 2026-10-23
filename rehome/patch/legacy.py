"""DOS, Windows 9x/Me and BSD: mostly hardware choices, a few file tweaks."""

from __future__ import annotations

import os
import re

from rehome.errors import RehomeError
from rehome.inspect import bootchain
from rehome.inspect.gfs import ci_path, mounted, read_text
from rehome.patch.registry import CONFIG, PatchResult, patch

# Where distributions keep SYSLINUX's BIOS files (Arch, Debian/Ubuntu, Fedora/openSUSE).
SYSLINUX_DIRS = ("/usr/lib/syslinux/bios", "/usr/lib/syslinux/modules/bios", "/usr/share/syslinux",
                 "/usr/lib/syslinux")
SYSLINUX_MBR = ("/usr/lib/syslinux/bios/mbr.bin", "/usr/lib/syslinux/mbr/mbr.bin",
                "/usr/share/syslinux/mbr.bin", "/usr/lib/syslinux/mbr.bin")
CHAIN_MODULES = ("chain.c32", "libcom32.c32", "libutil.c32")


def _syslinux_modules() -> str | None:
    for d in SYSLINUX_DIRS:
        if all(os.path.isfile(os.path.join(d, m)) for m in CHAIN_MODULES):
            return d
    return None


def _chain_loader(o) -> str | None:
    """chain.c32 option that starts this system's DOS kernel file."""
    # Paths are absolute: chain.c32 resolves them from SYSLINUX's directory.
    if o.family == "win9x":
        return "msdos7=/io.sys"                 # Windows 95/98/Me: MS-DOS 7.x IO.SYS
    ver = o.version or ""
    return {"freedos": "freedos=/kernel.sys", "pcdos": "pcdos=/ibmbio.com",
            "msdos": "msdos7=/io.sys" if ver.startswith("7") else "msdos=/io.sys"}.get(o.distro)


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


@patch("dos.boot_record", "Make the disk bootable", families=("dos", "win9x"))
def boot_record(ctx) -> PatchResult:
    """Repair the BIOS boot chain (MBR code, active flag, boot sector) in the overlay.

    Disks prepared on Linux often cannot start DOS or Windows 9x: mkfs.fat
    writes placeholder boot code, and no partition may be marked active. The
    boot sector is replaced by SYSLINUX, whose chain.c32 starts the system's
    own IO.SYS / KERNEL.SYS (Rehome cannot ship Microsoft's boot code).
    """
    g, o = ctx.g, ctx.os
    bc = bootchain.analyze(g, ctx.root)
    ctx.os.details["boot_chain"] = bc.to_dict()
    if bc.scheme == "gpt":
        return PatchResult("", "", "skipped", bc.problems[0])
    if not bc.problems:
        return PatchResult("", "", "not-needed", "The disk's boot code and active partition are fine.")
    changes = []
    if bc.vbr_code in bootchain.UNBOOTABLE:
        loader = _chain_loader(o)
        modules = _syslinux_modules()
        if not loader:
            return PatchResult("", "", "skipped", " ".join(bc.problems) +
                               f" Rehome has no boot loader for {o.name}.")
        if not modules:
            raise RehomeError("SYSLINUX is needed to make this disk bootable but is not installed.",
                              fix="Install syslinux, then run the fixes again with "
                                  "`rehome patch run VM --only dos.boot_record`.")
        # Files first, boot code last: a failure leaves the boot sector untouched.
        with mounted(g, ctx.root, readonly=False):
            g.mkdir_p("/syslinux")
            for m in CHAIN_MODULES:
                g.upload(os.path.join(modules, m), f"/syslinux/{m}")
            cfg = ("# Written by Rehome: this partition had no working boot code, so SYSLINUX\r\n"
                   f"# starts {o.name} through chain.c32 (in the VM's copy of the disk only).\r\n"
                   "DEFAULT system\r\nPROMPT 0\r\nTIMEOUT 0\r\n"
                   f"LABEL system\r\n  COM32 chain.c32\r\n  APPEND fs {loader}\r\n").encode()
            for name in ("syslinux.cfg", "extlinux.conf"):      # whichever installer runs
                g.write(f"/syslinux/{name}", cfg)
        tool = _install_boot_sector(g, ctx.root)
        changes.append(f"{ctx.root}: boot sector replaced by SYSLINUX ({tool}), which starts the "
                       f"system with chain.c32 {loader} (/syslinux/syslinux.cfg)")
    if bc.partition:
        if bc.mbr_code in bootchain.UNBOOTABLE:
            mbr = next((p for p in SYSLINUX_MBR if os.path.isfile(p)), None)
            if not mbr:
                raise RehomeError("The disk has no master boot record code, and SYSLINUX's "
                                  "mbr.bin is not installed.", fix="Install syslinux.")
            with open(mbr, "rb") as f:
                g.pwrite_device(bc.disk, f.read()[:440], 0)     # keeps the partition table
            changes.append(f"{bc.disk}: master boot record code written (SYSLINUX mbr.bin)")
        if bc.active != [bc.partition]:
            for p in g.part_list(bc.disk):
                n = p["part_num"]
                if n == bc.partition or n in bc.active:
                    g.part_set_bootable(bc.disk, n, n == bc.partition)
            changes.append(f"{bc.disk}: partition {bc.partition} marked active"
                           + (f" (was: {', '.join(map(str, bc.active))})" if bc.active else ""))
    return PatchResult("", "", "applied", " ".join(bc.problems) + " Fixed in the VM's disk.",
                       changes)


def _install_boot_sector(g, device: str) -> str:
    """SYSLINUX's boot sector and ldlinux.sys in /syslinux of a FAT partition; the tool used.

    libguestfs's ``syslinux`` needs mtools inside its helper VM, which it only has when
    mtools is installed on this computer (Arch's syslinux package does not pull it in).
    ``extlinux`` does the same job through the Linux FAT driver of the mounted partition.
    """
    try:
        g.syslinux(device, directory="/syslinux")
        return "syslinux"
    except RuntimeError as first:
        try:
            with mounted(g, device, readonly=False):
                g.extlinux("/syslinux")
            return "extlinux"
        except RuntimeError as second:
            raise RehomeError(
                "SYSLINUX could not be installed on the system's partition.",
                fix="Install the syslinux and mtools packages, then run the fixes again: "
                    "`rehome patch run VM --only dos.boot_record`.",
                details=f"syslinux: {first}\nextlinux: {second}") from second


@patch("bsd.report", "BSD disk controller", kind=CONFIG, families=("bsd",))
def bsd_report(ctx) -> PatchResult:
    bus = ctx.cfg.disks[0].bus if ctx.cfg.disks else "?"
    return PatchResult("", "", "info",
                       f"BSD systems name disks after their controller (ada, nda, vtbd), so the VM "
                       f"emulates the original controller ({bus}). Nothing inside the system was "
                       "changed.")
