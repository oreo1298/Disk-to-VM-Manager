"""DOS, Windows 3.x and Windows 9x/Me detection (libguestfs does not recognise these)."""

from __future__ import annotations

import re

from rehome.inspect import pe
from rehome.inspect.gfs import ci_path, mounted, read, read_text
from rehome.inspect.model import DetectedOS

FAT_TYPES = ("vfat", "msdos", "fat", "fat12", "fat16", "fat32")


def _win9x_dir(g) -> str | None:
    """Locate the Windows directory (MSDOS.SYS [Paths] WinDir, else common names)."""
    msdos = read_text(g, "/MSDOS.SYS")
    if msdos:
        m = re.search(r"^\s*WinDir\s*=\s*[A-Za-z]:\\?(.*?)\s*$", msdos, re.M | re.I)
        if m:
            cand = "/" + m.group(1).replace("\\", "/").strip("/")
            if ci_path(g, cand):
                return ci_path(g, cand)
    for cand in ("/WINDOWS", "/WIN95", "/WIN98", "/WINME", "/WIN"):
        p = ci_path(g, cand)
        if p and ci_path(g, f"{p}/WIN.COM"):
            return p
    return None


def _win9x_release(ver: tuple[int, int, int, int] | None) -> tuple[str, str, str]:
    """(distro, version, friendly name) from KERNEL32.DLL's file version."""
    if not ver:
        return "win98", "4.10", "Windows 9x"
    major, minor, build = ver[0], ver[1], ver[2]
    if (major, minor) == (4, 0):
        osr = {950: "", 1111: " OSR2", 1212: " OSR2.1", 1213: " OSR2.5", 1214: " OSR2.5"}.get(build, "")
        return "win95", "4.0", f"Windows 95{osr}"
    if (major, minor) == (4, 10):
        se = " Second Edition" if build >= 2222 else ""
        return "win98", "4.10", f"Windows 98{se}"
    if (major, minor) == (4, 90):
        return "winme", "4.90", "Windows Me"
    return "win98", f"{major}.{minor}", "Windows 9x"


def _dos_flavour(g) -> tuple[str, str, str] | None:
    """(distro, version, name) for the DOS on the mounted volume, or None."""
    if ci_path(g, "/KERNEL.SYS"):
        kern = read(g, "/KERNEL.SYS") or b""
        build = pe.find_ascii(kern, rb"FreeDOS kernel.{0,12}?(\d{4})")
        ver = ""
        vfile = read_text(g, "/FREEDOS/VERSION.FDI") or read_text(g, "/FDOS/VERSION.FDI") or ""
        m = re.search(r"PLATFORM_VERSION\s*=\s*([\d.]+)", vfile)
        if m:
            ver = m.group(1)
        name = f"FreeDOS {ver}".strip() if ver else "FreeDOS" + (f" (kernel {build})" if build else "")
        return "freedos", ver or (build or ""), name
    ibm = ci_path(g, "/IBMBIO.COM")
    if ibm:
        data = (read(g, "/IBMBIO.COM") or b"") + (read(g, "/IBMDOS.COM") or b"") + \
            (read(g, "/COMMAND.COM") or b"")
        if re.search(rb"DR-DOS|Novell DOS|OpenDOS|Caldera", data):
            ver = pe.find_ascii(data, rb"(?:DR-DOS|Novell DOS|OpenDOS)\s+(?:Release\s+)?(\d+\.\d+)") or ""
            return "drdos", ver, f"DR-DOS {ver}".strip()
        ver = pe.find_ascii(data, rb"Version\s+(\d+\.\d+)") or ""
        return "pcdos", ver, f"PC DOS {ver}".strip()
    if ci_path(g, "/IO.SYS"):
        cmd = read(g, "/COMMAND.COM") or b""
        ver = pe.find_ascii(cmd, rb"MS-DOS(?:\(R\))?\s+Version\s+(\d+\.\d+)") or ""
        return "msdos", ver, f"MS-DOS {ver}".strip()
    return None


def probe_volume(g, device: str) -> DetectedOS | None:
    """Look for DOS / Windows 3.x / 9x on one FAT volume."""
    with mounted(g, device, readonly=True):
        has_cmd = ci_path(g, "/COMMAND.COM") is not None
        dos = _dos_flavour(g)
        windir = _win9x_dir(g)
        if windir and ci_path(g, f"{windir}/SYSTEM/VMM32.VXD") or \
                (windir and ci_path(g, f"{windir}/SYSTEM/KERNEL32.DLL")):
            k32 = read(g, f"{windir}/SYSTEM/KERNEL32.DLL", limit=2 * 2**20)
            ver = pe.file_version(k32) if k32 else None
            distro, version, name = _win9x_release(ver)
            o = DetectedOS(root=device, family="win9x", distro=distro, name=name,
                           product_name=name, version=version, arch="i386", boot_mode="bios",
                           bootloader="io.sys")
            o.build = str(ver[2]) if ver else ""
            o.details["windir"] = windir
            o.details["kernel32_version"] = pe.version_str(ver)
            if ci_path(g, f"{windir}/SYSTEM/IOSUBSYS/ESDI_506.PDR"):
                o.original_bus = "ide"
            o.rtc = "localtime"
            return o
        if dos is None and not has_cmd:
            return None
        distro, version, name = dos or ("dos", "", "DOS")
        o = DetectedOS(root=device, family="dos", distro=distro, name=name, product_name=name,
                       version=version, arch="i386", boot_mode="bios",
                       bootloader={"freedos": "kernel.sys"}.get(distro, "io.sys"))
        o.rtc = "localtime"
        o.original_bus = "ide"
        if windir and ci_path(g, f"{windir}/SYSTEM/KRNL386.EXE"):
            o.name += " with Windows 3.x"
            o.details["windows3"] = windir
        return o
