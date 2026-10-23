"""After the first successful boot: guest tools, then paravirtual devices.

1. ``mark_first_boot_ok`` — the user confirmed the imported system works.
2. ``attach_tools`` — Windows: insert the virtio-win ISO (drivers, guest
   agent, SPICE agent); Linux: tell the user which packages to install.
3. ``switch_to_paravirt`` — once the drivers are in, move disks, network and
   display to virtio (a checkpoint is taken first so it can be undone).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rehome import paths
from rehome.errors import RehomeError
from rehome.patch import profiles
from rehome.patch.v2v import virtio_win_path
from rehome.vm import edit, library, runner, snapshots
from rehome.vm.config import VMConfig

VIRTIO_WIN_URL = ("https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
                  "stable-virtio/virtio-win.iso")
# Current virtio-win no longer supports Windows XP and Server 2003. The SPICE guest
# tools 0.65 do (QXL display driver, SPICE agent, virtio drivers of their time); this
# is the copy libosinfo's database lists for Windows XP (also used by GNOME Boxes).
XP_TOOLS_URL = ("https://zeenix.fedorapeople.org/drivers/win-tools/postinst/"
                "spice-guest-tools-0.65.exe")
XP_TOOLS_ISO = "rehome-windows-xp-tools.iso"
XP_README = """\
Rehome guest tools for Windows XP and Server 2003
=================================================

1. Run spice-guest-tools-0.65.exe and follow the installer. Windows XP needs
   Service Pack 3. Windows asks whether to continue with drivers that have not
   passed "Windows Logo testing": choose Continue Anyway.
2. Shut Windows down.
3. In Rehome, switch the VM to the faster devices (Summary tab, or
   `rehome tools paravirt VM`). The display becomes QXL.
