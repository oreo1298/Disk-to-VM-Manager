"""Identify what is installed on one or more disks.

    report = inspect([Drive("/path/disk.vmdk", "vmdk")], keys=["recovery key"])

All drives are attached read-only. Encrypted volumes are unlocked with the
keys given (BitLocker recovery keys, LUKS passphrases) when possible.
"""

from __future__ import annotations

import logging
from typing import Callable

from rehome.inspect import bootchain, layout, legacy, linux, windows
from rehome.inspect.gfs import Drive, ci_path, mounted, read_text, session
from rehome.inspect.model import DetectedOS, InspectionReport

log = logging.getLogger(__name__)

FAMILY = {"windows": "windows", "linux": "linux", "freebsd": "bsd", "netbsd": "bsd",
          "openbsd": "bsd", "dragonfly": "bsd", "hurd": "other", "minix": "other"}


def _unlock(g, report: InspectionReport, keys: list[str]) -> None:
    for disk in report.disks:
        for v in disk.volumes:
            if not v.encryption or v.encryption == "veracrypt":
                continue
            for i, key in enumerate(keys):
                name = f"rehome_crypt{abs(hash(v.device)) % 10000}"
                try:
                    kind = "bitlk" if v.encryption == "bitlocker" else None
                    if kind:
                        g.cryptsetup_open(v.device, key, name, readonly=True, crypttype=kind)
                    else:
                        g.cryptsetup_open(v.device, key, name, readonly=True)
                    v.unlocked = True
                    break
                except RuntimeError:
                    continue
            if not v.unlocked:
                report.encrypted_locked.append(v.device)


def _filesystems(g, report: InspectionReport) -> dict[str, str]:
    try:
        return g.list_filesystems()
    except RuntimeError as e:
        # parted refuses tables with partitions past the end of the disk
        # (truncated or rescued images); fall back to probing each device.
        report.notes.append(f"The partition table looks damaged ({e}); probing partitions "
                            "individually.")
        out = {}
        for dev in g.list_devices() + g.list_partitions():
            try:
                out[dev] = g.vfs_type(dev) or "unknown"
            except RuntimeError:
                out[dev] = "unknown"
        return out


def _drive_index(g, root: str) -> int:
    try:
        dev = g.part_to_dev(root)
    except RuntimeError:
        dev = root
    try:
        devs = g.list_devices()
        return devs.index(dev) if dev in devs else 0
    except RuntimeError:
        return 0


def _libguestfs_os(g, root: str) -> DetectedOS:
    typ = g.inspect_get_type(root)
    distro = g.inspect_get_distro(root)
    o = DetectedOS(root=root, family=FAMILY.get(typ, "other"),
                   distro=distro if distro != "unknown" else typ,
                   product_name=g.inspect_get_product_name(root),
                   arch=g.inspect_get_arch(root), hostname=g.inspect_get_hostname(root))
    if o.arch in ("i686", "i586", "i486"):
        o.arch = "i386"
    o.version = f"{g.inspect_get_major_version(root)}.{g.inspect_get_minor_version(root)}"
    try:
        o.osinfo = g.inspect_get_osinfo(root)
    except RuntimeError:
        pass
    try:
        o.build = g.inspect_get_build_id(root) if typ == "windows" else ""
    except RuntimeError:
        pass
    try:
        o.mountpoints = g.inspect_get_mountpoints(root)
    except RuntimeError:
        pass
    variant = ""
    try:
        variant = g.inspect_get_product_variant(root)
    except RuntimeError:
        pass
    if typ == "windows":
        o.edition = "server" if "server" in variant.lower() else "client" if variant != "unknown" else ""
        if "reactos" in o.product_name.lower():
            o.distro = "reactos"
    o.name = o.product_name if o.product_name != "unknown" else (distro or typ).title()
    o.disk_index = _drive_index(g, root)
    return o


def _boot_details(g, report: InspectionReport, o: DetectedOS) -> None:
    lay = report.disks[o.disk_index] if o.disk_index < len(report.disks) else None
    if lay is None:
        return
    if not o.boot_mode:
        o.boot_mode = layout.boot_mode_for(lay)
        # Linux on hybrid disks: trust /etc/fstab's /boot/efi.
        if o.family == "linux" and o.mountpoints:
            if any(mp in ("/boot/efi", "/efi") for mp in o.mountpoints):
                o.boot_mode = "uefi"
            elif lay.has_bios_boot or lay.scheme == "msdos":
                o.boot_mode = "bios"
    if o.family == "windows" and lay.scheme == "msdos" and not lay.has_esp:
        o.boot_mode = "bios"
    if o.boot_mode == "uefi":
        esp = next((v for d in report.disks for v in d.volumes
                    if v.part_type in (layout.ESP_GUID, "0xef")), None)
        if esp:
            try:
                with mounted(g, esp.device):
                    found = []
                    for path, name in (("/EFI/Microsoft/Boot/bootmgfw.efi", "Windows Boot Manager"),
                                       ("/EFI/systemd/systemd-bootx64.efi", "systemd-boot"),
                                       ("/EFI/refind", "rEFInd"), ("/EFI/VeraCrypt", "VeraCrypt"),
                                       ("/EFI/freebsd/loader.efi", "FreeBSD loader"),
                                       ("/EFI/BOOT/BOOTX64.EFI", "fallback loader")):
                        if ci_path(g, path):
                            found.append(name)
                    for d in ("/EFI/ubuntu", "/EFI/fedora", "/EFI/debian", "/EFI/opensuse",
                              "/EFI/arch", "/EFI/GRUB", "/EFI/grub", "/EFI/Manjaro", "/EFI/endeavouros"):
                        if ci_path(g, d):
                            found.append("GRUB")
                            break
                    o.details["esp_loaders"] = sorted(set(found))
                    if o.family == "windows" and not o.bootloader.startswith("Windows"):
                        o.bootloader = "Windows Boot Manager (UEFI)"
            except Exception as e:  # noqa: BLE001 - an unreadable ESP is not fatal
                log.debug("ESP probe failed: %s", e)
    else:
        o.details["mbr_loader"] = lay.mbr_loader
    if lay.mbr_loader in ("veracrypt", "truecrypt"):
        o.encryption.append("veracrypt")


