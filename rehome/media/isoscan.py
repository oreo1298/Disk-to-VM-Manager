"""Recognise an installer ISO from its files, when libosinfo does not know it.

libosinfo identifies media by volume labels and a few fields from its
database; new or derived distributions (Omarchy, Mint, ...) and many old
Windows discs are missing, and without libosinfo nothing is recognised. This
module reads the ISO 9660 file system itself (standard library only, a few
sectors) and looks at the files installers are known by:

    Windows NT 4 / 2000 / XP / 2003   I386/TXTSETUP.SIF ([SetupData] versions), WIN51* markers
    Windows Vista and later           sources/idwbinfo.txt (build branch); UDF-only discs by label
    Windows 95 / 98 / Me              WIN95, WIN98, WIN9X folders
    Linux                             arch/ (archiso), casper/, live/, .disk/info, .treeinfo, ...
    BSD, ReactOS, FreeDOS             bin/freebsd-version, <ver>/<arch>/bsd.rd, reactos/, freedos/
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

SECTOR = 2048


class NotIso(Exception):
    pass


@dataclass
class Entry:
    name: str
    lba: int
    size: int
    is_dir: bool


class Iso9660:
    """Read-only access to an ISO 9660 image (Joliet or Rock Ridge names, case-insensitive)."""

    def __init__(self, path: str):
        self.f = open(path, "rb")
        self.volume_id = ""
        self._root: Entry | None = None
        self._joliet = False
        self._dirs: dict[int, list[Entry]] = {}
        pvd_root = None
        for i in range(16, 32):
            d = self._sector(i)
            if len(d) < SECTOR or d[1:6] != b"CD001":
                break
            kind = d[0]
            if kind == 1 and pvd_root is None:
                self.volume_id = d[40:72].decode("ascii", "replace").strip()
                pvd_root = self._record(d[156:190])
            elif kind == 2 and d[88:91] in (b"%/@", b"%/C", b"%/E"):     # Joliet
                self._root = self._record(d[156:190], joliet=True)
                self._joliet = True
            elif kind == 255:
                break
        if pvd_root is None:
            self.f.close()
            raise NotIso(path)
        if self._root is None:
            self._root = pvd_root

    def close(self) -> None:
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sector(self, n: int, count: int = 1) -> bytes:
        self.f.seek(n * SECTOR)
        return self.f.read(count * SECTOR)

    def _record(self, rec: bytes, joliet: bool = False) -> Entry:
        lba = struct.unpack_from("<I", rec, 2)[0]
        size = struct.unpack_from("<I", rec, 10)[0]
        flags = rec[25]
        nlen = rec[32]
        raw = rec[33:33 + nlen]
        if joliet:
            name = raw.decode("utf-16-be", "replace")
        else:
            name = raw.decode("ascii", "replace")
            rr = _rock_ridge_name(rec, 33 + nlen + (1 - nlen % 2))
            if rr:
                name = rr
        name = name.split(";")[0]
        if name.endswith(".") and not (flags & 2):
            name = name[:-1]
        return Entry(name=name, lba=lba, size=size, is_dir=bool(flags & 2))

    def _list(self, d: Entry) -> list[Entry]:
        if d.lba in self._dirs:
            return self._dirs[d.lba]
        data = self._sector(d.lba, max(1, min(64, (d.size + SECTOR - 1) // SECTOR)))
        out, pos = [], 0
        while pos < len(data):
            n = data[pos]
            if n == 0:                       # records never cross a sector boundary
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            rec = data[pos:pos + n]
            if len(rec) >= 34 and not (rec[32] == 1 and rec[33] in (0, 1)):   # skip . and ..
                out.append(self._record(rec, self._joliet))
            pos += n
        self._dirs[d.lba] = out
        return out

    def find(self, path: str) -> Entry | None:
        cur = self._root
        for part in [p for p in path.strip("/").split("/") if p]:
            if cur is None or not cur.is_dir:
                return None
            want = part.lower()
            cur = next((e for e in self._list(cur) if e.name.lower() == want), None)
        return cur

    def exists(self, path: str) -> bool:
        return self.find(path) is not None

    def isdir(self, path: str) -> bool:
        e = self.find(path)
        return bool(e and e.is_dir)

    def listdir(self, path: str = "/") -> list[str]:
        e = self.find(path) if path.strip("/") else self._root
        return [x.name for x in self._list(e)] if e and e.is_dir else []

    def read(self, path: str, limit: int = 256 * 1024) -> bytes | None:
        e = self.find(path)
        if e is None or e.is_dir:
            return None
        self.f.seek(e.lba * SECTOR)
        return self.f.read(min(e.size, limit))

    def text(self, path: str) -> str:
        data = self.read(path)
        if data is None:
            return ""
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16", "replace")
        return data.decode("utf-8", "replace")


def _rock_ridge_name(rec: bytes, start: int) -> str:
    """The Rock Ridge alternate name (NM entries) in a directory record's system use area."""
    pos, name = start, ""
    while pos + 4 <= len(rec):
        sig, ln = rec[pos:pos + 2], rec[pos + 2]
        if ln < 4 or pos + ln > len(rec):
            break
        if sig == b"NM":
            name += rec[pos + 5:pos + ln].decode("utf-8", "replace")
        pos += ln
    return name


