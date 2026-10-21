"""Real unattended Ubuntu Server install (opt-in; slow, especially without KVM).

    REHOME_E2E_UBUNTU_ISO=/path/to/ubuntu-24.04.x-live-server-amd64.iso \
        python3 -m pytest tests/integration/test_unattended_ubuntu_e2e.py

The ISO can be fetched with `rehome download ubuntu-server-lts`. The test
creates the VM, prepares the autoinstall, boots the installer, waits for it
to power the VM off, checks the installed disk with libguestfs, then boots
the installed system once. REHOME_E2E_TIMEOUT (seconds, default 3 h) bounds
the installation.
"""

import os
import time
from pathlib import Path

import pytest

ISO = os.environ.get("REHOME_E2E_UBUNTU_ISO", "")
pytestmark = [pytest.mark.slow, pytest.mark.boot,
              pytest.mark.skipif(not ISO or not Path(ISO).is_file(),
                                 reason="set REHOME_E2E_UBUNTU_ISO to a live-server ISO")]


def test_ubuntu_server_installs_unattended():
    from rehome.media import unattended
    from rehome.vm import create, library, runner
    cfg = create.create(create.NewVMSpec(name="Ubuntu autoinstall e2e", iso=ISO,
                                         os="ubuntu24.04", disk_size=20 * 2**30))
    notes = unattended.prepare(cfg, unattended.Answers(
        user="alice", password="Rehome-e2e-Pass1", locale="en_US", keyboard="us",
        timezone="Europe/Berlin", hostname="rehome-e2e"))
    assert notes
    try:
        runner.start(cfg, headless=True)
        assert library.reload(cfg).direct_boot is None        # used once, then cleared
        timeout = float(os.environ.get("REHOME_E2E_TIMEOUT", 3 * 3600))
        assert runner.wait_stopped(cfg, timeout), "the installer did not power the VM off"
        runner.after_stop(cfg)

        from rehome.inspect.gfs import new_handle
        g = new_handle()
        g.add_drive_opts(str(cfg.dir / "disks/disk0.qcow2"), format="qcow2", readonly=1)
        g.launch()
        roots = g.inspect_os()
        assert roots, "no operating system on the disk"
        root = roots[0]
        assert g.inspect_get_distro(root) == "ubuntu"
        for mp, dev in sorted(g.inspect_get_mountpoints(root).items(), key=lambda m: len(m[0])):
            g.mount_ro(dev, mp)
        assert g.cat("/etc/hostname").strip() == "rehome-e2e"
        assert any(line.startswith("alice:") for line in g.cat("/etc/passwd").splitlines())
        assert g.is_dir("/home/alice")
        g.close()

        # Boots the installed system from the disk now.
        cfg = library.reload(cfg)
        runner.start(cfg, headless=True)
        time.sleep(30)
        assert runner.is_running(cfg)
    finally:
        if runner.is_running(cfg):
            runner.force_off(cfg)
