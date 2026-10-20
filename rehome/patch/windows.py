"""Offline fixes for Windows NT-family installations (2000 through 11 / Server 2025).

The classic failure when moving Windows to new hardware is STOP 0x7B
INACCESSIBLE_BOOT_DEVICE: the driver for the new boot controller is not set
to load at boot. Rehome emulates a controller Windows has an in-box driver
for and makes that driver boot-start:

* Windows 2000/XP/2003: PIIX IDE + the "MergeIDE" CriticalDeviceDatabase fix.
* Vista/7: AHCI (msahci) and IDE (pciide/intelide) boot-start.
* 8 and later: storahci/stornvme boot-start and their StartOverride removed.
virtio drivers come later (virt-v2v or the guest tools step).
"""

from __future__ import annotations

from rehome.inspect.gfs import ci_path, mounted, os_mounted
from rehome.inspect.registry import REG_EXPAND_SZ, REG_MULTI_SZ, REG_SZ, Hive, control_set, hive_paths
from rehome.inspect.windows import driver_data, hibernation_state
from rehome.patch.registry import CONFIG, PatchResult, patch

WIN = ("windows",)


def _nt(ctx) -> tuple[int, int]:
    try:
        major, minor = (int(x) for x in (ctx.os.version or "0.0").split(".")[:2])
    except ValueError:
        return (0, 0)
    return (major, minor)


def _system_hive(ctx, write=True) -> Hive:
    system, _software = hive_paths(ctx.g, ctx.root)
    return Hive(ctx.g, system, write=write)


def _system_volume(ctx) -> str:
    return ctx.g.inspect_get_mountpoints(ctx.root).get("/", ctx.root)


@patch("windows.hibernation", "Discard the hibernation state", families=WIN)
def hibernation(ctx) -> PatchResult:
    o = ctx.os
    choice = getattr(ctx.options, "discard_hibernation", None)
    changes = []
    g = ctx.g
    dev = _system_volume(ctx)
    if o.hibernated:
        if not choice:
            return PatchResult("", "", "skipped",
                               "Windows is hibernated and you chose to keep the hibernation state. "
                               "It will try to resume on the virtual hardware, which usually fails.")
        mounted_ok = False
        for opts in ("remove_hiberfile", "force"):
            try:
                g.mount_options(opts, dev, "/")
                mounted_ok = True
                break
            except RuntimeError:
                continue
        if not mounted_ok:
            g.mount(dev, "/")
        try:
            if hibernation_state(g, "/") == "hibernated":
                p = ci_path(g, "/hiberfil.sys")
                if p:
                    g.rm(p)
            changes.append("removed hiberfil.sys (the hibernated session) from the VM's disk")
        finally:
            g.umount_all()
    with os_mounted(g, ctx.root, readonly=False):
        with _system_hive(ctx) as h:
            ccs = control_set(g, ctx.root)
            if h.set(f"{ccs}\\Control\\Session Manager\\Power", "HiberbootEnabled", 0):
                changes.append("turned off Fast Startup so the VM always shuts down fully")
            changes += [c.describe() for c in h.changes]
    if not changes:
        return PatchResult("", "", "not-needed", "Windows was shut down normally.")
    return PatchResult("", "", "applied", "Hibernation state handled.", changes)


@patch("windows.bitlocker", "BitLocker", kind=CONFIG, families=WIN)
def bitlocker(ctx) -> PatchResult:
    vols = [v for d in (ctx.report.disks if ctx.report else []) for v in d.volumes
            if v.encryption == "bitlocker"]
    if not vols:
        return PatchResult("", "", "not-needed", "No BitLocker volumes.")
    return PatchResult("", "", "info",
                       "BitLocker is enabled. The VM's TPM is new, so Windows will show the BitLocker "
                       "recovery screen once: enter the 48-digit recovery key. Afterwards you can "
                       "re-seal BitLocker to the virtual TPM from inside Windows (or suspend it with "
                       "`manage-bde -protectors -disable C:` before the next restart).")


