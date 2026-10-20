"""Linux details: kernel, initramfs, virtio readiness, fstab, GPU drivers, RTC."""

from __future__ import annotations

import logging
import re

from rehome.inspect import initramfs
from rehome.inspect.gfs import download_bytes, os_mounted, read_text
from rehome.inspect.model import DetectedOS

log = logging.getLogger(__name__)

DEVICE_NAME_RE = re.compile(r"^(/dev/(?:sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)"
                            r"(?:p?\d+)?|/dev/disk/by-(?:id|path)/\S+)")


def _version_key(v: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"[.\-+]", v)]


def kernels(g) -> list[str]:
    for base in ("/usr/lib/modules", "/lib/modules"):
        try:
            if g.is_dir(base):
                ks = [k for k in g.ls(base) if g.is_dir(f"{base}/{k}") and
                      (g.exists(f"{base}/{k}/modules.dep") or g.exists(f"{base}/{k}/kernel"))]
                if ks:
                    return sorted(ks, key=_version_key)
        except RuntimeError:
            continue
    return []


def initramfs_tool(g) -> str:
    for path, tool in (("/etc/mkinitcpio.conf", "mkinitcpio"), ("/etc/booster.yaml", "booster"),
                       ("/etc/initramfs-tools", "initramfs-tools"), ("/etc/dracut.conf", "dracut"),
                       ("/usr/lib/dracut", "dracut"), ("/usr/bin/mkinitcpio", "mkinitcpio"),
                       ("/usr/bin/dracut", "dracut")):
        if g.exists(path):
            return tool
    return ""


def initramfs_path(g, kernel: str, tool: str) -> str | None:
    cands = [f"/boot/initramfs-{kernel}.img", f"/boot/initrd.img-{kernel}", f"/boot/initrd-{kernel}",
             f"/boot/initramfs-{kernel}", f"/boot/initrd-{kernel}.img"]
    if tool in ("mkinitcpio", "booster"):
        # Arch names initramfs after the kernel package, not the version.
        try:
            arch = sorted(f for f in g.ls("/boot") if f.startswith(("initramfs-", "booster-"))
                          and "fallback" not in f)
            cands += [f"/boot/{f}" for f in arch]
        except RuntimeError:
            pass
    for c in cands:
        if g.is_file(c):
            return c
    return None


def builtin_modules(g, kernel: str) -> set[str]:
    out = set()
    for base in ("/usr/lib/modules", "/lib/modules"):
        text = read_text(g, f"{base}/{kernel}/modules.builtin")
        if text:
            for line in text.splitlines():
                out.add(line.rsplit("/", 1)[-1].split(".ko")[0].replace("-", "_"))
            break
    return out


def analyze(g, root: str, o: DetectedOS) -> None:
    with os_mounted(g, root, readonly=True):
        osr = read_text(g, "/etc/os-release") or read_text(g, "/usr/lib/os-release") or ""
        m = re.search(r'^VERSION_ID="?([^"\n]+)"?', osr, re.M)
        if m:
            o.version = m.group(1)
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', osr, re.M)
        if m and (not o.name or o.name.lower() in ("linux", o.distro)):
            o.name = m.group(1)
        ks = kernels(g)
        if ks:
            o.kernel = ks[-1]
            o.details["kernels"] = ks
        tool = initramfs_tool(g)
        o.initramfs_tool = tool
        if o.kernel:
            builtin = builtin_modules(g, o.kernel)
            if "virtio_blk" in builtin:
                o.virtio_ready = True
                o.details["virtio"] = "built into the kernel"
            path = initramfs_path(g, o.kernel, tool)
            if path:
                data = download_bytes(g, path)
                files = initramfs.list_files(data) if data else None
                if files is not None:
                    mods = initramfs.storage_modules(files)
                    o.details["initramfs"] = path
                    o.details["initramfs_storage_modules"] = sorted(mods)
                    if o.virtio_ready is None:
                        o.virtio_ready = "virtio_blk" in mods or "virtio_scsi" in mods
                    if not o.original_bus:
                        o.original_bus = _bus_from_modules(mods)
                else:
                    o.notes.append(f"The initramfs {path} could not be read; virtio support is "
                                   "assumed missing and will be added.")
        _fstab(g, o)
        _bootloader(g, o)
        _gpu(g, o)
        _network(g, o)
        adj = read_text(g, "/etc/adjtime")
        if adj and len(adj.splitlines()) >= 3:
            o.rtc = "localtime" if adj.splitlines()[2].strip().upper() == "LOCAL" else "utc"
        else:
            o.rtc = "utc"
        for path, name in (("/usr/bin/vmtoolsd", "open-vm-tools"),
                           ("/usr/sbin/VBoxService", "VirtualBox Guest Additions"),
                           ("/usr/bin/VBoxClient", "VirtualBox Guest Additions"),
                           ("/usr/sbin/hv_kvp_daemon", "Hyper-V daemons")):
            if g.exists(path) and name not in o.hypervisor_tools:
                o.hypervisor_tools.append(name)


