"""virt-v2v integration: install virtio drivers into Windows 7+ guests.

``virt-v2v-in-place -i disk`` converts the VM's overlay in place: it copies
virtio drivers from virtio-win into the guest, registers them as boot
drivers and removes other hypervisors' tools where it knows how. It needs
the virtio-win driver collection (packaged on Fedora/RHEL, AUR on Arch, or
downloaded by Rehome from the Fedora project).
"""

from __future__ import annotations

import os
import subprocess

from rehome import paths
from rehome.patch.registry import EXTERNAL, PatchResult, patch
from rehome.util.proc import which

VIRTIO_WIN_LOCATIONS = ("/usr/share/virtio-win/virtio-win.iso", "/usr/share/virtio-win",
                        "/usr/share/virtio-win/drivers")


def virtio_win_path() -> str | None:
    env = os.environ.get("VIRTIO_WIN")
    if env and os.path.exists(env):
        return env
    for p in VIRTIO_WIN_LOCATIONS:
        if os.path.exists(p):
            return p
    dl = paths.downloads_dir() / "virtio-win.iso"
    return str(dl) if dl.exists() else None


@patch("windows.virt_v2v", "Install virtio drivers with virt-v2v", kind=EXTERNAL, families=("windows",))
def virt_v2v(ctx) -> PatchResult:
    o = ctx.os
    if o.distro == "reactos":
        return PatchResult("", "", "not-needed", "Not used for ReactOS.")
    try:
        major, minor = (int(x) for x in o.version.split(".")[:2])
    except ValueError:
        major, minor = 0, 0
    if (major, minor) < (6, 1):
        return PatchResult("", "", "not-needed", "virt-v2v supports Windows 7 / Server 2008 R2 and later.")
    tool = which("virt-v2v-in-place")
    if not tool:
        return PatchResult("", "", "skipped",
                           "virt-v2v is not installed; Rehome's own fixes make Windows boot on SATA. "
                           "Install virtio drivers later with the guest tools step.")
    vw = virtio_win_path()
    if not vw:
        return PatchResult("", "", "skipped",
                           "The virtio-win driver collection is not available, so virt-v2v was "
                           "skipped. Windows boots on SATA; the guest tools step can add virtio "
                           "drivers after the first boot.")
    if any(v.encryption == "bitlocker" for d in (ctx.report.disks if ctx.report else [])
           for v in d.volumes) and not getattr(ctx.options, "keys", None):
        return PatchResult("", "", "skipped", "The system volume is BitLocker-encrypted.")
    root_disk = ctx.cfg.disks[o.disk_index] if o.disk_index < len(ctx.cfg.disks) else ctx.cfg.disks[0]
    log_path = ctx.cfg.dir / "logs" / "virt-v2v.log"
    env = dict(os.environ, VIRTIO_WIN=vw, LIBGUESTFS_BACKEND=os.environ.get("LIBGUESTFS_BACKEND", "direct"))
    argv = [tool, "-i", "disk", str(ctx.cfg.resolve(root_disk.file)), "--block-driver", "virtio-blk",
            "-v", "-x"]
    with open(log_path, "w") as logf:
        try:
            cp = subprocess.run(argv, stdout=logf, stderr=subprocess.STDOUT, env=env, timeout=3600)
        except subprocess.TimeoutExpired:
            return PatchResult("", "", "failed", f"virt-v2v took more than an hour (see {log_path}).")
    if cp.returncode != 0:
        tail = log_path.read_text(errors="replace").splitlines()[-5:]
        return PatchResult("", "", "failed",
                           f"virt-v2v could not convert this Windows ({' '.join(tail)[-300:]}). "
                           "Rehome's own fixes still apply.")
    ctx.state["virtio_installed"] = True
    return PatchResult("", "", "applied",
                       "virtio drivers were installed. The first boot still uses SATA; switch to "
                       "virtio from the VM's guest tools page once Windows has started.",
                       [f"virt-v2v-in-place converted {root_disk.file} (log: logs/virt-v2v.log)"])