SERVICE_DEFAULTS = {
    # name: (ImagePath, Group, Type, Tag)
    "atapi": ("system32\\drivers\\atapi.sys", "SCSI miniport", 1, 0x19),
    "intelide": ("system32\\drivers\\intelide.sys", "System Bus Extender", 1, 4),
    "pciide": ("system32\\drivers\\pciide.sys", "System Bus Extender", 1, 3),
    "msahci": ("system32\\drivers\\msahci.sys", "SCSI miniport", 1, None),
    "storahci": ("System32\\drivers\\storahci.sys", "SCSI Miniport", 1, None),
    "stornvme": ("System32\\drivers\\stornvme.sys", "SCSI Miniport", 1, None),
}


def _make_boot_start(ctx, h: Hive, ccs: str, name: str, create: bool) -> bool:
    key = f"{ccs}\\Services\\{name}"
    if not h.exists(key):
        if not create:
            return False
        image, group, typ, tag = SERVICE_DEFAULTS[name]
        driver = image.split("\\")[-1]
        sysroot = ctx.os.details.get("systemroot", "/Windows")
        if not ci_path(ctx.g, f"{sysroot}/system32/drivers/{driver}"):
            return False
        h.set(key, "ImagePath", image, REG_EXPAND_SZ)
        h.set(key, "Group", group, REG_SZ)
        h.set(key, "Type", typ)
        h.set(key, "ErrorControl", 1)
        if tag is not None:
            h.set(key, "Tag", tag)
    h.set(key, "Start", 0)
    h.delete_key(f"{key}\\StartOverride")
    return True


@patch("windows.storage.boot_drivers", "Make the in-box disk drivers load at boot", families=WIN)
def boot_drivers(ctx) -> PatchResult:
    major, minor = _nt(ctx)
    if major < 6:
        return PatchResult("", "", "not-needed", "Handled by the MergeIDE fix on this Windows version.")
    g = ctx.g
    enabled = []
    with os_mounted(g, ctx.root, readonly=False):
        ccs = control_set(g, ctx.root)
        with _system_hive(ctx) as h:
            names = ["pciide", "intelide", "atapi"]
            names += ["msahci"] if (major, minor) < (6, 2) else ["storahci"]
            if (major, minor) >= (6, 3):
                names.append("stornvme")
            for extra in ("viostor", "vioscsi"):
                if h.exists(f"{ccs}\\Services\\{extra}"):
                    names.append(extra)
            for name in names:
                if _make_boot_start(ctx, h, ccs, name, create=name in SERVICE_DEFAULTS
                                    and name != "atapi"):
                    enabled.append(name)
            changes = [c.describe() for c in h.changes]
    if not changes:
        return PatchResult("", "", "not-needed", f"{', '.join(enabled)} already load at boot.")
    return PatchResult("", "", "applied",
                       f"Windows can now boot from SATA/AHCI, IDE{' and NVMe' if 'stornvme' in enabled else ''} "
                       f"controllers ({', '.join(enabled)}).", changes)