def _bsd_details(g, o: DetectedOS) -> None:
    try:
        with mounted(g, o.root):
            text = read_text(g, "/bin/freebsd-version") or ""
            import re
            m = re.search(r'USERLAND_VERSION="([^"]+)"', text)
            if m:
                o.version = m.group(1).split("-")[0]
                o.name = f"FreeBSD {m.group(1)}"
            fstab = read_text(g, "/etc/fstab") or ""
            for line in fstab.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    dev = parts[0]
                    o.details["root_device"] = dev
                    if "/ada" in dev or "/ad" in dev:
                        o.original_bus = "sata" if "/ada" in dev else "ide"
                    elif "/nvd" in dev or "/nda" in dev:
                        o.original_bus = "nvme"
                    elif "/vtbd" in dev:
                        o.original_bus = "virtio"
                    elif "/da" in dev:
                        o.original_bus = "scsi"
            if ci_path(g, "/etc/wall_cmos_clock"):
                o.rtc = "localtime"
    except Exception as e:  # noqa: BLE001 - BSD filesystems are often not mountable
        o.notes.append("The BSD root filesystem could not be opened, so the original disk "
                       "controller is emulated to be safe.")
        log.debug("bsd probe failed: %s", e)


def inspect(drives: list[Drive], keys: list[str] | None = None,
            progress: Callable[[str], None] | None = None) -> InspectionReport:
    report = InspectionReport()
    step = progress or (lambda msg: None)
    step("Starting the inspection appliance")
    with session(drives) as g:
        try:
            v = g.version()
            report.libguestfs_version = f"{v['major']}.{v['minor']}.{v['release']}"
        except RuntimeError:
            pass
        step("Reading partition tables")
        filesystems = _filesystems(g, report)
        for i, dev in enumerate(g.list_devices()):
            report.disks.append(layout.analyze_disk(g, i, dev, filesystems))
        if keys:
            step("Unlocking encrypted volumes")
            _unlock(g, report, keys)
            filesystems = _filesystems(g, report)
        for disk in report.disks:
            for vol in disk.volumes:
                if vol.encryption and not vol.unlocked:
                    if vol.device not in report.encrypted_locked:
                        report.encrypted_locked.append(vol.device)
            if layout.veracrypt_system(g, disk):
                report.notes.append(f"Disk {disk.index + 1} uses VeraCrypt system encryption. It "
                                    "cannot be inspected, but it can be booted as-is: VeraCrypt "
                                    "asks for its password at start-up.")
        step("Identifying operating systems")
        roots = []
        try:
            roots = g.inspect_os()
        except RuntimeError as e:
            report.problems.append(f"libguestfs inspection failed: {e}")
        for root in roots:
            o = _libguestfs_os(g, root)
            step(f"Examining {o.name}")
            try:
                if o.family == "windows":
                    windows.analyze(g, root, o)
                elif o.family == "linux":
                    linux.analyze(g, root, o)
                elif o.family == "bsd":
                    _bsd_details(g, o)
            except Exception as e:  # noqa: BLE001 - keep partial results
                o.warnings.append(f"Some details could not be read: {e}")
                log.debug("analysis of %s failed", root, exc_info=True)
            _boot_details(g, report, o)
            report.oses.append(o)
        known = {o.root for o in report.oses}
        step("Looking for DOS and Windows 9x")
        for dev, fs in filesystems.items():
            if fs in legacy.FAT_TYPES and dev not in known:
                try:
                    o = legacy.probe_volume(g, dev)
                except Exception as e:  # noqa: BLE001
                    log.debug("legacy probe of %s failed: %s", dev, e)
                    continue
                if o:
                    o.disk_index = _drive_index(g, dev)
                    lay = report.disks[o.disk_index] if o.disk_index < len(report.disks) else None
                    if lay:
                        o.details["mbr_loader"] = lay.mbr_loader
                    try:
                        bc = bootchain.analyze(g, dev)
                        o.details["boot_chain"] = bc.to_dict()
                        if bc.problems:
                            o.notes.append(" ".join(bc.problems) + " Rehome repairs this in the VM's "
                                           "copy of the disk (the original is not changed).")
                    except RuntimeError as e:
                        log.debug("boot chain of %s: %s", dev, e)
                    report.oses.append(o)
        for disk in report.disks:
            for o in layout.special_oses(disk):
                if o.family == "bsd" and any(x.family == "bsd" for x in report.oses):
                    continue
                if o.family == "bsd":
                    _bsd_details(g, o)
                report.oses.append(o)
    _order(report)
    if not report.oses:
        if report.encrypted_locked:
            report.problems.append("The disk is encrypted. Provide the BitLocker recovery key or "
                                   "LUKS passphrase so Rehome can identify the system.")
        else:
            report.problems.append("No operating system was found on the selected disk(s).")
    return report


def _order(report: InspectionReport) -> None:
    """Put the most likely boot target first (Windows/Linux before DOS before other)."""
    rank = {"windows": 0, "linux": 1, "bsd": 2, "win9x": 3, "dos": 4, "macos": 9, "other": 8}
    report.oses.sort(key=lambda o: (o.disk_index, rank.get(o.family, 5)))
