"""After the first successful boot: guest tools, then paravirtual devices.

1. ``mark_first_boot_ok`` — the user confirmed the imported system works.
2. ``attach_tools`` — Windows: insert the virtio-win ISO (drivers, guest
   agent, SPICE agent); Linux: tell the user which packages to install.
3. ``switch_to_paravirt`` — once the drivers are in, move disks, network and
   display to virtio (a checkpoint is taken first so it can be undone).
"""

from __future__ import annotations

from dataclasses import dataclass

from rehome.errors import RehomeError
from rehome.patch import profiles
from rehome.patch.v2v import virtio_win_path
from rehome.vm import edit, library, runner, snapshots
from rehome.vm.config import VMConfig

VIRTIO_WIN_URL = ("https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
                  "stable-virtio/virtio-win.iso")

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


def mark_first_boot_ok(cfg: VMConfig) -> None:
    with library.locked(cfg):
        fresh = library.reload(cfg)
        if fresh.import_info:
            fresh.import_info.first_boot_ok = True
        library.save(fresh)


def plan(cfg: VMConfig) -> ToolsPlan:
    fam = cfg.os.family
    if fam == "windows" and cfg.os.distro != "reactos":
        iso = virtio_win_path()
        if iso and iso.endswith(".iso"):
            return ToolsPlan("windows-iso", "Insert the virtio-win disc, then run "
                             "virtio-win-guest-tools.exe from it inside Windows. It installs the "
                             "virtio drivers, the QEMU guest agent and the SPICE agent "
                             "(clipboard, automatic resolution).", iso=iso)
        return ToolsPlan("windows-iso", "The virtio-win driver disc (from the Fedora project, "
                         "about 700 MB) is needed for the guest tools.", needs_download=True)
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


def paravirt_changes(cfg: VMConfig) -> dict:
    try:
        prof = profiles.get(cfg.profile)
    except KeyError:
        return {}
    return dict(prof.paravirt)


def switch_to_paravirt(cfg: VMConfig) -> list[str]:
    if runner.state(cfg).active:
        raise RehomeError("Shut the VM down first.")
    changes = paravirt_changes(cfg)
    if not changes:
        raise RehomeError("There are no faster virtual devices for this operating system.")
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
