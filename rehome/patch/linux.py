"""Offline fixes for Linux installations.

* fstab / bootloader: device names (/dev/sda2, /dev/nvme0n1p2, by-id links)
  change in a VM, so they are replaced with filesystem UUIDs.
* initramfs: host-only initramfs images (dracut, mkinitcpio, initramfs-tools)
  often lack virtio drivers; Rehome adds them and regenerates the image with
  the guest's own tool. If that fails, the VM emulates the original
  controller instead.
* proprietary GPU drivers: X configurations that force nvidia/fglrx are set
  aside so the desktop falls back to the generic driver.
"""

from __future__ import annotations

import re

from rehome.inspect import initramfs
from rehome.inspect.gfs import download_bytes, os_mounted, read_text
from rehome.inspect.linux import DEVICE_NAME_RE, initramfs_path, kernels
from rehome.patch.registry import CONFIG, PatchResult, patch

LINUX = ("linux",)
VIRTIO_MODULES = ["virtio_blk", "virtio_scsi", "virtio_pci", "virtio_net", "virtio_console",
                  "ahci", "nvme", "sd_mod"]
BACKUP_SUFFIX = ".rehome-orig"


def _backup(g, path: str) -> None:
    if g.is_file(path) and not g.exists(path + BACKUP_SUFFIX):
        g.cp_a(path, path + BACKUP_SUFFIX)


def _device_map(ctx) -> dict[str, str]:
    """Guest device name -> UUID=... for every device the guest's fstab names."""
    g = ctx.g
    mapping = {}
    mps = g.inspect_get_mountpoints(ctx.root)
    fstab = read_text(g, "/etc/fstab") or ""
    root_disk_guest = None
    for line in fstab.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("#"):
            continue
        spec, mp = parts[0], parts[1]
        if not DEVICE_NAME_RE.match(spec):
            continue
        dev = mps.get(mp)
        if dev:
            try:
                uuid = g.vfs_uuid(dev)
            except RuntimeError:
                uuid = ""
            if uuid:
                mapping[spec] = f"UUID={uuid}"
            if mp == "/":
                root_disk_guest = re.sub(r"p?\d+$", "", spec)
    # Swap and other unmounted partitions: same partition number on the root disk.
    root_dev = mps.get("/", "")
    try:
        root_disk_appl = g.part_to_dev(root_dev)
    except RuntimeError:
        root_disk_appl = None
    for line in fstab.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("#") or parts[0] in mapping:
            continue
        spec = parts[0]
        if not DEVICE_NAME_RE.match(spec) or not root_disk_guest or not root_disk_appl:
            continue
        m = re.search(r"p?(\d+)$", spec)
        if m and spec.startswith(root_disk_guest):
            for p in g.list_partitions():
                try:
                    if g.part_to_dev(p) == root_disk_appl and g.part_to_partnum(p) == int(m.group(1)):
                        uuid = g.vfs_uuid(p)
                        if uuid:
                            mapping[spec] = f"UUID={uuid}"
                except RuntimeError:
                    continue
    return mapping


@patch("linux.fstab", "Mount filesystems by UUID", families=LINUX)
def fstab(ctx) -> PatchResult:
    g = ctx.g
    with os_mounted(g, ctx.root, readonly=False):
        text = read_text(g, "/etc/fstab")
        if text is None:
            return PatchResult("", "", "not-needed", "No /etc/fstab.")
        mapping = _device_map(ctx)
        ctx.state["device_map"] = mapping
        if not mapping:
            return PatchResult("", "", "not-needed", "/etc/fstab already uses stable names.")
        out, changes = [], []
        for line in text.splitlines(keepends=True):
            parts = line.split()
            if parts and parts[0] in mapping:
                new = line.replace(parts[0], mapping[parts[0]], 1)
                changes.append(f"{parts[0]} → {mapping[parts[0]]} ({parts[1] if len(parts) > 1 else ''})")
                out.append(new)
            else:
                out.append(line)
        _backup(g, "/etc/fstab")
        g.write("/etc/fstab", "".join(out).encode())
    return PatchResult("", "", "applied", f"{len(changes)} fstab entr{'y' if len(changes) == 1 else 'ies'} now use UUIDs.",
                       changes)


