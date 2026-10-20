"""Windows NT family details: version/build/edition, drivers, hibernation, HAL."""

from __future__ import annotations

import logging
from functools import lru_cache

from rehome import paths
from rehome.inspect import pe
from rehome.inspect.gfs import ci_path, os_mounted, read
from rehome.inspect.model import DetectedOS
from rehome.inspect.registry import Hive, control_set, hive_paths
from rehome.util import toml

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def driver_data() -> dict:
    return toml.load(paths.data_files_dir() / "windows" / "drivers.toml")


def friendly_name(product: str, build: int, display_version: str, edition_id: str) -> str:
    name = product or "Windows"
    if build >= 22000 and "Windows 10" in name:
        # Windows 11 still reports "Windows 10" in ProductName.
        name = name.replace("Windows 10", "Windows 11")
    if display_version and display_version not in name:
        name = f"{name} {display_version}"
    return name


HIBER_SIGNATURES = (b"hibr", b"HIBR", b"wake", b"WAKE", b"RSTR", b"rstr")


def hibernation_state(g, mountpoint: str = "/") -> str:
    """'hibernated', 'clean' or '' (no hiberfil.sys) for the mounted system volume."""
    data = read(g, f"{mountpoint.rstrip('/')}/hiberfil.sys", limit=4096)
    if data is None:
        return ""
    sig = data[:4]
    if sig in (b"hibr", b"HIBR", b"RSTR", b"rstr"):
        return "hibernated"
    return "clean"


HAL_KINDS = {
    "halacpi.dll": "ACPI uniprocessor PC (PIC)",
    "halaacpi.dll": "ACPI uniprocessor PC (APIC)",
    "halmacpi.dll": "ACPI multiprocessor PC",
    "hal.dll": "Standard PC (non-ACPI)",
    "halapic.dll": "MPS uniprocessor PC",
    "halmps.dll": "MPS multiprocessor PC",
    "halsp.dll": "Compaq SystemPro",
}


def detect_hal(g, sysroot: str) -> str:
    data = read(g, f"{sysroot}/system32/hal.dll", limit=8 * 2**20)
    if not data:
        return ""
    orig = (pe.string_value(data, "OriginalFilename") or "").lower()
    return orig if orig in HAL_KINDS else ""


def analyze(g, root: str, o: DetectedOS) -> None:
    drv = driver_data()
    storage = drv.get("storage", {})
    try:
        sysroot = g.inspect_get_windows_systemroot(root)
    except RuntimeError:
        sysroot = "/Windows"
    o.details["systemroot"] = sysroot
    system, software = hive_paths(g, root)
    ccs = control_set(g, root)
    o.details["control_set"] = ccs
    with os_mounted(g, root, readonly=True):
        try:
            with Hive(g, software) as h:
                cv = h.values("Microsoft\\Windows NT\\CurrentVersion")
        except Exception as e:  # damaged/missing hive
            cv = {}
            o.warnings.append(f"The SOFTWARE registry hive could not be read ({e}).")
        build = cv.get("CurrentBuildNumber") or cv.get("CurrentBuild") or o.build
        try:
            build_n = int(str(build).strip() or 0)
        except ValueError:
            build_n = 0
        o.build = str(build_n) if build_n else o.build
        major, minor = cv.get("CurrentMajorVersionNumber"), cv.get("CurrentMinorVersionNumber")
        if isinstance(major, int):
            o.version = f"{major}.{minor or 0}"
        elif cv.get("CurrentVersion"):
            o.version = str(cv["CurrentVersion"])
        o.product_name = cv.get("ProductName", o.product_name)
        edition_id = cv.get("EditionID", "")
        install_type = str(cv.get("InstallationType", "")).lower()
        o.details.update({"edition_id": edition_id, "installation_type": install_type,
                          "display_version": cv.get("DisplayVersion") or cv.get("ReleaseId") or "",
                          "ubr": cv.get("UBR", "")})
        if not o.edition:
            o.edition = "server" if "server" in install_type or "server" in o.product_name.lower() \
                else "client"
        o.name = friendly_name(o.product_name, build_n, o.details["display_version"], edition_id)
        if "reactos" in o.product_name.lower():
            o.distro = "reactos"
        try:
            with Hive(g, system) as h:
                services = f"{ccs}\\Services"
                boot = {}
                for name in storage:
                    start = h.get(f"{services}\\{name}", "Start")
                    if start is not None:
                        boot[name] = start
                o.boot_drivers = boot
                present = _present_pci_services(h, f"{ccs}\\Enum\\PCI")
                o.details["pci_services"] = sorted(present)
                o.original_bus = guess_bus(boot, present, storage)
                power = h.values(f"{ccs}\\Control\\Session Manager\\Power")
                o.fast_startup = bool(power.get("HiberbootEnabled"))
                o.details["hibernate_enabled"] = power.get("HibernateEnabled")
                tz = h.values(f"{ccs}\\Control\\TimeZoneInformation")
                o.rtc = "utc" if tz.get("RealTimeIsUniversal") else "localtime"
                for group in drv.get("hostile", []):
                    hits = [s for s in group["services"] if h.exists(f"{services}\\{s}")]
                    if hits and any(w in group["name"] for w in ("VMware", "VirtualBox", "Parallels", "Xen")):
                        o.hypervisor_tools.append(group["name"])
                bitlocker = h.get(f"{ccs}\\Control\\BitlockerStatus", "BootStatus")
                if bitlocker:
                    o.details["bitlocker_boot_status"] = bitlocker
        except Exception as e:
            o.warnings.append(f"The SYSTEM registry hive could not be read ({e}).")
        state = hibernation_state(g, "/")
        if state == "hibernated":
            o.hibernated = True
            o.warnings.append(
                "Windows is hibernated or was shut down with Fast Startup. Booting it on new "
                "hardware would resume a session that cannot work here.")
        if o.version.startswith("5.") or o.version.startswith("4."):
            o.hal = detect_hal(g, sysroot)
        o.bootloader = _windows_bootloader(g)
    if o.details.get("display_version") == "":
        o.details.pop("display_version")


def _present_pci_services(h: Hive, enum_pci: str) -> set[str]:
    out = set()
    for dev in h.subkeys(enum_pci):
        for inst in h.subkeys(f"{enum_pci}\\{dev}"):
            svc = h.get(f"{enum_pci}\\{dev}\\{inst}", "Service")
            if isinstance(svc, str) and svc:
                out.add(svc)
    return out


def guess_bus(boot: dict, present: set[str], storage: dict) -> str:
    """Which controller did Windows boot from on the old machine?"""
    lower_present = {p.lower() for p in present}
    ranked = []
    for name, start in boot.items():
        info = storage.get(name, {})
        if start == 0 and name.lower() in lower_present:
            ranked.append((0, info.get("bus", "")))
        elif name.lower() in lower_present:
            ranked.append((1, info.get("bus", "")))
    if not ranked:
        for name, start in boot.items():
            if start == 0 and storage.get(name, {}).get("bus") and not storage[name].get("builtin"):
                ranked.append((2, storage[name]["bus"]))
    if not ranked:
        return ""
    ranked.sort()
    bus = ranked[0][1]
    return {"hyperv": "scsi"}.get(bus, bus)


def _windows_bootloader(g) -> str:
    if ci_path(g, "/bootmgr"):
        return "bootmgr"
    if ci_path(g, "/ntldr"):
        return "ntldr"
    return ""