# --- recognition ---------------------------------------------------------------------------

@dataclass
class Guess:
    short_id: str            # libosinfo short id when one fits ("winxp", "archlinux"), else ""
    name: str                # what to show, e.g. "Omarchy (based on Arch Linux)"
    family: str              # libosinfo-style family: winnt / win9x / linux / freebsd / ...
    distro: str = ""
    version: str = ""
    arch: str = ""
    evidence: str = ""       # what gave it away (for `rehome os-detect`)


# Windows build branches (sources/idwbinfo.txt) -> libosinfo short id.
BRANCHES = [
    (r"^(lh|longhorn|vista)", "winvista"), (r"^win7", "win7"), (r"^win8_", "win8"),
    (r"^winblue", "win8.1"), (r"^(co|ni|ge|dt|br)_release", "win11"),
    (r"^fe_release", "win2k22"),
    (r"^(th\d|rs\d|rs_prerelease|19h\d|vb_release|mn_release)", "win10"),
]
# Labels of Windows discs whose ISO 9660 layer is empty (UDF only) or that lack markers.
WINDOWS_LABELS = [
    (r"^(J_)?(CCCOMA|CPBA|CCSA|CES[A-Z]*)_", "win11"), (r"^SSS_X64", "win2k22"),
    (r"^IR\d_|^IRM_|^IR5_", "win8.1"), (r"^HRM_|^HB1_", "win8"),
    (r"^G[A-Z]{1,3}1?(RMC|SP1RMC)|^GRMC|^GSP1RMC", "win7"),
    (r"^(LRMC|FB1E|LR1C)", "winvista"),
    (r"^(WX[PH]|GRTM|GX[A-Z]|VRM|XRM|WXP)", "winxp"),
]
XP_EDITIONS = {"WIN51IP": "Professional", "WIN51IC": "Home Edition", "WIN51MC": "Media Center",
               "WIN51AP": "Professional x64"}
SERVER_2003 = ("WIN51IS", "WIN51IA", "WIN51IB", "WIN51ID", "WIN51IL", "WIN51IE")


def _sif_version(sif: str) -> tuple[int, int] | None:
    sect = re.search(r"\[SetupData\](.*?)(?:\n\[|\Z)", sif, re.S | re.I)
    body = sect.group(1) if sect else sif
    ma = re.search(r"^\s*MajorVersion\s*=\s*(\d+)", body, re.M | re.I)
    mi = re.search(r"^\s*MinorVersion\s*=\s*(\d+)", body, re.M | re.I)
    return (int(ma.group(1)), int(mi.group(1))) if ma and mi else None


def _brand(label: str) -> str:
    word = re.split(r"[_\-\s.]+", label.strip())[0] if label.strip() else ""
    return word.capitalize() if word.isalpha() else ""


