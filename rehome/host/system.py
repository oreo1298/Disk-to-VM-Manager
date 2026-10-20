"""Distribution, package manager, desktop and display session detection."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from rehome.util.proc import which


@dataclass
class OsRelease:
    id: str = "linux"
    id_like: list[str] = field(default_factory=list)
    name: str = "Linux"
    pretty_name: str = "Linux"
    version_id: str = ""
    variant_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def family(self) -> str:
        """One of arch, debian, fedora, suse or the raw ID."""
        ids = [self.id] + self.id_like
        for fam, members in FAMILIES.items():
            if any(i in members for i in ids):
                return fam
        return self.id


FAMILIES = {
    "arch": {"arch", "archarm", "manjaro", "endeavouros", "garuda", "artix", "cachyos"},
    "debian": {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "kali",
               "raspbian", "neon", "devuan", "mx"},
    "fedora": {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine"},
    "suse": {"suse", "opensuse", "opensuse-tumbleweed", "opensuse-leap", "sles",
             "opensuse-slowroll", "opensuse-aeon", "opensuse-kalpa"},
}


def parse_os_release(text: str) -> OsRelease:
    raw: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            parts = shlex.split(v)
            v = parts[0] if parts else ""
        except ValueError:
            v = v.strip("'\"")
        raw[k.strip()] = v
    return OsRelease(
        id=raw.get("ID", "linux").lower(),
        id_like=raw.get("ID_LIKE", "").lower().split(),
        name=raw.get("NAME", "Linux"),
        pretty_name=raw.get("PRETTY_NAME", raw.get("NAME", "Linux")),
        version_id=raw.get("VERSION_ID", ""),
        variant_id=raw.get("VARIANT_ID", ""),
        raw=raw,
    )


def detect_os_release(root: str = "/") -> OsRelease:
    for p in ("etc/os-release", "usr/lib/os-release"):
        f = Path(root, p)
        if f.exists():
            try:
                return parse_os_release(f.read_text(errors="replace"))
            except OSError:
                continue
    return OsRelease()


PACKAGE_MANAGERS = {
    # family: (binary, install command template, query command)
    "arch": ("pacman", "sudo pacman -S --needed {pkgs}"),
    "debian": ("apt", "sudo apt install {pkgs}"),
    "fedora": ("dnf", "sudo dnf install {pkgs}"),
    "suse": ("zypper", "sudo zypper install {pkgs}"),
}


def detect_package_manager(osr: OsRelease) -> str | None:
    fam = osr.family
    if fam in PACKAGE_MANAGERS:
        binary = PACKAGE_MANAGERS[fam][0]
        if fam == "fedora" and not which("dnf") and which("dnf5"):
            return "dnf"
        if which(binary) or which("apt-get" if binary == "apt" else binary):
            return binary
    for binary in ("pacman", "apt", "dnf", "zypper"):
        if which(binary):
            return binary
    return None


@dataclass
class Session:
    desktop: str = "unknown"
    session_type: str = "unknown"   # wayland / x11 / tty / unknown
    wayland_display: str = ""
    x11_display: str = ""

    @property
    def is_graphical(self) -> bool:
        return bool(self.wayland_display or self.x11_display)


def detect_session(env: dict | None = None) -> Session:
    env = os.environ if env is None else env
    desktop = env.get("XDG_CURRENT_DESKTOP") or env.get("XDG_SESSION_DESKTOP") \
        or env.get("DESKTOP_SESSION") or ""
    if not desktop and env.get("KDE_FULL_SESSION"):
        desktop = "KDE"
    stype = (env.get("XDG_SESSION_TYPE") or "").lower()
    wl = env.get("WAYLAND_DISPLAY", "")
    x = env.get("DISPLAY", "")
    if not stype:
        stype = "wayland" if wl else ("x11" if x else "tty")
    return Session(desktop=desktop or "unknown", session_type=stype,
                   wayland_display=wl, x11_display=x)


def desktop_family(desktop: str) -> str:
    d = desktop.lower()
    for name in ("gnome", "kde", "xfce", "cinnamon", "mate", "lxqt", "budgie",
                 "pantheon", "sway", "hyprland", "cosmic", "unity"):
        if name in d:
            return name
    return d or "unknown"
