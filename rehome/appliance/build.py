"""Building an appliance image from a Rehome VM.

The image (docs/DECISIONS.md D26):

    1  BIOS boot partition      1 MiB    GRUB for BIOS computers
    2  EFI system partition   256 MiB    GRUB for UEFI computers (EFI/BOOT/BOOTX64.EFI)
    3  system (ext4)            6 GiB    a copy of the cached base system, configured
    4  data (ext4)            the VM     /var/lib/rehome: the VM, grown on first boot

Nothing here needs root: the image is assembled inside libguestfs's helper
VM. The VM's disks are flattened into standalone qcow2 files first, so the
appliance never depends on the computer it was built on.
"""

from __future__ import annotations

import contextlib
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from rehome import __version__, paths
from rehome.appliance import base as base_mod
from rehome.appliance import image, ubuntu
from rehome.errors import RehomeError
from rehome.inspect import gfs
from rehome.util import toml
from rehome.util.units import human
from rehome.vm import disks, runner
from rehome.vm.config import VMConfig

MIB = 2**20
GIB = 2**30
GPT_BIOS = "21686148-6449-6E6F-744E-656564454649"
GPT_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GPT_ROOT_X86_64 = "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709"
GPT_LINUX_DATA = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"

HOME = "/var/lib/rehome"                       # REHOME_HOME inside the appliance
VMS = f"{HOME}/data/vms"
CONFIG = "/etc/rehome/appliance.toml"
CODE = "/opt/rehome"

KERNEL_QUIET = ("quiet loglevel=3 rd.udev.log_level=3 systemd.show_status=false "
                "vt.global_cursor_default=0")


@dataclass
class Options:
    output: Path
    include_media: bool = True          # copy the VM's install media (ISOs, floppies) along
    maintenance_password: str = ""      # "" = the maintenance console opens without one
    network: str = "nat"                # nat / none
    usb_auto: bool = True               # give plugged-in USB devices to the VM
    kernel_args: str = ""               # appended to every boot entry (e.g. console=ttyS0)
    refresh_base: bool = False          # rebuild the base system with current updates
    fmt: str = "raw"                    # raw (to write to a drive) / qcow2
    cpu_model: str = ""                 # "" = keep the VM's; e.g. "Westmere" for a fixed CPU


@dataclass
class Plan:
    """What the build will do, for review before starting (CLI and GUI)."""
    vm: VMConfig
    disks: list[tuple[str, Path, int]] = field(default_factory=list)   # (id, source, est. bytes)
    media: list[tuple[str, Path]] = field(default_factory=list)       # (device id, file)
    warnings: list[str] = field(default_factory=list)
    data_bytes: int = 0

    @property
    def total_estimate(self) -> int:
        return MIB * 300 + 6 * GIB + self.data_bytes