def _windows(iso: Iso9660, label: str) -> Guess | None:
    root = {n.upper() for n in iso.listdir("/")}
    for arch_dir, arch in (("i386", "i386"), ("amd64", "x86_64")):
        sif = iso.text(f"{arch_dir}/txtsetup.sif")
        ver = _sif_version(sif) if sif else None
        if not ver:
            continue
        major, minor = ver
        if (major, minor) == (4, 0):
            return Guess("winnt4.0", "Windows NT 4.0", "winnt", "winnt", "4.0", arch, "TXTSETUP.SIF")
        if (major, minor) == (5, 0):
            return Guess("win2k", "Windows 2000", "winnt", "win2k", "5.0", arch, "TXTSETUP.SIF")
        if (major, minor) == (5, 1) or "WIN51AP" in root:
            edition = next((v for k, v in XP_EDITIONS.items() if k in root), "")
            return Guess("winxp", f"Windows XP {edition}".strip(), "winnt", "winxp",
                         "5.1" if major == 5 and minor == 1 else "5.2", arch, "TXTSETUP.SIF")
        if (major, minor) == (5, 2):
            return Guess("win2k3", "Windows Server 2003", "winnt", "win2k3", "5.2", arch,
                         "TXTSETUP.SIF")
    if iso.isdir("win98") or iso.isdir("win9x") or iso.isdir("win95"):
        if iso.isdir("win9x"):
            return Guess("winme", "Windows Me", "win9x", "winme", "4.90", "i386", "WIN9X folder")
        if iso.isdir("win98"):
            return Guess("win98", "Windows 98", "win9x", "win98", "4.10", "i386", "WIN98 folder")
        return Guess("win95", "Windows 95", "win9x", "win95", "4.0", "i386", "WIN95 folder")
    info = iso.text("sources/idwbinfo.txt")
    if info:
        branch = re.search(r"BuildBranch\s*=\s*(\S+)", info)
        arch = re.search(r"BuildArch\s*=\s*(\S+)", info)
        a = "x86_64" if arch and arch.group(1).lower() in ("amd64", "x64") else "i386" if arch else ""
        if branch:
            for pattern, sid in BRANCHES:
                if re.search(pattern, branch.group(1), re.I):
                    return Guess(sid, _WINDOWS_NAMES[sid], "winnt", sid, "", a,
                                 f"build branch {branch.group(1)}")
    if iso.exists("sources/install.wim") or iso.exists("sources/install.esd") or \
            "UDF" in iso.text("readme.txt").upper():
        for pattern, sid in WINDOWS_LABELS:
            if re.search(pattern, label, re.I):
                return Guess(sid, _WINDOWS_NAMES[sid], "winnt", sid, "", "", f"volume label {label}")
        if "UDF" in iso.text("readme.txt").upper():
            # Windows 8 and later hide everything but README.TXT from ISO 9660.
            return Guess("win10", "Windows (8 or later)", "winnt", "win10", "", "",
                         "Windows setup files (UDF)")
        return Guess("win7", "Windows (Vista or 7)", "winnt", "win7", "", "",
                     "Windows setup files")
    return None


_WINDOWS_NAMES = {"winvista": "Windows Vista", "win7": "Windows 7", "win8": "Windows 8",
                  "win8.1": "Windows 8.1", "win10": "Windows 10", "win11": "Windows 11",
                  "win2k22": "Windows Server 2022", "winxp": "Windows XP"}


def _disk_info(iso: Iso9660) -> str:
    return iso.text(".disk/info").strip().splitlines()[0].strip() if iso.exists(".disk/info") else ""