BOOT_CONFIGS = ["/etc/default/grub", "/boot/grub/grub.cfg", "/boot/grub2/grub.cfg",
                "/boot/efi/EFI/*/grub.cfg", "/etc/kernel/cmdline", "/boot/refind_linux.conf",
                "/boot/loader/entries/*.conf", "/boot/efi/loader/entries/*.conf",
                "/efi/loader/entries/*.conf", "/boot/grub/menu.lst", "/etc/lilo.conf"]


@patch("linux.bootloader", "Point the boot loader at UUIDs", families=LINUX)
def bootloader(ctx) -> PatchResult:
    g = ctx.g
    changes = []
    with os_mounted(g, ctx.root, readonly=False):
        mapping = ctx.state.get("device_map") or _device_map(ctx)
        if not mapping:
            return PatchResult("", "", "not-needed", "The boot configuration does not use device names.")
        pattern = re.compile(r"\b(root|resume)=(" + "|".join(re.escape(k) for k in
                                                             sorted(mapping, key=len, reverse=True)) + r")(?=[\s\"']|$)")
        files = []
        for pat in BOOT_CONFIGS:
            try:
                files += g.glob_expand(pat)
            except RuntimeError:
                pass
        for f in files:
            text = read_text(g, f)
            if text is None:
                continue
            new = pattern.sub(lambda m: f"{m.group(1)}={mapping[m.group(2)]}", text)
            if new != text:
                _backup(g, f)
                g.write(f, new.encode())
                changes.append(f"updated root=/resume= in {f}")
    if "/etc/lilo.conf" in [c.split()[-1] for c in changes]:
        return PatchResult("", "", "applied", "LILO configuration updated, but LILO must be re-run "
                           "from inside the VM (boot it from a rescue ISO).", changes)
    if not changes:
        return PatchResult("", "", "not-needed", "The boot configuration does not use device names.")
    return PatchResult("", "", "applied", "The kernel finds its root filesystem by UUID.", changes)


def _has_virtio(g, kernel: str, tool: str) -> bool | None:
    path = initramfs_path(g, kernel, tool)
    if not path:
        return None
    data = download_bytes(g, path)
    files = initramfs.list_files(data) if data else None
    if files is None:
        return None
    mods = initramfs.storage_modules(files)
    return "virtio_blk" in mods or "virtio_scsi" in mods


def _command(g, argv: list[str], timeout_note: str = "") -> str:
    try:
        return g.command(argv)
    except RuntimeError as e:
        raise RuntimeError(f"{' '.join(argv)} failed: {e}") from e