def plan(cfg: VMConfig, opts: Options) -> Plan:
    if runner.state(cfg).active:
        raise RehomeError(f"\"{cfg.name}\" must be turned off to build an appliance from it.",
                          code="vm-running")
    if runner.saved_state_path(cfg).exists():
        raise RehomeError(f"\"{cfg.name}\" has a saved state (it was suspended).",
                          fix="Start it and shut it down from inside, or discard the saved "
                              "state, then build the appliance.", code="saved-state")
    if cfg.machine.arch not in ("x86_64", "i386", "i686"):
        raise RehomeError("Appliances can only run PC (x86) VMs.", code="unsupported")
    p = Plan(vm=cfg)
    for d in cfg.disks:
        src = cfg.resolve(d.file)
        try:
            est = disks.actual_size(src) if not (d.source and d.source.mode == "linked") \
                else (d.source.size or disks.info(src)["virtual-size"])
        except (RehomeError, OSError, KeyError):
            est = 0
        p.disks.append((d.id, src, est))
    if opts.include_media:
        for dev in list(cfg.cdroms) + list(cfg.floppies):
            if dev.file:
                f = Path(dev.file)
                if f.is_file():
                    p.media.append((dev.id, f))
                else:
                    p.warnings.append(f"{dev.id}: {dev.file} is missing and is left out.")
    if cfg.shares:
        p.warnings.append("Shared folders point to this computer and are left out.")
    if cfg.passthrough.pci:
        p.warnings.append("PCI pass-through is not part of appliances yet; it is left out.")
    if cfg.display.gl:
        p.warnings.append("3D acceleration (virgl) is turned off in the appliance.")
    if any(n.mode == "bridge" for n in cfg.nics) and opts.network != "none":
        p.warnings.append("Bridged networking becomes NAT in the appliance.")
    if (opts.cpu_model or cfg.machine.cpu_model) == "host":
        p.warnings.append("The VM uses the processor model of the computer it runs on, so the "
                          "processor the guest sees changes from one computer to the next. A "
                          "fixed model (--cpu-model) keeps it the same everywhere.")
    if cfg.machine.firmware != "bios" and (cfg.dir / "nvram.qcow2").exists():
        p.warnings.append("UEFI settings (boot entries) start fresh in the appliance; "
                          "Windows and most Linux systems still boot from the default path.")
    p.data_bytes = sum(e for _i, _s, e in p.disks) + sum(f.stat().st_size for _i, f in p.media)
    return p


def _ascii(text: str) -> str:
    """GRUB's text menu shows only ASCII."""
    out = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c for c in out if c.isprintable()) or "the VM"


def _grub_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def grub_cfg(root_uuid: str, vm_name: str, extra: str = "") -> str:
    name = _ascii(vm_name)
    linux = f"linux /boot/vmlinuz root=UUID={root_uuid} ro"
    extra = (" " + extra.strip()) if extra.strip() else ""

    def entry(title: str, entry_id: str, args: str) -> str:
        return (f"menuentry {_grub_quote(title)} --id {entry_id} {{\n"
                f"    recordfail\n"
                f"    {linux} {args}{extra}\n"
                f"    initrd /boot/initrd.img\n"
                f"}}\n")
    return (
        "# Rehome appliance boot menu, written by Rehome (update-grub does not change it).\n"
        "# Hidden unless Esc is pressed, or the last boot did not reach the VM.\n"
        "set default=vm\n"
        "set timeout_style=hidden\n"
        "set timeout=2\n"
        "if [ -s $prefix/grubenv ]; then\n"
        "    set have_grubenv=true\n"
        "    load_env\n"
        "fi\n"
        "if [ \"${next_entry}\" ]; then\n"
        "    set default=\"${next_entry}\"\n"
        "    set next_entry=\n"
        "    save_env next_entry\n"
        "fi\n"
        "if [ \"${recordfail}\" = 1 ]; then\n"
        "    set timeout_style=menu\n"
        "    set timeout=30\n"
        "fi\n"
        "function recordfail {\n"
        "    set recordfail=1\n"
        "    if [ -n \"${have_grubenv}\" ]; then save_env recordfail; fi\n"
        "}\n"
        "terminal_output console\n"
        f"search --no-floppy --fs-uuid --set=root {root_uuid}\n"
        + entry(f"Start {name}", "vm", f"{KERNEL_QUIET} rehome.mode=vm")
        + entry(f"Start {name} with basic graphics", "vm-basic",
                f"{KERNEL_QUIET} nomodeset rehome.mode=vm rehome.graphics=basic")
        + entry("Maintenance console", "maintenance", "loglevel=4 rehome.mode=maintenance")
        + entry("Maintenance console with basic graphics", "maintenance-basic",
                "loglevel=4 nomodeset rehome.mode=maintenance rehome.graphics=basic")
        + "if [ \"${grub_platform}\" = efi ]; then\n"
          "    menuentry 'Firmware settings (UEFI setup)' --id fwsetup { fwsetup }\n"
          "fi\n"
          "menuentry 'Restart' --id reboot { reboot }\n"
          "menuentry 'Power off' --id poweroff { halt }\n")


