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
# Current virtio-win supports Windows 8 and later (its installer refuses older
# systems). Older Windows gets the SPICE guest tools that libosinfo's database lists
# for it (also used by GNOME Boxes): QXL display driver, SPICE agent and the virtio
# drivers of their time.
@dataclass(frozen=True)
class LegacyTools:
    systems: str           # for messages
    url: str
    iso: str               # the disc Rehome makes in the downloads folder
    note: str              # what to know before installing
    switch: str            # what "switch to faster devices" changes afterwards


LEGACY_TOOLS = {
    "xp": LegacyTools(
        "Windows XP and Server 2003",
        "https://zeenix.fedorapeople.org/drivers/win-tools/postinst/spice-guest-tools-0.65.exe",
        "rehome-windows-xp-tools.iso",
        "Windows XP needs Service Pack 3.",
        "the display becomes QXL: resolutions up to 2560×1600, such as 1920×1080"),
    "vista7": LegacyTools(
        "Windows 7 (and Vista)",
        "https://fedorapeople.org/groups/virt/unattended/drivers/postinst/spice-guest-tools/"
        "0.141/spice-guest-tools-0.141.exe",
        "rehome-windows-7-tools.iso",
        "libosinfo lists this version for Windows 7; Vista is not listed and untested.",
        "disks and network become virtio, the display QXL"),
}
XP_TOOLS_URL = LEGACY_TOOLS["xp"].url
XP_TOOLS_ISO = LEGACY_TOOLS["xp"].iso
# Profiles of the systems each tool set is for, when the VM has no version number
# (the system was chosen by hand in the New VM wizard).
ERA_BY_PROFILE = {"windows-xp": "xp", "windows-vista-7": "vista7", "windows-2000": "old",
                  "windows-nt4": "old"}

README = """\
Rehome guest tools for {systems}
{line}

1. Run {exe} and follow the installer. {note}
   Windows asks whether to continue with drivers that are not signed by
   Microsoft: choose to install them (Continue Anyway / Install).
2. Shut Windows down.
3. In Rehome, switch the VM to the faster devices (Summary tab, or
   `rehome tools paravirt VM`): {switch}.
4. Start the VM again.

If Windows does not start properly afterwards, go back to the checkpoint
"Before switching to virtio" that Rehome took first.
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


def windows_era(cfg: VMConfig) -> str:
    """"xp", "vista7", "old" (2000, NT), "modern" (8 and later), or "" (not Windows)."""
    if cfg.os.family != "windows" or cfg.os.distro == "reactos":
        return ""
    v = windows_version(cfg)
    if v == (0, 0):
        return ERA_BY_PROFILE.get(cfg.profile, "modern")
    if v < (5, 1):
        return "old"
    if v < (6, 0):
        return "xp"
    if v < (6, 2):
        return "vista7"
    return "modern"


def legacy_windows(cfg: VMConfig) -> bool:
    """Windows XP to 7: guest tools from the SPICE project, not current virtio-win."""
    return windows_era(cfg) in LEGACY_TOOLS


def mark_first_boot_ok(cfg: VMConfig) -> None:
    with library.locked(cfg):
        fresh = library.reload(cfg)
        if fresh.import_info:
            fresh.import_info.first_boot_ok = True
        library.save(fresh)


def _tools_iso(era: str) -> str | None:
    iso = paths.downloads_dir() / LEGACY_TOOLS[era].iso
    return str(iso) if iso.exists() else None


def xp_tools_iso() -> str | None:
    return _tools_iso("xp")


def plan(cfg: VMConfig) -> ToolsPlan:
    fam = cfg.os.family
    era = windows_era(cfg)
    if era in LEGACY_TOOLS:
        t = LEGACY_TOOLS[era]
        exe = t.url.rsplit("/", 1)[1]
        what = (f"They install the QXL display driver, the virtio drivers and the SPICE agent. "
                f"{t.note} Then shut Windows down and switch to the faster devices: {t.switch}.")
        iso = _tools_iso(era)
        if iso:
            return ToolsPlan("windows-iso", f"Insert the guest tools disc, then run {exe} from "
                             f"it inside Windows. " + what, iso=iso, url=t.url)
        return ToolsPlan("windows-iso", f"The SPICE guest tools for {t.systems} are needed "
                         f"({exe}, from the location libosinfo lists; current virtio-win needs "
                         f"Windows 8 or later). " + what, needs_download=True, url=t.url)
    if era == "old":
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


def download_legacy_tools(era: str, progress=None, cancel=None) -> str:
    """Download the SPICE guest tools for an older Windows and put them on a disc."""
    from rehome.media import download
    exe = download.download(LEGACY_TOOLS[era].url, progress=progress, cancel=cancel)
    return make_tools_iso(exe, era)


def download_xp_tools(progress=None, cancel=None) -> str:
    return download_legacy_tools("xp", progress, cancel)


def make_tools_iso(exe: str | os.PathLike, era: str, out: Path | None = None) -> str:
    from rehome.media.unattended import make_iso
    t = LEGACY_TOOLS[era]
    out = Path(out) if out else paths.ensure(paths.downloads_dir()) / t.iso
    title = f"Rehome guest tools for {t.systems}"
    readme = README.format(systems=t.systems, line="=" * len(title), exe=Path(exe).name,
                           note=t.note, switch=t.switch)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copy(exe, root / Path(exe).name)
        (root / "README.TXT").write_bytes(readme.replace("\n", "\r\n").encode())
        part = out.with_name(out.name + ".part")
        make_iso(root, "REHOME_TOOLS", part)
        os.replace(part, out)
    os.chmod(out, 0o644)
    return str(out)


def make_xp_tools_iso(exe: str | os.PathLike, out: Path | None = None) -> str:
    return make_tools_iso(exe, "xp", out)


def download_tools(cfg: VMConfig, progress=None, cancel=None) -> str:
    """Download what ``plan(cfg)`` needs and return the disc to insert."""
    era = windows_era(cfg)
    if era in LEGACY_TOOLS:
        return download_legacy_tools(era, progress, cancel)
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