@patch("linux.initramfs", "Add virtio drivers to the initramfs", families=LINUX)
def add_virtio(ctx) -> PatchResult:
    g = ctx.g
    o = ctx.os
    if o.virtio_ready:
        ctx.state["virtio_ready"] = True
        return PatchResult("", "", "not-needed", "The initramfs already contains virtio drivers.")
    changes = []
    with os_mounted(g, ctx.root, readonly=False):
        ks = kernels(g)
        if not ks:
            return PatchResult("", "", "skipped", "No installed kernel was found.")
        tool = o.initramfs_tool
        mods = " ".join(VIRTIO_MODULES)
        try:
            if tool == "dracut":
                conf = "/etc/dracut.conf.d/90-rehome-virtio.conf"
                g.mkdir_p("/etc/dracut.conf.d")
                g.write(conf, f'# Added by Rehome: drivers for virtual hardware\nadd_drivers+=" {mods} "\n'.encode())
                changes.append(f"wrote {conf}")
                for k in ks:
                    _command(g, ["dracut", "-f", "--kver", k])
                    changes.append(f"rebuilt the initramfs for {k} with dracut")
            elif tool == "mkinitcpio":
                text = read_text(g, "/etc/mkinitcpio.conf") or ""
                m = re.search(r"^MODULES=\((.*?)\)", text, re.M)
                existing = m.group(1).split() if m else []
                wanted = [x for x in VIRTIO_MODULES if x not in existing and x != "virtio_console"]
                if wanted:
                    new_line = "MODULES=(" + " ".join(existing + wanted) + ")"
                    text = text.replace(m.group(0), new_line) if m else text + f"\n{new_line}\n"
                    _backup(g, "/etc/mkinitcpio.conf")
                    g.write("/etc/mkinitcpio.conf", text.encode())
                    changes.append(f"added {' '.join(wanted)} to MODULES in /etc/mkinitcpio.conf")
                _command(g, ["mkinitcpio", "-P"])
                changes.append("rebuilt the initramfs images with mkinitcpio -P")
            elif tool == "initramfs-tools":
                text = read_text(g, "/etc/initramfs-tools/modules") or ""
                have = {ln.strip() for ln in text.splitlines()}
                add = [x for x in VIRTIO_MODULES if x not in have]
                if add:
                    _backup(g, "/etc/initramfs-tools/modules")
                    g.write("/etc/initramfs-tools/modules",
                            (text.rstrip("\n") + "\n# Added by Rehome\n" + "\n".join(add) + "\n").encode())
                    changes.append(f"added {' '.join(add)} to /etc/initramfs-tools/modules")
                _command(g, ["update-initramfs", "-u", "-k", "all"])
                changes.append("rebuilt the initramfs with update-initramfs")
            else:
                return PatchResult("", "", "skipped",
                                   f"Unknown initramfs generator ({tool or 'none found'}); the VM "
                                   "will emulate the original disk controller instead.", changes)
        except RuntimeError as e:
            return PatchResult("", "", "failed",
                               f"Rebuilding the initramfs failed ({e}); the VM will emulate the "
                               "original disk controller instead.", changes)
        ok = _has_virtio(g, ks[-1], tool)
    if ok:
        ctx.state["virtio_ready"] = True
        return PatchResult("", "", "applied", "The initramfs now contains virtio drivers.", changes)
    if ok is None:
        ctx.state["virtio_ready"] = True
        return PatchResult("", "", "applied", "The initramfs was rebuilt with virtio drivers "
                           "(could not be double-checked).", changes)
    return PatchResult("", "", "failed", "The rebuilt initramfs still lacks virtio drivers; the "
                       "VM will emulate the original disk controller instead.", changes)


@patch("linux.switch_virtio", "Use virtio disks", kind=CONFIG, families=LINUX)
def switch_virtio(ctx) -> PatchResult:
    if not (ctx.state.get("virtio_ready") or ctx.os.virtio_ready):
        return PatchResult("", "", "not-needed", "Keeping the emulated original controller.")
    changed = [d.id for d in ctx.cfg.disks if d.bus != "virtio"]
    for d in ctx.cfg.disks:
        d.bus = "virtio"
    if not changed:
        return PatchResult("", "", "not-needed", "Disks already use virtio.")
    return PatchResult("", "", "applied", "Disks use the fast virtio controller.",
                       [f"{d} → virtio" for d in changed])