def fstab(root_uuid: str, esp_uuid: str, data_uuid: str) -> str:
    return ("# Rehome appliance\n"
            f"UUID={root_uuid}  /                ext4  defaults,errors=remount-ro  0 1\n"
            f"UUID={esp_uuid}  /boot/efi        vfat  umask=0077,nofail  0 2\n"
            f"UUID={data_uuid}  {HOME}  ext4  defaults,nofail,x-systemd.device-timeout=15s  0 2\n")


def appliance_config(cfg: VMConfig, opts: Options, base: base_mod.Base) -> dict:
    return {
        "appliance": {"vm": cfg.id, "name": cfg.name, "built": time.strftime("%Y-%m-%d %H:%M"),
                      "rehome_version": __version__, "system": base.meta.get("release", ""),
                      "password": bool(opts.maintenance_password)},
        "kiosk": {"usb_auto": opts.usb_auto, "countdown": 3},
        "network": {"mode": opts.network},
    }


def vm_for_appliance(cfg: VMConfig, opts: Options, media: dict[str, str]) -> VMConfig:
    """The VM's configuration as it lives in the appliance."""
    out = VMConfig.from_dict(cfg.to_dict())
    for d in out.disks:
        d.file, d.format = f"disks/{d.id}.qcow2", "qcow2"
        if d.source:
            d.source.mode = "copy"
    for dev in list(out.cdroms) + list(out.floppies):
        dev.file = media.get(dev.id, "")
    out.display.console = "kiosk"
    if out.machine.accel == "kvm":
        out.machine.accel = "auto"            # still start (slowly) where virtualization is off
    if opts.cpu_model:
        out.machine.cpu_model = opts.cpu_model
    out.display.gl = False
    out.sound.backend = "auto"
    out.shares = []
    out.passthrough.pci = []
    out.snapshots, out.current_snapshot = [], ""
    out.direct_boot = None
    if opts.network == "none":
        out.nics = []
    else:
        for n in out.nics:
            if n.mode == "bridge":
                n.mode, n.bridge = "nat", ""
    return out


# --- the build ---------------------------------------------------------------------------

def _stage(progress, name: str, detail: str = "") -> None:
    if progress:
        progress.stage(name, detail)


def _export_disks(cfg: VMConfig, work: Path, progress) -> list[tuple[str, Path]]:
    from rehome.disk.overlay import SourceAccess
    linked = [d.source for d in cfg.disks if d.source and d.source.mode == "linked"]
    out = []
    ctx = SourceAccess(linked) if linked else contextlib.nullcontext()
    with ctx:
        n = len(cfg.disks) or 1
        for i, d in enumerate(cfg.disks):
            dest = work / f"{d.id}.qcow2"

            def prog(frac, i=i):
                if progress:
                    progress.update((i + frac) / n)
            disks.convert(str(cfg.resolve(d.file)), str(dest), src_fmt=d.format, progress=prog,
                          cancel=progress.cancel_cb() if progress else None)
            out.append((d.id, dest))
    return out


def _code_members() -> list[tuple[Path, str]]:
    root = paths.source_root()
    pkg = root / "rehome"
    skip_dirs = {"__pycache__", "ui"}          # the GUI is not used inside the appliance
    members = image.tree_members(pkg, "rehome",
                                 skip=lambda rel: bool(skip_dirs & set(rel.parts))
                                 or rel.suffix in (".pyc", ".pyo"))
    data = paths.data_files_dir()
    members += image.tree_members(data, "data",
                                  skip=lambda rel: rel.parts[:2] == ("appliance", "rootfs")
                                  or rel.suffix in (".png", ".svg"))
    return members