@patch("windows.storage.mergeide", "Enable the in-box IDE drivers (MergeIDE)", families=WIN)
def mergeide(ctx) -> PatchResult:
    """The classic MergeIDE fix for Windows NT 4/2000/XP/2003."""
    g = ctx.g
    cdd_map = driver_data().get("mergeide", {})
    with os_mounted(g, ctx.root, readonly=False):
        ccs = control_set(g, ctx.root)
        sysroot = ctx.os.details.get("systemroot", "/WINDOWS")
        missing = [n for n in ("atapi.sys", "intelide.sys", "pciide.sys", "pciidex.sys")
                   if not ci_path(g, f"{sysroot}/system32/drivers/{n}")]
        with _system_hive(ctx) as h:
            cdd = f"{ccs}\\Control\\CriticalDeviceDatabase"
            for dev_id, (service, class_guid) in cdd_map.items():
                h.set(f"{cdd}\\{dev_id}", "Service", service, REG_SZ)
                h.set(f"{cdd}\\{dev_id}", "ClassGUID", class_guid, REG_SZ)
            for name in ("atapi", "intelide", "pciide"):
                key = f"{ccs}\\Services\\{name}"
                image, group, typ, tag = SERVICE_DEFAULTS[name]
                if not h.exists(key) or h.get(key, "ImagePath") is None:
                    h.set(key, "ImagePath", image, REG_EXPAND_SZ)
                    h.set(key, "Group", group, REG_SZ)
                    h.set(key, "Type", typ)
                    h.set(key, "Tag", tag)
                h.set(key, "ErrorControl", 1)
                h.set(key, "Start", 0)
            changes = [c.describe() for c in h.changes]
    msg = "The emulated Intel PIIX IDE controller is registered as a boot device."
    status = "applied" if changes else "not-needed"
    if missing:
        msg += (f" Note: {', '.join(missing)} are not in system32\\drivers; if Windows stops with "
                "0x7B, copy them from the installation CD (I386 folder) into the VM.")
        status = "applied"
    return PatchResult("", "", status, msg, changes)


@patch("windows.hal_check", "Match the virtual CPUs to the installed HAL", kind=CONFIG, families=WIN)
def hal_check(ctx) -> PatchResult:
    hal = ctx.os.hal
    m = ctx.cfg.machine
    if not hal:
        return PatchResult("", "", "not-needed", "The HAL type could not be determined; no change.")
    if hal in ("halacpi.dll", "halaacpi.dll", "hal.dll", "halapic.dll"):
        if m.cpus != 1:
            m.cpus = 1
            return PatchResult("", "", "applied",
                               f"This Windows uses a uniprocessor HAL ({hal}); the VM gets one CPU. "
                               "(Windows would ignore extra CPUs anyway.)", [f"CPUs → 1"])
        return PatchResult("", "", "not-needed", f"Uniprocessor HAL ({hal}) and one virtual CPU.")
    if hal == "hal.dll":
        return PatchResult("", "", "info", "Standard PC (non-ACPI) HAL: shut Windows down, then "
                           "use Turn Off — it cannot power the VM off by itself.")
    return PatchResult("", "", "not-needed", f"Multiprocessor HAL ({hal}).")


def _remove_from_filters(h: Hive, ccs: str, services: set[str]) -> list[str]:
    """Remove disabled services from device class UpperFilters/LowerFilters."""
    removed = []
    lower = {s.lower() for s in services}
    base = f"{ccs}\\Control\\Class"
    for cls in h.subkeys(base):
        for value in ("UpperFilters", "LowerFilters"):
            current = h.get(f"{base}\\{cls}", value)
            if not isinstance(current, list):
                continue
            kept = [f for f in current if f.lower() not in lower]
            if kept != current:
                h.set(f"{base}\\{cls}", value, kept, REG_MULTI_SZ)
                removed += [f"{f} ({cls} {value})" for f in current if f.lower() in lower]
    return removed


def _disable_groups(ctx, hypervisor: bool) -> PatchResult:
    g = ctx.g
    groups = [grp for grp in driver_data().get("hostile", [])
              if any(w in grp["name"] for w in ("VMware", "VirtualBox", "Parallels", "Xen")) == hypervisor]
    disabled = []
    with os_mounted(g, ctx.root, readonly=False):
        ccs = control_set(g, ctx.root)
        with _system_hive(ctx) as h:
            for grp in groups:
                hits = []
                for svc in grp["services"]:
                    key = f"{ccs}\\Services\\{svc}"
                    if h.exists(key) and h.get(key, "Start") != 4:
                        h.set(key, "Start", 4)
                        hits.append(svc)
                if hits:
                    disabled.append(f"{grp['name']}: {', '.join(hits)}")
            all_svcs = {s for grp in groups for s in grp["services"]}
            filters = _remove_from_filters(h, ccs, all_svcs)
            changes = [c.describe() for c in h.changes]
    if not disabled:
        return PatchResult("", "", "not-needed", "Nothing to disable.")
    msg = "Disabled: " + "; ".join(disabled) + ". They stay installed and can be removed from " \
          "inside Windows."
    if filters:
        msg += f" Also removed them from device filter lists ({len(filters)})."
    return PatchResult("", "", "applied", msg, changes)