def _linux(iso: Iso9660, label: str) -> Guess | None:
    info = _disk_info(iso)
    if iso.isdir("arch") and (iso.exists("arch/version") or iso.isdir("arch/boot")):
        brand = _brand(label)
        if not brand or label.upper().startswith("ARCH"):
            return Guess("archlinux", "Arch Linux", "linux", "arch", "", "x86_64", "arch/ (archiso)")
        return Guess("archlinux", f"{brand} (based on Arch Linux)", "linux", "arch", "", "x86_64",
                     f"arch/ (archiso), volume label {label}")
    if info:
        m = re.match(r"(.+?)\s+(\d+(?:\.\d+)*)", info)
        name = (m.group(1) + " " + m.group(2)) if m else info.split(" - ")[0]
        low = info.lower()
        if low.startswith("ubuntu") or iso.isdir("casper"):
            ver = m.group(2) if m else ""
            sid = f"ubuntu{'.'.join(ver.split('.')[:2])}" if low.startswith("ubuntu") and ver else ""
            return Guess(sid, name, "linux", "ubuntu", ver, "", ".disk/info")
        if low.startswith("debian"):
            ver = m.group(2).split(".")[0] if m else ""
            return Guess(f"debian{ver}" if ver else "", name, "linux", "debian", ver, "",
                         ".disk/info")
        return Guess("", name, "linux", "", m.group(2) if m else "", "", ".disk/info")
    tree = iso.text(".treeinfo")
    if tree:
        fam = re.search(r"^\s*family\s*=\s*(.+)$", tree, re.M)
        ver = re.search(r"^\s*version\s*=\s*(\S+)", tree, re.M)
        name = (fam.group(1).strip() if fam else "Linux") + (f" {ver.group(1)}" if ver else "")
        sid = ""
        if fam and fam.group(1).strip().lower() == "fedora" and ver:
            sid = f"fedora{ver.group(1)}"
        distro = (fam.group(1).split()[0].lower() if fam else "")
        return Guess(sid, name, "linux", distro, ver.group(1) if ver else "", "", ".treeinfo")
    if iso.isdir("casper"):
        return Guess("", _brand(label) or "Ubuntu-based Linux", "linux", "ubuntu", "", "", "casper/")
    if iso.exists("boot/x86_64/loader/linux"):
        return Guess("opensusetumbleweed" if "tumbleweed" in label.lower() else "",
                     "openSUSE" if "suse" in label.lower() else (_brand(label) or "SUSE Linux"),
                     "linux", "opensuse", "", "x86_64", "boot/x86_64/loader")
    if iso.isdir("live") or iso.isdir("isolinux") or iso.isdir("syslinux") or \
            iso.exists("images/pxeboot/vmlinuz") or iso.isdir("boot/grub"):
        has_kernel = any(n.lower().startswith(("vmlinuz", "bzimage", "linux"))
                         for d in ("/", "live", "isolinux", "boot", "casper", "images/pxeboot")
                         for n in iso.listdir(d))
        if has_kernel:
            return Guess("", _brand(label) or "Linux", "linux", "", "", "", "Linux kernel")
    return None


def _bsd_and_others(iso: Iso9660, label: str) -> Guess | None:
    fv = iso.text("bin/freebsd-version")
    if fv:
        m = re.search(r'USERLAND_VERSION="?(\d+)\.(\d+)', fv)
        ver = f"{m.group(1)}.{m.group(2)}" if m else ""
        return Guess(f"freebsd{ver}" if ver else "", f"FreeBSD {ver}".strip(), "freebsd",
                     "freebsd", ver, "", "bin/freebsd-version")
    for name in iso.listdir("/"):
        if re.fullmatch(r"\d+\.\d+", name) and iso.isdir(name):
            for arch in iso.listdir(name):
                if iso.exists(f"{name}/{arch}/bsd.rd"):
                    return Guess(f"openbsd{name}", f"OpenBSD {name}", "openbsd", "openbsd", name,
                                 "x86_64" if arch == "amd64" else "i386", f"{name}/{arch}/bsd.rd")
    if iso.exists("netbsd") and (iso.isdir("amd64") or iso.isdir("i386")):
        return Guess("", "NetBSD", "netbsd", "netbsd", "", "", "netbsd kernel")
    if iso.isdir("reactos"):
        return Guess("reactos0.4", "ReactOS", "reactos", "reactos", "0.4", "i386", "reactos/")
    if iso.isdir("freedos") or (re.match(r"^FD1\d", label, re.I) and iso.isdir("packages")):
        return Guess("freedos1.3", "FreeDOS", "msdos", "freedos", "1.3", "i386", "freedos/")
    return None


def detect(path: str) -> Guess | None:
    """Recognise the operating system on an installer ISO (None if not recognised)."""
    try:
        iso = Iso9660(path)
    except (OSError, NotIso):
        return None
    with iso:
        label = iso.volume_id
        try:
            return (_windows(iso, label) or _bsd_and_others(iso, label) or _linux(iso, label))
        except (OSError, struct.error, IndexError, UnicodeError):
            return None


def volume_id(path: str) -> str:
    try:
        with Iso9660(path) as iso:
            return iso.volume_id
    except (OSError, NotIso):
        return ""
