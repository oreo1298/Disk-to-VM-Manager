"""Discover import sources (physical disks and image files) and check their safety.

Everything here runs unprivileged: lsblk, sysfs and /proc are world-readable.
The privileged helper repeats the critical checks and adds an O_EXCL claim
at open time, so a race between this analysis and the open is harmless.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rehome.errors import RehomeError
from rehome.util.proc import run, which
from rehome.util.units import human

LSBLK_COLS = ("NAME,PATH,KNAME,PKNAME,SIZE,TYPE,TRAN,MODEL,SERIAL,VENDOR,RM,RO,ROTA,HOTPLUG,"
              "FSTYPE,LABEL,PARTTYPE,PARTLABEL,PTTYPE,MOUNTPOINTS,WWN,MAJ:MIN")
SAFE_ROOT_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home", "[SWAP]")

# First bytes of the formats QEMU reads, plus E01 (libewf).
MAGIC = [
    (0, b"QFI\xfb", "qcow2"),
    (0, b"conectix", "vpc"),
    (0, b"vhdxfile", "vhdx"),
    (0, b"KDMV", "vmdk"),
    (0, b"# Disk DescriptorFile", "vmdk"),
    (64, b"\x7f\x10\xda\xbe", "vdi"),
    (0, b"<<< ", "vdi"),
    (0, b"EVF\x09\x0d\x0a\xff\x00", "ewf"),
    (0, b"EVF2\x0d\x0a\x81\x00", "ewf"),
    (0, b"LVF\x09\x0d\x0a\xff\x00", "ewf"),
]
FORMAT_LABEL = {"raw": "raw disk image", "qcow2": "QEMU qcow2", "vpc": "VHD (Virtual PC/Hyper-V)",
                "vhdx": "VHDX (Hyper-V)", "vmdk": "VMDK (VMware)", "vdi": "VDI (VirtualBox)",
                "ewf": "E01 forensic image", "block": "physical disk"}


@dataclass
class Partition:
    path: str
    size: int
    fstype: str = ""
    label: str = ""
    parttype: str = ""
    mountpoints: list[str] = field(default_factory=list)


@dataclass
class Source:
    kind: str                    # block / image / ewf
    path: str                    # what the user gave or the kernel name (/dev/sdb)
    stable_path: str = ""        # /dev/disk/by-id/... (block) or absolute path (image)
    format: str = "raw"
    size: int = 0                # virtual size in bytes
    actual_size: int = 0         # bytes used on the host (images)
    label: str = ""              # human description
    model: str = ""
    serial: str = ""
    transport: str = ""          # sata / nvme / usb / ata / scsi / mmc / virtio
    removable: bool = False
    rotational: bool = False
    partition_table: str = ""    # gpt / dos / ""
    partitions: list[Partition] = field(default_factory=list)
    backing_chain: list[str] = field(default_factory=list)
    encrypted_image: bool = False
    safe: bool = True
    problems: list[str] = field(default_factory=list)
    problem_code: str = ""

    @property
    def display(self) -> str:
        if self.kind == "block":
            parts = [self.model or self.path, human(self.size)]
            if self.transport:
                parts.append(self.transport.upper())
            return " — ".join(parts)
        return f"{os.path.basename(self.path)} — {FORMAT_LABEL.get(self.format, self.format)}, {human(self.size)}"

    def to_dict(self) -> dict:
        return asdict(self)


# --- block devices -------------------------------------------------------------------

def _lsblk(path: str | None = None) -> list[dict]:
    if not which("lsblk"):
        raise RehomeError("lsblk is not installed.", fix="Install util-linux.")
    argv = ["lsblk", "-J", "-b", "-o", LSBLK_COLS]
    if path:
        argv.append(path)
    cp = run(argv, what="Listing disks", timeout=30)
    return json.loads(cp.stdout).get("blockdevices", [])


def _stable_path(dev: dict) -> str:
    """Prefer /dev/disk/by-id names: they survive reboots and re-plugging."""
    by_id = Path("/dev/disk/by-id")
    real = os.path.realpath(dev.get("path") or "")
    best = ""
    if by_id.is_dir():
        ranked = []
        for link in by_id.iterdir():
            try:
                if os.path.realpath(link) != real:
                    continue
            except OSError:
                continue
            n = link.name
            rank = 0 if n.startswith(("nvme-eui", "wwn-")) else 1 if n.startswith(
                ("ata-", "nvme-", "usb-", "scsi-", "mmc-", "virtio-")) else 2
            ranked.append((rank, n))
        if ranked:
            best = str(by_id / sorted(ranked)[0][1])
    return best or dev.get("path") or ""


def _mounts(dev: dict) -> list[str]:
    return [m for m in (dev.get("mountpoints") or []) if m]


def host_usage() -> dict[str, str]:
    """kernel name -> mountpoint (or [SWAP]) for devices in use by the host."""
    used: dict[str, str] = {}

    def walk(nodes, ancestors):
        for n in nodes:
            names = ancestors + [n.get("kname") or n.get("name")]
            for m in _mounts(n):
                for a in names:
                    used.setdefault(a, m)
            walk(n.get("children") or [], names)
    try:
        walk(_lsblk(), [])
    except RehomeError:
        pass
    # Stacked devices (LVM, dm-crypt, md) list their parents as children in
    # lsblk's tree only from the parent side; also follow holders in sysfs.
    sysblock = Path("/sys/class/block")
    for name, mp in list(used.items()):
        for slave in (sysblock / name / "slaves").glob("*") if (sysblock / name).exists() else []:
            used.setdefault(slave.name, mp)
            parent = Path(os.path.realpath(sysblock / slave.name)).parent.name
            used.setdefault(parent, mp)
    return used


def _disk_source(dev: dict, usage: dict[str, str]) -> Source:
    kname = dev.get("kname") or dev.get("name")
    s = Source(kind="block", path=dev.get("path") or f"/dev/{kname}",
               stable_path=_stable_path(dev), format="raw", size=int(dev.get("size") or 0),
               model=(dev.get("model") or "").strip(), serial=(dev.get("serial") or "").strip(),
               transport=(dev.get("tran") or "").lower(), removable=bool(dev.get("rm")),
               rotational=bool(dev.get("rota")), partition_table=dev.get("pttype") or "")
    vendor = (dev.get("vendor") or "").strip()
    s.label = " ".join(x for x in (vendor, s.model) if x) or s.path
    for child in dev.get("children") or []:
        s.partitions.append(Partition(path=child.get("path") or "", size=int(child.get("size") or 0),
                                      fstype=child.get("fstype") or "", label=child.get("label") or "",
                                      parttype=child.get("parttype") or "",
                                      mountpoints=_mounts(child)))
    _assess_block(s, dev, usage)
    return s


def _assess_block(s: Source, dev: dict, usage: dict[str, str]) -> None:
    names = {dev.get("kname") or dev.get("name")}
    stack = list(dev.get("children") or [])
    while stack:
        c = stack.pop()
        names.add(c.get("kname") or c.get("name"))
        stack.extend(c.get("children") or [])
    hits = {n: usage[n] for n in names if n in usage}
    if any(mp in SAFE_ROOT_MOUNTS for mp in hits.values()):
        where = sorted(set(hits.values()) & set(SAFE_ROOT_MOUNTS))
        s.safe = False
        s.problem_code = "host-disk"
        s.problems.append(f"This disk holds the system you are running right now ({', '.join(where)}). "
                          "Rehome never imports the running system's disk.")
    elif hits:
        s.safe = False
        s.problem_code = "mounted"
        mps = ", ".join(sorted(set(hits.values())))
        s.problems.append(f"Partitions of this disk are mounted ({mps}). Unmount them first.")
    sysblock = Path("/sys/class/block")
    for n in names:
        holders = list((sysblock / n / "holders").glob("*")) if (sysblock / n).exists() else []
        if holders and not s.problem_code:
            s.safe = False
            s.problem_code = "in-use"
            s.problems.append(f"{n} is used by {', '.join(h.name for h in holders)} (RAID, LVM or "
                              "an opened encrypted volume). Stop that first.")
            break
    if dev.get("ro"):
        s.problems.append("The disk is write-protected (fine: Rehome only reads it).")


def list_disks(include_unsafe: bool = True) -> list[Source]:
    usage = host_usage()
    out = []
    for dev in _lsblk():
        if dev.get("type") not in ("disk",):
            continue
        name = dev.get("name") or ""
        if name.startswith(("zram", "ram", "fd", "sr")) or int(dev.get("size") or 0) == 0:
            continue
        if name.startswith(("loop", "nbd")) and not os.environ.get("REHOME_SHOW_LOOP"):
            continue
        s = _disk_source(dev, usage)
        if s.safe or include_unsafe:
            out.append(s)
    return out


def describe_block(path: str) -> Source:
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except FileNotFoundError:
        raise RehomeError(f"{path} does not exist.", fix="Check the device name with `rehome sources`.")
    if not stat.S_ISBLK(st.st_mode):
        raise RehomeError(f"{path} is not a disk.")
    devs = _lsblk(real)
    if not devs:
        raise RehomeError(f"{path} could not be examined.")
    dev = devs[0]
    if dev.get("type") == "part":
        s = _disk_source(dev, host_usage())
        s.problems.append("This is a single partition; Rehome will present it as a whole disk. "
                          "Importing the whole disk is usually what you want.")
        return s
    return _disk_source(dev, host_usage())


# --- image files ----------------------------------------------------------------------

def sniff_format(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError as e:
        raise RehomeError(f"{path} cannot be read: {e.strerror}.",
                          fix="Check that the file exists and that you may read it.")
    for off, magic, fmt in MAGIC:
        if head[off:off + len(magic)] == magic:
            return fmt
    try:
        size = os.path.getsize(path)
        if size >= 512:
            with open(path, "rb") as f:
                f.seek(size - 512)
                if f.read(8) == b"conectix":   # fixed VHD: footer only
                    return "vpc"
    except OSError:
        pass
    return "raw"


def describe_image(path: str) -> Source:
    from rehome.vm import disks
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise RehomeError(f"{path} does not exist.")
    if os.path.isdir(path):
        raise RehomeError(f"{path} is a folder, not a disk image.",
                          fix="Choose the image file itself (for split VMDKs, the small descriptor file).")
    fmt = sniff_format(path)
    s = Source(kind="image", path=path, stable_path=path, format=fmt,
               actual_size=os.stat(path).st_blocks * 512)
    if fmt == "ewf":
        from rehome.disk import ewf
        s.kind = "ewf"
        s.size = ewf.media_size(path)
        s.label = os.path.basename(path)
        return s
    info = disks.info(path, fmt=fmt)
    s.format = info.get("format", fmt)
    s.size = int(info.get("virtual-size", 0))
    s.label = os.path.basename(path)
    if info.get("encrypted"):
        s.encrypted_image = True
        s.safe = False
        s.problem_code = "encrypted-image"
        s.problems.append("This image file is encrypted by QEMU/VirtualBox; decrypt it first.")
    chain = info.get("full-backing-filename") or info.get("backing-filename")
    if chain:
        s.backing_chain.append(chain)
        if not os.path.exists(chain):
            s.safe = False
            s.problem_code = "missing-backing"
            s.problems.append(f"This image depends on {chain}, which is missing.")
    if s.format == "vhdx" and "parent" in json.dumps(info.get("format-specific", {})).lower():
        s.problems.append("Differencing VHDX disks are not supported by QEMU; merge it in Hyper-V first.")
    return s


def describe(path: str) -> Source:
    """Describe a source given by path (block device or image file)."""
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except FileNotFoundError:
        raise RehomeError(f"{path} does not exist.")
    if stat.S_ISBLK(st.st_mode):
        return describe_block(path)
    return describe_image(path)