@patch("windows.disable_services", "Disable software tied to the old hardware", families=WIN)
def disable_services(ctx) -> PatchResult:
    return _disable_groups(ctx, hypervisor=False)


@patch("windows.hostile_software", "Disable other hypervisors' guest tools", families=WIN)
def hostile_software(ctx) -> PatchResult:
    return _disable_groups(ctx, hypervisor=True)


@patch("uefi.fallback_loader", "Make the UEFI boot loader findable", families=("windows", "linux"))
def fallback_loader(ctx) -> PatchResult:
    """A new VM's firmware has no boot entries; it looks for \\EFI\\BOOT\\BOOTX64.EFI.

    Many installs (GRUB without --removable, for example) lack it, so copy
    the system's own loader there (on the VM's overlay only).
    """
    g = ctx.g
    esp = None
    for dev, fs in g.list_filesystems().items():
        if fs != "vfat":
            continue
        try:
            disk = g.part_to_dev(dev)
            num = g.part_to_partnum(dev)
            if g.part_get_parttype(disk) == "gpt":
                if g.part_get_gpt_type(disk, num).upper() == "C12A7328-F81F-11D2-BA4B-00A0C93EC93B":
                    esp = dev
                    break
            elif g.part_get_mbr_id(disk, num) == 0xEF:
                esp = dev
                break
        except RuntimeError:
            continue
    if not esp:
        return PatchResult("", "", "skipped", "No EFI system partition was found.")
    arch = ctx.os.arch or "x86_64"
    target_name = {"x86_64": "BOOTX64.EFI", "i386": "BOOTIA32.EFI", "aarch64": "BOOTAA64.EFI"}.get(arch, "BOOTX64.EFI")
    changes = []
    with mounted(g, esp, readonly=False):
        if ci_path(g, f"/EFI/BOOT/{target_name}"):
            return PatchResult("", "", "not-needed", "The fallback boot loader already exists.")
        candidates = [
            ["/EFI/Microsoft/Boot/bootmgfw.efi"],
        ]
        for vendor in g.ls("/EFI") if g.is_dir("/EFI") else []:
            base = f"/EFI/{vendor}"
            if vendor.upper() in ("BOOT", "MICROSOFT"):
                continue
            for loader in ("shimx64.efi", "grubx64.efi", "systemd-bootx64.efi", "refind_x64.efi"):
                if ci_path(g, f"{base}/{loader}"):
                    candidates.append([f"{base}/{loader}"] +
                                      [f"{base}/{x}" for x in ("grubx64.efi", "mmx64.efi", "fbx64.efi",
                                                               "BOOTX64.CSV", "grub.cfg")
                                       if x != loader and ci_path(g, f"{base}/{x}")])
                    break
        for files in candidates:
            src = ci_path(g, files[0])
            if not src:
                continue
            if not g.is_dir("/EFI/BOOT") and not ci_path(g, "/EFI/BOOT"):
                g.mkdir_p("/EFI/BOOT")
            boot_dir = ci_path(g, "/EFI/BOOT") or "/EFI/BOOT"
            g.cp(src, f"{boot_dir}/{target_name}")
            changes.append(f"copied {files[0]} to \\EFI\\BOOT\\{target_name}")
            for extra in files[1:]:
                real = ci_path(g, extra)
                if real:
                    g.cp(real, f"{boot_dir}/{extra.rsplit('/', 1)[1]}")
                    changes.append(f"copied {extra} next to it")
            break
    if not changes:
        return PatchResult("", "", "skipped", "No boot loader to copy was found on the EFI partition.")
    return PatchResult("", "", "applied", "The firmware will find the boot loader on its own.", changes)
