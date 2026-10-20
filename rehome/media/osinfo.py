"""libosinfo integration: recognise installer media and map OSes to facts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

log = logging.getLogger(__name__)

# NT version and build for libosinfo's Windows short IDs (libosinfo itself
# reports marketing versions such as "11.0").
WINDOWS_IDS = {
    "win2k": ("5.0", 2195, "client"), "winxp": ("5.1", 2600, "client"),
    "win2k3": ("5.2", 3790, "server"), "win2k3r2": ("5.2", 3790, "server"),
    "winvista": ("6.0", 6002, "client"), "win2k8": ("6.0", 6002, "server"),
    "win7": ("6.1", 7601, "client"), "win2k8r2": ("6.1", 7601, "server"),
    "win8": ("6.2", 9200, "client"), "win2k12": ("6.2", 9200, "server"),
    "win8.1": ("6.3", 9600, "client"), "win2k12r2": ("6.3", 9600, "server"),
    "win10": ("10.0", 10240, "client"), "win2k16": ("10.0", 14393, "server"),
    "win2k19": ("10.0", 17763, "server"), "win2k22": ("10.0", 20348, "server"),
    "win11": ("10.0", 22000, "client"), "win2k25": ("10.0", 26100, "server"),
    "winnt4.0": ("4.0", 1381, "client"),
}
WIN9X = {"win95": "win95", "win98": "win98", "winme": "winme"}
BSD_FAMILIES = {"freebsd", "netbsd", "openbsd", "dragonfly"}


def available() -> bool:
    try:
        _db()
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def _db():
    import gi
    gi.require_version("Libosinfo", "1.0")
    from gi.repository import Libosinfo
    loader = Libosinfo.Loader()
    loader.process_default_path()
    return loader.get_db()


def _lib():
    _db()  # ensures gi.require_version() ran first
    from gi.repository import Libosinfo
    return Libosinfo


@dataclass
class OsEntry:
    short_id: str
    name: str
    id: str
    family: str
    distro: str
    version: str
    vendor: str = ""
    ram_mib: int = 0
    min_ram_mib: int = 0
    storage_bytes: int = 0
    cpus: int = 0
    efi: bool | None = None          # True: UEFI supported/preferred; False: BIOS only
    virtio_block: bool = False
    virtio_net: bool = False
    devices: list[str] = field(default_factory=list)
    eol: bool = False

    def facts(self) -> dict:
        return facts_for(self)


def _resources(res_list, arch="x86_64"):
    best = None
    for r in res_list.get_elements():
        a = r.get_architecture()
        if a in (arch, "all"):
            best = r
            if a == arch:
                break
    return best


def entry(os_obj) -> OsEntry:
    e = OsEntry(short_id=os_obj.get_short_id() or "", name=os_obj.get_name() or "",
                id=os_obj.get_id(), family=os_obj.get_family() or "",
                distro=os_obj.get_distro() or "", version=os_obj.get_version() or "",
                vendor=os_obj.get_vendor() or "")
    try:
        rec = _resources(os_obj.get_recommended_resources())
        mn = _resources(os_obj.get_minimum_resources())
        if rec:
            e.ram_mib = max(0, rec.get_ram()) // 2**20 if rec.get_ram() > 0 else 0
            e.storage_bytes = max(0, rec.get_storage())
            e.cpus = max(0, rec.get_n_cpus())
        if mn and mn.get_ram() > 0:
            e.min_ram_mib = mn.get_ram() // 2**20
    except Exception:  # older libosinfo
        pass
    try:
        devs = [d.get_name() for d in os_obj.get_all_devices(None).get_elements()]
        e.devices = devs
        e.virtio_block = any(d in ("virtio-block", "virtio1.0-block") for d in devs)
        e.virtio_net = any(d in ("virtio-net", "virtio1.0-net") for d in devs)
    except Exception:
        pass
    try:
        fws = os_obj.get_complete_firmware_list(None).get_elements()
        types = {f.get_firmware_type(): f.is_supported() for f in fws}
        if types.get("efi"):
            e.efi = True
        elif types.get("bios") is False:
            e.efi = True
        elif "efi" in types and not types["efi"]:
            e.efi = False
    except Exception:
        pass
    try:
        e.eol = bool(os_obj.get_eol_date_string())
    except Exception:
        pass
    return e


def facts_for(e: OsEntry) -> dict:
    sid = e.short_id
    f: dict = {"name": e.name, "osinfo_id": e.id, "short_id": sid, "arch": "x86_64"}
    if e.family == "winnt" or sid in WINDOWS_IDS:
        ver, build, edition = WINDOWS_IDS.get(sid, (e.version or "10.0", 0, "client"))
        f.update(family="windows", distro="windows", version=ver, build=build, edition=edition)
    elif e.family == "win9x" or sid in WIN9X:
        f.update(family="win9x", distro=WIN9X.get(sid, "win98"), version=e.version, arch="i386")
    elif e.family in ("dos", "msdos"):
        f.update(family="dos", distro=e.distro or "dos", version=e.version, arch="i386")
    elif e.family == "linux":
        f.update(family="linux", distro=e.distro, version=e.version)
    elif e.family in BSD_FAMILIES:
        f.update(family="bsd", distro=e.family, version=e.version)
    elif e.family == "darwin":
        f.update(family="macos", distro="macos", version=e.version)
    elif sid.startswith("reactos"):
        f.update(family="windows", distro="reactos", version="5.2", edition="client")
    else:
        f.update(family="other", distro=e.distro, version=e.version)
    if e.efi is not None:
        f["efi"] = e.efi
    return f


def by_short_id(short_id: str) -> OsEntry | None:
    db = _db()
    o = None
    try:
        o = db.get_os_by_short_id(short_id)
    except Exception:
        pass
    if o is None:
        for cand in db.get_os_list().get_elements():
            if cand.get_short_id() == short_id or cand.get_id() == short_id:
                o = cand
                break
    return entry(o) if o else None


def search(text: str, limit: int = 30) -> list[OsEntry]:
    text = text.lower()
    out = []
    for o in _db().get_os_list().get_elements():
        name = (o.get_name() or "").lower()
        sid = (o.get_short_id() or "").lower()
        if text in name or text in sid:
            out.append(entry(o))
    out.sort(key=lambda e: (e.eol, e.name))
    return out[:limit]


@dataclass
class MediaInfo:
    path: str
    os: OsEntry | None
    volume_id: str = ""
    installer: bool = True
    live: bool = False
    arch: str = ""

    @property
    def label(self) -> str:
        return self.os.name if self.os else (self.volume_id or "Unknown installer")


def identify_media(path: str) -> MediaInfo:
    """Identify an installer ISO. Falls back to volume-label heuristics."""
    Lib = _lib()
    try:
        media = Lib.Media.create_from_location(path, None)
    except Exception as e:  # not an ISO9660 image, unreadable, ...
        log.debug("osinfo media detection failed: %s", e)
        return MediaInfo(path=path, os=None)
    vol = media.get_volume_id() or ""
    info = MediaInfo(path=path, os=None, volume_id=vol, arch=media.get_architecture() or "")
    db = _db()
    try:
        matched = db.identify_media(media)
    except Exception:
        matched = False
    if matched:
        os_obj = media.get_os()
        info.os = entry(os_obj) if os_obj else None
        try:
            info.installer = media.get_installer()
            info.live = media.get_live()
        except Exception:
            pass
    else:
        guess = guess_from_label(vol)
        if guess:
            info.os = by_short_id(guess) or KNOWN_EXTRA.get(guess)
    return info


# Operating systems that osinfo-db does not (always) describe.
KNOWN_EXTRA = {
    "reactos0.4": OsEntry(short_id="reactos0.4", name="ReactOS", id="https://reactos.org/reactos/0.4",
                          family="reactos", distro="reactos", version="0.4", ram_mib=1024,
                          storage_bytes=8 * 2**30),
}


LABEL_GUESSES = [
    (r"^CCCOMA_|^CPBA_|^CCSA_|^SSS_X64", "win11"),
    (r"^FD1[34]", "freedos1.3"),
    (r"^ReactOS", "reactos0.4"),
    (r"^Ubuntu", "ubuntu24.04"),
    (r"^Fedora", "fedora41"),
    (r"^ARCH_", "archlinux"),
    (r"^Debian", "debian12"),
    (r"^openSUSE", "opensusetumbleweed"),
    (r"^14_\d_RELEASE|^FREEBSD", "freebsd14.0"),
]


def guess_from_label(label: str) -> str | None:
    for pattern, sid in LABEL_GUESSES:
        if re.search(pattern, label, re.I):
            return sid
    return None
