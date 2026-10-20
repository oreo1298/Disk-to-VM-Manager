"""Build a real, bootable FreeDOS hard-disk image for tests.

The image mimics a classic DOS installation on a physical disk: an MBR with
one active FAT16 partition at LBA 63, the FreeDOS boot sector (assembled
from the kernel sources) with the BPB written by mkfs.fat, KERNEL.SYS,
COMMAND.COM (FreeCOM), FDCONFIG.SYS and AUTOEXEC.BAT. AUTOEXEC prints a
marker line so boot tests can detect a successful boot from VGA text memory.

Inputs (downloaded from SourceForge's FreeDOS project if missing):
  ke2042_86f32.zip (kernel), ke2042s.zip (kernel sources), com083Beta54bin.zip
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MARKER = "REHOME-FREEDOS-BOOT-OK"
FILES = {
    "ke2042_86f32.zip": "https://sourceforge.net/projects/freedos/files/Kernel/2042/ke2042_86f32.zip/download",
    "ke2042s.zip": "https://sourceforge.net/projects/freedos/files/Kernel/2042/ke2042s.zip/download",
    "com083Beta54bin.zip": "https://sourceforge.net/projects/freedos/files/OldFiles/com083Beta54bin.zip/download",
}
MBR_CANDIDATES = ["/usr/lib/syslinux/mbr/mbr.bin", "/usr/share/syslinux/mbr.bin",
                  "/usr/lib/syslinux/bios/mbr.bin", "/usr/share/syslinux/bios/mbr.bin"]
SECTOR = 512
HEADS, SPT = 16, 63


def media_dir() -> Path:
    d = Path(os.environ.get("REHOME_TEST_MEDIA", Path(__file__).resolve().parents[1] / "images"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch(name: str) -> Path:
    for d in (media_dir(), Path("/home/user/test-media")):
        if (d / name).exists():
            return d / name
    dest = media_dir() / name
    with urllib.request.urlopen(FILES[name], timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def available() -> str | None:
    """Return a reason string if the image cannot be built here."""
    for tool in ("nasm", "mkfs.fat", "mcopy"):
        if not shutil.which(tool) and not os.path.exists(f"/usr/sbin/{tool}") \
                and not os.path.exists(f"/sbin/{tool}"):
            return f"{tool} is not installed"
    if not any(os.path.exists(p) for p in MBR_CANDIDATES):
        return "syslinux mbr.bin is not installed"
    return None


def _chs(lba: int) -> bytes:
    cyl = lba // (HEADS * SPT)
    head = (lba // SPT) % HEADS
    sec = lba % SPT + 1
    if cyl > 1023:
        cyl, head, sec = 1023, HEADS - 1, SPT
    return bytes([head, (sec & 0x3F) | ((cyl >> 2) & 0xC0), cyl & 0xFF])


def _mkfs() -> str:
    for p in ("mkfs.fat", "/usr/sbin/mkfs.fat", "/sbin/mkfs.fat"):
        if shutil.which(p) or os.path.exists(p):
            return p
    raise RuntimeError("mkfs.fat missing")


def build(out: str | os.PathLike, size_mib: int = 64, label: str = "FREEDOS",
          extra_files: dict[str, bytes] | None = None) -> Path:
    out = Path(out)
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        with zipfile.ZipFile(fetch("ke2042_86f32.zip")) as z:
            kernel = z.read("bin/KERNEL.SYS")
            country = z.read("bin/country.sys")
        with zipfile.ZipFile(fetch("ke2042s.zip")) as z:
            for n in z.namelist():
                if n.lower().endswith("boot/boot.asm"):
                    (t / "boot.asm").write_bytes(z.read(n))
        with zipfile.ZipFile(fetch("com083Beta54bin.zip")) as z:
            command = z.read("command.com")
        subprocess.run(["nasm", "-f", "bin", "-DISFAT16", "boot.asm", "-o", "fat16.bin"],
                       cwd=t, check=True, capture_output=True)
        boot = (t / "fat16.bin").read_bytes()
        assert len(boot) == SECTOR and boot[510:] == b"\x55\xaa"

        start = SPT  # LBA 63, like DOS FDISK
        total = size_mib * 2**20 // SECTOR
        part_sectors = total - start
        part = t / "part.img"
        subprocess.run([_mkfs(), "-F", "16", "-n", label, "-h", str(start), "-g", f"{HEADS}/{SPT}",
                        "-C", str(part), str(part_sectors * SECTOR // 1024)],
                       check=True, capture_output=True)
        # Merge: FreeDOS boot code + the BPB mkfs.fat computed.
        with open(part, "r+b") as f:
            vbr = bytearray(f.read(SECTOR))
            merged = bytearray(boot)
            merged[0x03:0x3E] = vbr[0x03:0x3E]
            merged[0x24] = 0x80                     # boot drive: first hard disk
            f.seek(0)
            f.write(merged)
        files = {
            "KERNEL.SYS": kernel,
            "COMMAND.COM": command,
            "COUNTRY.SYS": country,
            "FDCONFIG.SYS": b"SHELL=C:\\COMMAND.COM C:\\ /E:1024 /P\r\nFILES=40\r\nBUFFERS=20\r\nLASTDRIVE=Z\r\n",
            "AUTOEXEC.BAT": (f"@ECHO OFF\r\nSET PATH=C:\\FDOS\\BIN;C:\\\r\nECHO {MARKER}\r\nVER\r\n").encode(),
        }
        files.update(extra_files or {})
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1", MTOOLS_NO_VFAT="1")
        subprocess.run(["mmd", "-i", str(part), "::FDOS", "::FDOS/BIN", "::TEMP"], check=True,
                       capture_output=True, env=env)
        for name, data in files.items():
            src = t / name.replace("/", "_")
            src.write_bytes(data)
            subprocess.run(["mcopy", "-i", str(part), str(src), f"::{name}"], check=True,
                           capture_output=True, env=env)
        mbr_code = Path(next(p for p in MBR_CANDIDATES if os.path.exists(p))).read_bytes()[:440]
        entry = bytes([0x80]) + _chs(start) + bytes([0x06]) + _chs(total - 1) + \
            struct.pack("<II", start, part_sectors)
        mbr = mbr_code + b"\x00" * (446 - len(mbr_code)) + entry + b"\x00" * 48 + b"\x55\xaa"
        assert len(mbr) == SECTOR
        with open(out, "wb") as f:
            f.write(mbr)
            f.write(b"\x00" * (start - 1) * SECTOR)
            with open(part, "rb") as p:
                shutil.copyfileobj(p, f, 4 * 2**20)
    return out


if __name__ == "__main__":
    import sys
    print(build(sys.argv[1] if len(sys.argv) > 1 else "freedos.img"))
