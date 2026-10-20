"""List the contents of a Linux initramfs (concatenated, possibly compressed cpio)."""

from __future__ import annotations

import bz2
import gzip
import lzma
import shutil
import subprocess

SIGNATURES = [
    (b"\x1f\x8b", "gzip"), (b"\xfd7zXZ\x00", "xz"), (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x02\x21\x4c\x18", "lz4"), (b"\x04\x22\x4d\x18", "lz4"), (b"BZh", "bzip2"),
    (b"\x5d\x00\x00", "lzma"), (b"\x89LZO", "lzo"),
]


def _decompress(data: bytes, kind: str) -> bytes | None:
    try:
        if kind == "gzip":
            return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else None
        if kind in ("xz", "lzma"):
            return lzma.LZMADecompressor().decompress(data)
        if kind == "bzip2":
            return bz2.BZ2Decompressor().decompress(data)
    except (OSError, EOFError, lzma.LZMAError, ValueError):
        # Trailing garbage after the stream is normal; decompressors that
        # stop at stream end handle it, gzip.decompress does not.
        if kind == "gzip":
            try:
                d = gzip.GzipFile(fileobj=__import__("io").BytesIO(data))
                return d.read()
            except (OSError, EOFError):
                return None
        return None
    tool = {"zstd": ["zstd", "-dcq"], "lz4": ["lz4", "-dcq"], "lzo": ["lzop", "-dc"]}.get(kind)
    if tool and shutil.which(tool[0]):
        cp = subprocess.run(tool, input=data, capture_output=True, timeout=120)
        if cp.stdout:
            return cp.stdout
    return None


def _newc(data: bytes, pos: int, out: list[str]) -> int:
    """Parse one newc/crc archive starting at ``pos``; return the end position."""
    while pos + 110 <= len(data) and data[pos:pos + 5] == b"07070":
        try:
            filesize = int(data[pos + 54:pos + 62], 16)
            namesize = int(data[pos + 94:pos + 102], 16)
        except ValueError:
            break
        name = data[pos + 110:pos + 110 + namesize - 1].decode("utf-8", errors="replace")
        pos = (pos + 110 + namesize + 3) & ~3
        pos = (pos + filesize + 3) & ~3
        if name == "TRAILER!!!":
            return pos
        out.append(name)
    return pos


def list_files(data: bytes, depth: int = 0) -> list[str] | None:
    """All paths in the initramfs, or None if a compressed part could not be read."""
    out: list[str] = []
    pos = 0
    while pos < len(data):
        while pos < len(data) and data[pos] == 0:
            pos += 1
        if pos >= len(data):
            break
        if data[pos:pos + 5] == b"07070":
            pos = _newc(data, pos, out)
            continue
        kind = next((k for sig, k in SIGNATURES if data[pos:pos + len(sig)] == sig), None)
        if kind is None or depth > 3:
            break
        inner = _decompress(data[pos:], kind)
        if inner is None:
            return out or None
        rest = list_files(inner, depth + 1)
        if rest is None:
            return out or None
        out.extend(rest)
        break
    return out


STORAGE_MODULES = {
    "virtio_blk": "virtio", "virtio_scsi": "virtio", "nvme": "nvme", "ahci": "sata",
    "ata_piix": "ide", "pata_acpi": "ide", "ata_generic": "ide", "mptspi": "scsi",
    "mptsas": "scsi", "mpt3sas": "scsi", "vmw_pvscsi": "scsi", "hv_storvsc": "hyperv",
    "xen_blkfront": "xen", "usb_storage": "usb", "uas": "usb", "sdhci": "mmc",
}


def storage_modules(files: list[str]) -> set[str]:
    found = set()
    for f in files:
        base = f.rsplit("/", 1)[-1]
        mod = base.split(".ko", 1)[0].replace("-", "_") if ".ko" in base else ""
        if mod in STORAGE_MODULES:
            found.add(mod)
    return found