def build(cfg: VMConfig, opts: Options, progress=None) -> Path:
    """Build the appliance image for ``cfg``; returns the image path."""
    pl = plan(cfg, opts)
    out = Path(opts.output)
    if out.exists():
        raise RehomeError(f"{out} already exists.", fix="Choose another file name.",
                          code="exists")
    base = base_mod.ensure(progress, refresh=opts.refresh_base)
    work = paths.ensure(ubuntu.cache_dir() / "work") / f"{cfg.short_id}-{int(time.time())}"
    work.mkdir(parents=True)
    tmp_out = out.with_name(out.name + ".building")
    try:
        _stage(progress, "Copying the VM's disks", cfg.name)
        exported = _export_disks(cfg, work, progress)
        disk_bytes = sum(disks.actual_size(p) for _i, p in exported)
        media_bytes = sum(f.stat().st_size for _i, f in pl.media)
        payload = disk_bytes + media_bytes + 64 * MIB
        data_size = max(GIB, int(payload * 1.08) + 512 * MIB)    # ext4 overhead + headroom
        data_size = (data_size + MIB - 1) // MIB * MIB
        esp = ubuntu.load_release().esp_mib * MIB
        root = base.size
        first = MIB
        total = first + MIB + esp + root + data_size + MIB
        disks.create(tmp_out, total, fmt=opts.fmt)
        _stage(progress, "Writing the appliance image")
        drives = [gfs.Drive(str(tmp_out), opts.fmt, False, discard=True),
                  gfs.Drive(str(base.path), "qcow2", True)]
        with gfs.session(drives, memsize=1024) as g:
            _partition(g, esp, root)
            if progress:
                progress.update(0.05, "Copying the system")
            g.copy_device_to_device("/dev/sdb", "/dev/sda3", sparse=True)
            g.e2fsck("/dev/sda3", forceall=True)
            g.set_uuid_random("/dev/sda3")
            g.mkfs("vfat", "/dev/sda2", label="REHOME-EFI")
            g.mkfs("ext4", "/dev/sda4", label="rehome-data")
            g.mount("/dev/sda3", "/")
            g.mkdir_p("/boot/efi")
            g.mount("/dev/sda2", "/boot/efi")
            g.mkdir_p(HOME)
            g.mount("/dev/sda4", HOME)
            uuids = {p: g.vfs_uuid(f"/dev/sda{p}") for p in (2, 3, 4)}

            if progress:
                progress.update(0.1, "Installing Rehome")
            image.tar_in(g, _code_members(), CODE)
            rootfs = paths.data_files_dir() / "appliance" / "rootfs"
            image.tar_in(g, image.tree_members(rootfs, ""), "/")
            # Packages install data files without execute bits.
            g.chmod(0o755, "/usr/local/bin/rehome")
            g.mkdir_p("/etc/rehome")
            g.write(CONFIG, toml.dumps(appliance_config(cfg, opts, base)).encode())
            g.write("/etc/fstab", fstab(uuids[3], uuids[2], uuids[4]).encode())
            g.write("/etc/hostname", b"rehome\n")
            g.write("/etc/hosts", b"127.0.0.1 localhost\n127.0.1.1 rehome\n"
                                  b"::1 localhost ip6-localhost ip6-loopback\n")

            if progress:
                progress.update(0.15, f"Copying the VM ({human(payload)})")
            vm_dir = f"{VMS}/{_vm_dirname(cfg)}"
            media = {dev_id: f"media/{f.name}" for dev_id, f in pl.media}
            appl_cfg = vm_for_appliance(cfg, opts, media)
            members = [(p, f"disks/{d_id}.qcow2") for d_id, p in exported]
            members += [(f, f"media/{f.name}") for _i, f in pl.media]
            for extra in ("tpm",):
                if (cfg.dir / extra).is_dir():
                    members += image.tree_members(cfg.dir / extra, extra)
            image.tar_in(g, members, vm_dir)
            g.mkdir_p(f"{vm_dir}/logs")
            g.write(f"{vm_dir}/vm.toml", toml.dumps(appl_cfg.to_dict()).encode())

            if progress:
                progress.update(0.85, "Configuring the system and boot loader")
            _configure(g, opts)
            g.write("/boot/grub/grub.cfg",
                    grub_cfg(uuids[3], cfg.name, opts.kernel_args).encode())
            g.fstrim(HOME)
            g.fstrim("/boot/efi")
            g.fstrim("/")
        tmp_out.rename(out)
    except BaseException:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def _vm_dirname(cfg: VMConfig) -> str:
    from rehome.vm import library
    return f"{library.slug(cfg.name)}-{cfg.short_id}"


