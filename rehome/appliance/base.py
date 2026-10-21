"""The appliance's base system: Ubuntu plus the packages Rehome needs, built once and cached.

The base is a qcow2 image holding one ext4 filesystem (no partition table).
Building it needs no root and no network inside libguestfs:

1. extract the pinned ubuntu-base tarball into a new filesystem;
2. copy the archive's signed indexes in and let apt check them (``apt-get
   update`` against a ``file:`` mirror);
3. ask apt which packages it would download (``--print-uris``), download
   exactly those on the host, copy them in, and install them offline.

Each appliance build (``build.py``) copies this filesystem into its own
image and configures it, so building a second appliance takes minutes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from rehome import paths
from rehome.appliance import image, ubuntu
from rehome.inspect import gfs
from rehome.util.units import human

# Bump when the way the base is built changes, so cached bases are rebuilt.
BASE_FORMAT = 2
MAX_AGE_DAYS = 30
MIRROR = ubuntu.MIRROR_IN_IMAGE

APT_CONF = """\
APT::Install-Recommends "false";
APT::Install-Suggests "false";
Acquire::Languages "none";
Dpkg::Options { "--force-confdef"; "--force-confold"; };
"""

# Documentation and translations are not needed in an appliance nobody logs into;
# copyright files stay (licences).
DPKG_EXCLUDES = """\
path-exclude=/usr/share/doc/*
path-include=/usr/share/doc/*/copyright
path-exclude=/usr/share/man/*
path-exclude=/usr/share/info/*
path-exclude=/usr/share/locale/*
path-include=/usr/share/locale/locale.alias
path-exclude=/usr/share/lintian/*
"""

# The initramfs must work on any computer, not only on libguestfs's helper VM where
# it is generated: include every storage and USB driver (dracut's generic mode).
DRACUT_CONF = """\
# Rehome appliance: a generic initramfs that boots on any computer.
hostonly="no"
hostonly_cmdline="no"
compress="zstd"
"""

# Rehome writes the boot menu itself; the kernel's post-install hook must not
# replace it with a generated one (grub-probe also fails inside libguestfs).
UPDATE_GRUB_STUB = """\
#!/bin/sh
echo "update-grub: the Rehome appliance manages its own boot menu (/boot/grub/grub.cfg)." >&2
exit 0
"""


@dataclass
class Base:
    path: Path
    meta: dict

    @property
    def size(self) -> int:
        return int(self.meta["size"])


def _key(release: ubuntu.Release) -> str:
    h = hashlib.sha256(json.dumps([BASE_FORMAT, release.sha256, release.suites,
                                   release.components, release.package_list]).encode())
    return h.hexdigest()[:16]


def _meta_path(img: Path) -> Path:
    return img.with_suffix(".json")


def cached(release: ubuntu.Release | None = None) -> Base | None:
    """The cached base for this release and package list, if there is one."""
    release = release or ubuntu.load_release()
    img = ubuntu.cache_dir() / f"base-{_key(release)}.qcow2"
    try:
        meta = json.loads(_meta_path(img).read_text())
    except (OSError, ValueError):
        return None
    return Base(img, meta) if img.exists() else None


def is_stale(base: Base) -> bool:
    return time.time() - base.meta.get("built", 0) > MAX_AGE_DAYS * 86400


def ensure(progress=None, refresh: bool = False) -> Base:
    """The base image, built (or rebuilt with current updates) when needed."""
    release = ubuntu.load_release()
    base = cached(release)
    if base and not refresh and not is_stale(base):
        return base
    return build(release, progress)


def _stage(progress, name: str, detail: str = "") -> None:
    if progress:
        progress.stage(name, detail)


def _frac(progress):
    return progress.fraction_cb() if progress else None


def _cancel(progress):
    return progress.cancel_cb() if progress else None


def build(release: ubuntu.Release, progress=None) -> Base:
    cache = ubuntu.cache_dir()
    img = cache / f"base-{_key(release)}.qcow2"
    tmp = img.with_name(img.name + ".building")
    tmp.unlink(missing_ok=True)

    _stage(progress, "Downloading Ubuntu", release.name)
    tarball = ubuntu.base_tarball(release, progress=_dl(progress), cancel=_cancel(progress))
    _stage(progress, "Downloading package lists")
    mirror = ubuntu.fetch_indexes(release, progress=_frac(progress), cancel=_cancel(progress))

    size = release.root_gib * 2**30
    from rehome.vm import disks
    disks.create(tmp, size)
    try:
        with gfs.session([gfs.Drive(str(tmp), "qcow2", False, discard=True)], memsize=2048) as g:
            _stage(progress, "Preparing the system")
            g.mkfs("ext4", "/dev/sda", label="rehome-root")
            g.mount("/dev/sda", "/")
            g.tar_in(str(tarball), "/", compress="gzip")
            _configure_apt(g, release)
            image.tar_in(g, [(mirror.local(f), f) for f in mirror.files], MIRROR)
            image.run(g, "apt-get update", "checking the Ubuntu package lists")

            _stage(progress, "Choosing packages")
            pkgs = " ".join(image.quote(p) for p in release.package_list)
            uris = image.run(g, "apt-get -y --print-uris dist-upgrade\n"
                                f"apt-get -y --print-uris install {pkgs}",
                             "choosing the packages to install")
            pool = ubuntu.parse_print_uris(uris)

            _stage(progress, "Downloading packages", f"{len(set(pool))} packages")
            ubuntu.fetch_packages(release, mirror, pool, progress=_frac(progress),
                                  cancel=_cancel(progress))
            image.tar_in(g, [(mirror.local(p), p) for p in sorted(set(pool))], MIRROR)

            _stage(progress, "Installing packages")
            image.run(g, f"apt-get -y dist-upgrade\napt-get -y install {pkgs}",
                      "installing packages")
            versions = image.run(g, "dpkg-query -W -f '${Package} ${Version}\\n'",
                                 "listing the installed packages")
            _finish_apt(g)
            image.run(g, "test -s /boot/vmlinuz && test -s /boot/initrd.img",
                      "checking the kernel and its initramfs were installed")
            used = g.statvfs("/")
            g.fstrim("/")
            g.umount_all()
        meta = {"format": BASE_FORMAT, "release": release.name, "codename": release.codename,
                "built": time.time(), "size": size,
                "used": (used["blocks"] - used["bfree"]) * used["frsize"],
                "packages": dict(line.split(" ", 1) for line in versions.splitlines()
                                 if " " in line)}
        os.replace(tmp, img)
        _meta_path(img).write_text(json.dumps(meta, indent=1, sort_keys=True))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _cleanup(img, {p.rsplit("/", 1)[-1] for p in pool})
    return Base(img, meta)


def _dl(progress):
    if not progress:
        return None
    return lambda done, total, _speed, _eta: progress.update(done / total if total else 0)


def _configure_apt(g, release: ubuntu.Release) -> None:
    # The base's own sources (Ubuntu's mirrors) are restored after the build.
    if g.exists("/etc/apt/sources.list.d/ubuntu.sources"):
        g.mv("/etc/apt/sources.list.d/ubuntu.sources", "/etc/apt/ubuntu.sources.rehome")
    g.write("/etc/apt/sources.list.d/rehome-build.sources", (
        "Types: deb\n"
        f"URIs: file:{MIRROR}\n"
        f"Suites: {' '.join(release.suites)}\n"
        f"Components: {' '.join(release.components)}\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n").encode())
    g.write("/etc/apt/apt.conf.d/90rehome", APT_CONF.encode())
    g.mkdir_p("/etc/dpkg/dpkg.cfg.d")
    g.write("/etc/dpkg/dpkg.cfg.d/90rehome-minimal", DPKG_EXCLUDES.encode())
    g.mkdir_p("/etc/dracut.conf.d")
    g.write("/etc/dracut.conf.d/90-rehome.conf", DRACUT_CONF.encode())
    # No service may start while packages are installed into the image.
    g.write("/usr/sbin/policy-rc.d", b"#!/bin/sh\nexit 101\n")
    g.chmod(0o755, "/usr/sbin/policy-rc.d")
    image.run(g, "dpkg-divert --local --rename --divert /usr/sbin/update-grub.distrib "
                 "--add /usr/sbin/update-grub", "setting up the boot menu")
    g.write("/usr/sbin/update-grub", UPDATE_GRUB_STUB.encode())
    g.chmod(0o755, "/usr/sbin/update-grub")


def _finish_apt(g) -> None:
    g.rm_rf("/etc/apt/sources.list.d/rehome-build.sources")
    if g.exists("/etc/apt/ubuntu.sources.rehome"):
        g.mv("/etc/apt/ubuntu.sources.rehome", "/etc/apt/sources.list.d/ubuntu.sources")
    g.rm_rf("/usr/sbin/policy-rc.d")
    image.run(g, "apt-get clean\nrm -rf /var/lib/apt/lists/* " + MIRROR,
              "cleaning up after installing packages")


def _cleanup(current: Path, keep_debs: set[str]) -> None:
    for p in current.parent.glob("base-*.qcow2"):
        if p != current:
            p.unlink(missing_ok=True)
            _meta_path(p).unlink(missing_ok=True)
    ubuntu.prune_debs(keep_debs)


def describe(base: Base) -> str:
    return (f"{base.meta.get('release', '?')}, {len(base.meta.get('packages', {}))} packages, "
            f"{human(base.meta.get('used', 0))} used")


def cache_root() -> Path:
    return paths.cache_dir() / "appliance"