@patch("linux.gpu_driver", "Fall back from proprietary GPU drivers", families=LINUX)
def gpu_driver(ctx) -> PatchResult:
    if not ctx.os.gpu_drivers:
        return PatchResult("", "", "not-needed", "No proprietary GPU driver configuration.")
    g = ctx.g
    changes = []
    with os_mounted(g, ctx.root, readonly=False):
        cands = ["/etc/X11/xorg.conf"]
        try:
            cands += [f"/etc/X11/xorg.conf.d/{f}" for f in g.ls("/etc/X11/xorg.conf.d")]
        except RuntimeError:
            pass
        for f in cands:
            text = read_text(g, f) or ""
            if re.search(r'Driver\s+"(nvidia|fglrx|amdgpu-pro)"', text):
                g.mv(f, f + ".rehome-disabled")
                changes.append(f"set aside {f} (it forces a proprietary driver)")
        conf = "/etc/modprobe.d/rehome-vm.conf"
        if "nvidia" in ctx.os.gpu_drivers and not g.exists(conf):
            g.write(conf, b"# Added by Rehome: no NVIDIA GPU inside the VM\n"
                          b"blacklist nvidia\nblacklist nvidia_drm\nblacklist nvidia_modeset\n"
                          b"blacklist nvidia_uvm\n")
            changes.append(f"wrote {conf} (keeps the NVIDIA module from loading)")
    return PatchResult("", "", "applied" if changes else "not-needed",
                       "The desktop will use the VM's virtual GPU." if changes else
                       "The proprietary driver is installed but not forced; nothing to change.",
                       changes)


@patch("linux.network", "Add a fallback network configuration", families=LINUX)
def network(ctx) -> PatchResult:
    pinned = ctx.os.details.get("pinned_interfaces")
    if not pinned:
        return PatchResult("", "", "not-needed", "Network configuration is not tied to interface names.")
    g = ctx.g
    changes = []
    with os_mounted(g, ctx.root, readonly=False):
        if g.is_dir("/etc/netplan"):
            path = "/etc/netplan/99-rehome-vm.yaml"
            g.write(path, b"# Added by Rehome: DHCP on the VM's network card\nnetwork:\n  version: 2\n"
                          b"  ethernets:\n    rehome-vm:\n      match:\n        driver: \"virtio_net e1000e e1000 8139cp\"\n"
                          b"      dhcp4: true\n      dhcp6: true\n")
            g.chmod(0o600, path)
            changes.append(f"wrote {path}")
        if g.is_dir("/etc/systemd/network"):
            path = "/etc/systemd/network/99-rehome-vm.network"
            g.write(path, b"# Added by Rehome: DHCP on the VM's network card\n[Match]\n"
                          b"Driver=virtio_net e1000e e1000 8139cp\n\n[Network]\nDHCP=yes\n")
            changes.append(f"wrote {path}")
    return PatchResult("", "", "applied" if changes else "not-needed",
                       f"The old configuration names {', '.join(pinned)}; the VM's card gets DHCP.",
                       changes)


@patch("linux.hostile_software", "Disable other hypervisors' guest agents", families=LINUX)
def hostile_software(ctx) -> PatchResult:
    g = ctx.g
    units = ["vboxadd.service", "vboxadd-service.service", "vboxservice.service",
             "vmtoolsd.service", "vmware-tools.service", "hv-kvp-daemon.service", "hv_kvp_daemon.service"]
    changes = []
    with os_mounted(g, ctx.root, readonly=False):
        for u in units:
            exists = any(g.exists(f"{d}/{u}") for d in ("/usr/lib/systemd/system", "/lib/systemd/system",
                                                        "/etc/systemd/system"))
            target = f"/etc/systemd/system/{u}"
            if exists and not (g.is_symlink(target) if g.exists(target) else False):
                if g.exists(target):
                    g.mv(target, target + BACKUP_SUFFIX)
                g.ln_sf("/dev/null", target)
                changes.append(f"masked {u}")
    return PatchResult("", "", "applied" if changes else "not-needed",
                       "Other hypervisors' agents will not start." if changes else
                       "No other hypervisor's guest agent is installed.", changes)


@patch("linux.rtc", "Keep the guest's clock convention", kind=CONFIG, families=LINUX)
def rtc(ctx) -> PatchResult:
    want = ctx.os.rtc or "utc"
    if ctx.cfg.machine.rtc != want:
        ctx.cfg.machine.rtc = want
        return PatchResult("", "", "applied", f"The VM's clock runs in {want.upper()} like the original.",
                           [f"rtc → {want}"])
    return PatchResult("", "", "not-needed", f"The guest expects the hardware clock in {want.upper()}.")