def _partition(g, esp: int, root: int) -> None:
    s = 512
    g.part_init("/dev/sda", "gpt")
    start = MIB // s
    g.part_add("/dev/sda", "p", start, start + MIB // s - 1)
    start += MIB // s
    g.part_add("/dev/sda", "p", start, start + esp // s - 1)
    start += esp // s
    g.part_add("/dev/sda", "p", start, start + root // s - 1)
    start += root // s
    g.part_add("/dev/sda", "p", start, -(MIB // s))
    for n, (typ, name) in enumerate(((GPT_BIOS, "BIOS boot"), (GPT_ESP, "EFI system"),
                                     (GPT_ROOT_X86_64, "Rehome system"),
                                     (GPT_LINUX_DATA, "Rehome data")), start=1):
        g.part_set_gpt_type("/dev/sda", n, typ)
        g.part_set_name("/dev/sda", n, name)


CONFIGURE = r"""
# The user the VM runs as (no password: nobody logs in as it).
useradd --create-home --uid 1000 --user-group --shell /usr/sbin/nologin rehome
for grp in kvm audio video render input plugdev; do
    if getent group "$grp" >/dev/null; then usermod -aG "$grp" rehome; fi
done
chown -R rehome:rehome /var/lib/rehome
chmod 0750 /var/lib/rehome

# Services: the appliance's own, networking; no text logins.
systemctl enable rehome-boot.service rehome-firstboot.service
systemctl enable systemd-networkd.service systemd-resolved.service iwd.service
systemctl mask systemd-networkd-wait-online.service
systemctl disable getty@tty1.service 2>/dev/null || true
rm -f /etc/systemd/system/getty.target.wants/getty@tty1.service
systemctl set-default multi-user.target
ln -sf ../run/systemd/resolve/stub-resolv.conf /etc/resolv.conf

# A new identity on first boot.
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id

# Boot loader for both kinds of firmware (the UEFI one at the removable-media
# path, so no firmware boot entry is needed).
grub-install --target=i386-pc --boot-directory=/boot /dev/sda
grub-install --target=x86_64-efi --efi-directory=/boot/efi --boot-directory=/boot \
    --removable --no-nvram
grub-editenv /boot/grub/grubenv create
"""


def _configure(g, opts: Options) -> None:
    image.run(g, CONFIGURE, "configuring the system")
    if opts.maintenance_password:
        g.write("/tmp/rehome-pw", f"root:{opts.maintenance_password}\n".encode())
        try:
            image.run(g, "chpasswd < /tmp/rehome-pw", "setting the maintenance password")
        finally:
            g.rm_rf("/tmp/rehome-pw")
        g.mkdir_p("/etc/systemd/system/rehome-maintenance.service.d")
        g.write("/etc/systemd/system/rehome-maintenance.service.d/password.conf", (
            b"# The maintenance console asks for the root password (set when building).\n"
            b"[Service]\nExecStart=\n"
            b"ExecStart=-/sbin/agetty -o '-p -- \\\\u' --noclear - linux\n"))
        g.write("/root/.profile", (
            b"# Rehome appliance: the maintenance console after logging in on tty1.\n"
            b"if [ \"$(tty)\" = /dev/tty1 ]; then exec /usr/local/bin/rehome appliance console; fi\n"
        ))
    else:
        image.run(g, "passwd -l root >/dev/null", "locking the root account")