def _bus_from_modules(mods: set[str]) -> str:
    for mod in ("nvme", "ahci", "virtio_blk", "virtio_scsi", "ata_piix", "mpt3sas", "mptsas",
                "vmw_pvscsi", "hv_storvsc", "xen_blkfront"):
        if mod in mods:
            return initramfs.STORAGE_MODULES[mod]
    return ""


def _fstab(g, o: DetectedOS) -> None:
    text = read_text(g, "/etc/fstab") or ""
    by_name = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        spec = line.split()[0]
        if DEVICE_NAME_RE.match(spec):
            by_name.append(spec)
    if by_name:
        o.details["fstab_device_names"] = by_name
        o.notes.append("/etc/fstab refers to disks by device name; Rehome will switch them to UUIDs.")
        if not o.original_bus:
            o.original_bus = _bus_from_name(by_name[0])


def _bus_from_name(dev: str) -> str:
    if "/nvme" in dev:
        return "nvme"
    if "/vd" in dev:
        return "virtio"
    if "/hd" in dev:
        return "ide"
    if "/xvd" in dev:
        return "xen"
    if "/sd" in dev or "ata-" in dev:
        return "sata"
    return ""


def _bootloader(g, o: DetectedOS) -> None:
    for path, name in (("/boot/grub/grub.cfg", "grub2"), ("/boot/grub2/grub.cfg", "grub2"),
                       ("/boot/loader/loader.conf", "systemd-boot"), ("/boot/efi/loader/loader.conf", "systemd-boot"),
                       ("/efi/loader/loader.conf", "systemd-boot"), ("/boot/grub/menu.lst", "grub-legacy"),
                       ("/etc/lilo.conf", "lilo"), ("/boot/refind_linux.conf", "rEFInd"),
                       ("/boot/limine.conf", "limine"), ("/boot/syslinux/syslinux.cfg", "syslinux")):
        if g.exists(path):
            o.bootloader = name
            break
    grub_default = read_text(g, "/etc/default/grub") or ""
    m = re.search(r'^GRUB_CMDLINE_LINUX(?:_DEFAULT)?="([^"]*)"', grub_default, re.M)
    if m:
        o.details["kernel_cmdline"] = m.group(1)
    if any(DEVICE_NAME_RE.match(tok.split("=", 1)[-1]) for tok in o.details.get("kernel_cmdline", "").split()
           if tok.startswith(("root=", "resume="))):
        o.notes.append("The kernel command line refers to a disk by device name; Rehome will fix it.")


def _gpu(g, o: DetectedOS) -> None:
    xorg = read_text(g, "/etc/X11/xorg.conf") or ""
    found = set(re.findall(r'Driver\s+"(nvidia|fglrx|amdgpu-pro)"', xorg))
    try:
        if g.is_dir("/etc/X11/xorg.conf.d"):
            for f in g.ls("/etc/X11/xorg.conf.d"):
                t = read_text(g, f"/etc/X11/xorg.conf.d/{f}") or ""
                found |= set(re.findall(r'Driver\s+"(nvidia|fglrx|amdgpu-pro)"', t))
    except RuntimeError:
        pass
    if o.kernel:
        for base in ("/usr/lib/modules", "/lib/modules"):
            try:
                hits = g.find(f"{base}/{o.kernel}") if g.is_dir(f"{base}/{o.kernel}") else []
            except RuntimeError:
                hits = []
            if any(h.rsplit("/", 1)[-1].startswith("nvidia.ko") for h in hits):
                found.add("nvidia")
            break
    if found:
        o.gpu_drivers = sorted(found)
        o.notes.append("A proprietary GPU driver is installed; Rehome will make the desktop fall "
                       "back to the generic driver inside the VM.")


def _network(g, o: DetectedOS) -> None:
    pinned = []
    try:
        if g.is_dir("/etc/netplan"):
            for f in g.ls("/etc/netplan"):
                t = read_text(g, f"/etc/netplan/{f}") or ""
                pinned += re.findall(r"^\s{2,4}(en\w+|eth\d+|wl\w+):", t, re.M)
        if g.is_dir("/etc/systemd/network"):
            for f in g.ls("/etc/systemd/network"):
                t = read_text(g, f"/etc/systemd/network/{f}") or ""
                pinned += re.findall(r"^Name=(\S+)", t, re.M)
    except RuntimeError:
        pass
    if pinned:
        o.details["pinned_interfaces"] = sorted(set(pinned))