4. Start the VM again. In Display Properties > Settings, choose a resolution
   up to 2560 x 1600 (for example your screen's 1920 x 1080).

If Windows does not start properly afterwards, go back to the checkpoint
"Before switching to virtio" that Rehome took.
"""

LINUX_PACKAGES = {
    "debian": "sudo apt install qemu-guest-agent spice-vdagent",
    "ubuntu": "sudo apt install qemu-guest-agent spice-vdagent",
    "linuxmint": "sudo apt install qemu-guest-agent spice-vdagent",
    "fedora": "sudo dnf install qemu-guest-agent spice-vdagent",
    "rhel": "sudo dnf install qemu-guest-agent spice-vdagent",
    "centos": "sudo dnf install qemu-guest-agent spice-vdagent",
    "rocky": "sudo dnf install qemu-guest-agent spice-vdagent",
    "almalinux": "sudo dnf install qemu-guest-agent spice-vdagent",
    "opensuse": "sudo zypper install qemu-guest-agent spice-vdagent",
    "sles": "sudo zypper install qemu-guest-agent spice-vdagent",
    "archlinux": "sudo pacman -S qemu-guest-agent spice-vdagent",
    "arch": "sudo pacman -S qemu-guest-agent spice-vdagent",
    "manjaro": "sudo pacman -S qemu-guest-agent spice-vdagent",
    "endeavouros": "sudo pacman -S qemu-guest-agent spice-vdagent",
}


@dataclass
class ToolsPlan:
    kind: str              # windows-iso / linux-packages / none
    message: str
    iso: str = ""
    command: str = ""
    needs_download: bool = False
    url: str = ""          # what Download fetches


def windows_version(cfg: VMConfig) -> tuple[int, int]:
    try:
        major, minor = (int(x) for x in cfg.os.version.split(".")[:2])
        return major, minor
    except ValueError:
        return 0, 0


def legacy_windows(cfg: VMConfig) -> bool:
    """Windows XP or Server 2003 (NT 5.1/5.2): tools come from the SPICE project."""
    return (cfg.os.family == "windows" and cfg.os.distro != "reactos"
            and (5, 1) <= windows_version(cfg) < (6, 0))


def mark_first_boot_ok(cfg: VMConfig) -> None:
    with library.locked(cfg):
        fresh = library.reload(cfg)
        if fresh.import_info:
            fresh.import_info.first_boot_ok = True
        library.save(fresh)


def xp_tools_iso() -> str | None:
    iso = paths.downloads_dir() / XP_TOOLS_ISO
    return str(iso) if iso.exists() else None


def plan(cfg: VMConfig) -> ToolsPlan:
    fam = cfg.os.family
    if legacy_windows(cfg):
        iso = xp_tools_iso()
        what = ("They install the QXL display driver (resolutions up to 2560×1600, such as "
                "1920×1080) and the SPICE agent; Windows XP needs Service Pack 3. Then shut "
                "Windows down and switch to the faster devices: the display becomes QXL.")
        if iso:
            return ToolsPlan("windows-iso", "Insert the guest tools disc, then run "
                             "spice-guest-tools-0.65.exe from it inside Windows. " + what,
                             iso=iso, url=XP_TOOLS_URL)
        return ToolsPlan("windows-iso", "The SPICE guest tools for Windows XP are needed (the "
                         "SPICE project's Windows tools, version 0.65, from the location "
                         "libosinfo lists for XP). " + what, needs_download=True, url=XP_TOOLS_URL)
    if fam == "windows" and cfg.os.distro != "reactos" and \
            (0, 0) < windows_version(cfg) < (5, 1):
        return ToolsPlan("none", "There are no guest tools for Windows 2000 and older; it uses "
                         "its built-in drivers.")
    if fam == "windows" and cfg.os.distro != "reactos":
        iso = virtio_win_path()
        if iso and iso.endswith(".iso"):
            return ToolsPlan("windows-iso", "Insert the virtio-win disc, then run "
                             "virtio-win-guest-tools.exe from it inside Windows. It installs the "
                             "virtio drivers, the QEMU guest agent and the SPICE agent "
                             "(clipboard, automatic resolution).", iso=iso)
        return ToolsPlan("windows-iso", "The virtio-win driver disc (from the Fedora project, "
                         "about 700 MB) is needed for the guest tools.", needs_download=True,
                         url=VIRTIO_WIN_URL)
    if fam == "linux":
        cmd = LINUX_PACKAGES.get(cfg.os.distro, "")
        return ToolsPlan("linux-packages",
                         "Most Linux distributions already include the virtio drivers. Install the "
                         "guest agent and SPICE agent inside the VM for clipboard sharing and "
                         "automatic resolution" + (":" if cmd else " (packages qemu-guest-agent and "
                                                   "spice-vdagent)."), command=cmd)
    return ToolsPlan("none", "There are no guest tools for this operating system.")


def download_virtio_win(progress=None, cancel=None) -> str:
    """Download virtio-win.iso from the Fedora project (official source)."""
    from rehome.media import download
    return str(download.download(VIRTIO_WIN_URL, filename="virtio-win.iso", progress=progress,
                                 cancel=cancel))


def download_xp_tools(progress=None, cancel=None) -> str:
    """Download the SPICE guest tools for Windows XP and put them on a disc."""
    from rehome.media import download
    exe = download.download(XP_TOOLS_URL, progress=progress, cancel=cancel)
    return make_xp_tools_iso(exe)


def make_xp_tools_iso(exe: str | os.PathLike, out: Path | None = None) -> str:
    from rehome.media.unattended import make_iso
    out = Path(out) if out else paths.ensure(paths.downloads_dir()) / XP_TOOLS_ISO
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copy(exe, root / Path(exe).name)
        (root / "README.TXT").write_bytes(XP_README.replace("\n", "\r\n").encode())
        part = out.with_name(out.name + ".part")
        make_iso(root, "REHOME_TOOLS", part)
        os.replace(part, out)
    os.chmod(out, 0o644)
    return str(out)


def download_tools(cfg: VMConfig, progress=None, cancel=None) -> str:
    """Download what ``plan(cfg)`` needs and return the disc to insert."""
    if legacy_windows(cfg):
        return download_xp_tools(progress, cancel)
    return download_virtio_win(progress, cancel)


def attach_tools(cfg: VMConfig, iso: str) -> None:
    cd = next((c for c in cfg.cdroms), None)
    if cd is None:
        raise RehomeError("This VM has no CD drive.",
                          fix="Turn it off and add a CD drive in the settings.")
    if runner.state(cfg).active:
        runner.change_cd(cfg, cd.id, iso)
    with library.locked(cfg):
        fresh = library.reload(cfg)
        fresh.cdroms[0].file = iso
        library.save(fresh)


DEVICE_NAMES = {"virtio": "VirtIO", "virtio-net-pci": "VirtIO network", "virtio-vga": "VirtIO GPU",
                "qxl-vga": "QXL graphics"}


def describe_changes(changes: dict) -> str:
    """"disk virtio, display QXL graphics" for the GUI and CLI."""
    what = {"disk_bus": "disks", "nic": "network", "display": "display"}
    return ", ".join(f"{what.get(k, k)}: {DEVICE_NAMES.get(v, v)}" for k, v in changes.items())


def paravirt_changes(cfg: VMConfig) -> dict:
    try:
        prof = profiles.get(cfg.profile)
    except KeyError:
        return {}
    return dict(prof.paravirt)


def _check_available(cfg: VMConfig, changes: dict) -> None:
    """Refuse a switch this computer's QEMU cannot run (it would fall back to plain VGA)."""
    from rehome.host import info as host_info
    caps = host_info.gather().caps(cfg.machine.arch)
    if not caps or not caps.devices:
        return
    for dev in (changes.get("display"), changes.get("nic")):
        if dev and not caps.has_device(dev):
            raise RehomeError(
                f"This computer's QEMU has no {DEVICE_NAMES.get(dev, dev)} ({dev}).",
                fix="Install QEMU's SPICE/QXL module (qemu-system-modules-spice on Debian and "
                    "Ubuntu), then try again." if dev == "qxl-vga" else
                    "Install the full QEMU system emulator package, then try again.",
                code="device-missing")


def switch_to_paravirt(cfg: VMConfig) -> list[str]:
    if runner.state(cfg).active:
        raise RehomeError("Shut the VM down first.")
    changes = paravirt_changes(cfg)
    if not changes:
        raise RehomeError("There are no faster virtual devices for this operating system.")
    _check_available(cfg, changes)
    snapshots.create(cfg, "Before switching to virtio",
                     "Taken automatically before Rehome switched the VM to paravirtual devices.",
                     kind="pre-tools")
    cfg = library.reload(cfg)
    log: list[str] = []

    def change(new: VMConfig):
        if "disk_bus" in changes:
            for d in new.disks:
                if d.bus != changes["disk_bus"]:
                    log.append(f"{d.id}: {d.bus} → {changes['disk_bus']}")
                    d.bus = changes["disk_bus"]
        if "nic" in changes:
            for n in new.nics:
                if n.model != changes["nic"]:
                    log.append(f"{n.id}: {n.model} → {changes['nic']}")
                    n.model = changes["nic"]
        if "display" in changes and new.display.device != changes["display"]:
            log.append(f"display: {new.display.device} → {changes['display']}")
            new.display.device = changes["display"]
        new.guest.paravirt = True
        new.guest.tools_installed = True
    edit.mutate(cfg, change)
    return log
