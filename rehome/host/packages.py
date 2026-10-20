"""Map logical components to distro packages and install commands."""

from __future__ import annotations

from functools import lru_cache

from rehome import paths
from rehome.util import toml

INSTALL_TEMPLATES = {
    "arch": "sudo pacman -S --needed {pkgs}",
    "debian": "sudo apt install {pkgs}",
    "fedora": "sudo dnf install {pkgs}",
    "suse": "sudo zypper install {pkgs}",
}


@lru_cache(maxsize=1)
def components() -> dict:
    return toml.load(paths.data_files_dir() / "packages.toml")


def packages_for(keys: list[str], family: str) -> list[str]:
    comps = components()
    pkgs: list[str] = []
    for k in keys:
        for p in comps.get(k, {}).get(family, []):
            if p not in pkgs:
                pkgs.append(p)
    return pkgs


def install_command(keys: list[str], family: str) -> str | None:
    pkgs = packages_for(keys, family)
    tmpl = INSTALL_TEMPLATES.get(family)
    if not pkgs or not tmpl:
        return None
    return tmpl.format(pkgs=" ".join(pkgs))
