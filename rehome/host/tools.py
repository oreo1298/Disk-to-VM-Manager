"""Inventory of external programs and libraries Rehome uses."""

from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import dataclass

from rehome.util.proc import output, which


@dataclass
class Tool:
    key: str            # logical name, also the key in data/packages.toml
    label: str
    purpose: str
    required: bool      # required for the core feature set
    found: bool = False
    path: str = ""
    version: str = ""
    note: str = ""


def _version(cmd: list[str], pattern: str = r"(\d+\.\d+(?:\.\d+)?)") -> str:
    out = output(cmd, timeout=20)
    m = re.search(pattern, out)
    return m.group(1) if m else ""


def _bin_tool(key, label, purpose, required, names, version_args=("--version",),
              extra_dirs=()) -> Tool:
    t = Tool(key, label, purpose, required)
    for n in names:
        p = which(n, tuple(extra_dirs))
        if p:
            t.found, t.path = True, p
            if version_args:
                t.version = _version([p, *version_args])
            break
    return t


def _python_module(key, label, purpose, required, module) -> Tool:
    t = Tool(key, label, purpose, required)
    try:
        mod = __import__(module)
        t.found, t.path = True, getattr(mod, "__file__", "") or module
    except Exception as e:  # ImportError or broken bindings
        t.note = str(e)
    return t


def _gi(key, label, purpose, required, namespace, version) -> Tool:
    t = Tool(key, label, purpose, required)
    try:
        import gi
        gi.require_version(namespace, version)
        mod = __import__("gi.repository", fromlist=[namespace])
        lib = getattr(mod, namespace)
        t.found, t.path = True, f"gi:{namespace}-{version}"
        for attr in ("get_major_version", "MAJOR_VERSION"):
            if hasattr(lib, attr):
                if namespace == "Gtk":
                    t.version = f"{lib.get_major_version()}.{lib.get_minor_version()}.{lib.get_micro_version()}"
                elif namespace == "Adw":
                    t.version = f"{lib.get_major_version()}.{lib.get_minor_version()}.{lib.get_micro_version()}"
                break
    except Exception as e:
        t.note = str(e)
    return t


def virtiofsd_path() -> str | None:
    return which("virtiofsd", ("/usr/libexec", "/usr/lib", "/usr/lib/qemu",
                               "/usr/libexec/virtiofsd"))


def inventory() -> list[Tool]:
    tools = [
        _bin_tool("qemu-img", "qemu-img", "Creates overlays, copies and converts disk images.",
                  True, ["qemu-img"]),
        _bin_tool("swtpm", "swtpm", "Software TPM 2.0 for Windows 11 and BitLocker.",
                  False, ["swtpm"]),
        _bin_tool("swtpm_setup", "swtpm_setup", "Initialises software TPM state.",
                  False, ["swtpm_setup"]),
        _python_module("libguestfs", "libguestfs (Python bindings)",
                       "Identifies the installed OS and applies offline fixes.", True, "guestfs"),
        _bin_tool("virt-v2v", "virt-v2v", "Converts Windows 7+ and major Linux guests to virtio.",
                  False, ["virt-v2v"], ("--version",)),
        _bin_tool("virt-v2v-in-place", "virt-v2v-in-place", "In-place conversion of overlays.",
                  False, ["virt-v2v-in-place"], ("--version",)),
        _bin_tool("nbdkit", "nbdkit", "Serves physical disks to offline tools.", False, ["nbdkit"]),
        _bin_tool("virtiofsd", "virtiofsd", "Shared folders (virtiofs).", False, ["virtiofsd"],
                  ("--version",), ("/usr/libexec", "/usr/lib", "/usr/lib/qemu")),
        _bin_tool("passt", "passt", "Fast unprivileged NAT networking.", False, ["passt"],
                  ("--version",)),
        _bin_tool("smartctl", "smartmontools", "Checks disk health before import.", False,
                  ["smartctl"], ("--version",), ("/usr/sbin", "/sbin")),
        _bin_tool("ddrescue", "GNU ddrescue", "Rescues failing disks to an image file.", False,
                  ["ddrescue"]),
        _bin_tool("ewfmount", "libewf tools", "Reads E01 forensic images.", False,
                  ["ewfmount"], ("-V",)),
        _bin_tool("pkexec", "pkexec (polkit)", "Asks for permission to open physical disks.",
                  False, ["pkexec"], ("--version",)),
        _bin_tool("xorriso", "xorriso", "Builds unattended-install media.", False,
                  ["xorriso", "mkisofs", "genisoimage"], ("-version",)),
        _bin_tool("mkfs.fat", "dosfstools", "Builds FAT media for DOS and Windows installs.",
                  False, ["mkfs.fat", "mkfs.vfat"], ("--help",), ("/usr/sbin", "/sbin")),
        _bin_tool("lsblk", "util-linux", "Lists disks.", True, ["lsblk"]),
        _gi("libosinfo", "libosinfo", "Recognises installer ISOs and recommends settings.",
            False, "Libosinfo", "1.0"),
        _gi("gtk4", "GTK 4", "Graphical interface.", False, "Gtk", "4.0"),
        _gi("libadwaita", "libadwaita", "Graphical interface.", False, "Adw", "1"),
    ]
    by_key = {t.key: t for t in tools}
    v2v = by_key["virt-v2v"]
    if v2v.found and not by_key["virt-v2v-in-place"].found:
        v2v.note = "virt-v2v-in-place is missing; whole-disk conversion will be used instead."
    if by_key["mkfs.fat"].found and not by_key["mkfs.fat"].version:
        by_key["mkfs.fat"].version = _version([by_key["mkfs.fat"].path, "--help"],
                                              r"mkfs\.fat (\d+\.\d+\.\d+)")
    return tools


def helper_installed() -> str | None:
    """Path of the installed privileged helper, if any."""
    env = os.environ.get("REHOME_HELPER_PATH")
    if env and os.path.isfile(env):
        return env
    for p in ("/usr/libexec/rehome/rehome-helper", "/usr/lib/rehome/rehome-helper",
              "/usr/local/libexec/rehome/rehome-helper"):
        if os.path.isfile(p):
            return p
    return None


def polkit_policy_installed() -> bool:
    return os.path.isfile("/usr/share/polkit-1/actions/io.github.oreo1298.Rehome.policy")


GUESTFS_BINDING_GLOBS = (
    "/usr/lib*/python3*/site-packages/guestfs.py",
    "/usr/lib*/python3*/dist-packages/guestfs.py",
    "/usr/lib/python3/dist-packages/guestfs.py",
    "/usr/local/lib*/python3*/site-packages/guestfs.py",
)


def guestfs_diagnosis(import_error: str) -> tuple[str, str]:
    """Why ``import guestfs`` fails, in plain English: (detail, fix)."""
    me = f"Python {sys.version_info.major}.{sys.version_info.minor} ({sys.executable})"
    if import_error and "No module named 'guestfs'" not in import_error:
        return (f"libguestfs' Python bindings are installed but cannot be loaded by {me}: "
                f"{import_error}",
                "Reinstall libguestfs. If the error stays, the distribution's package is broken "
                "for this Python version; please report it to your distribution.")
    here = {os.path.realpath(p) for p in sys.path if p}
    elsewhere = sorted({p for pat in GUESTFS_BINDING_GLOBS for p in glob.glob(pat)
                        if os.path.realpath(os.path.dirname(p)) not in here})
    mine = f"python{sys.version_info.major}.{sys.version_info.minor}"
    other = sorted({m for p in elsewhere for m in re.findall(r"python3\.\d+", p) if m != mine})
    if other:
        return (f"libguestfs' Python bindings are installed only for {', '.join(other)} "
                f"({', '.join(elsewhere)}), but Rehome runs on {me}.",
                "The distribution's libguestfs package was built for a different Python version "
                "than the one installed. Update the system once libguestfs has been rebuilt, or "
                "report it to your distribution.")
    if elsewhere:
        return (f"libguestfs' Python bindings are installed in {', '.join(elsewhere)}, which "
                f"{me} does not search (a virtual environment?).",
                "Run Rehome with the system Python (/usr/bin/python3), as the installer sets up.")
    if which("guestfish") or glob.glob("/usr/lib*/libguestfs.so*") or \
            glob.glob("/usr/lib/*/libguestfs.so*"):
        return (f"libguestfs is installed, but without Python bindings for {me}.",
                "Install libguestfs' Python bindings (Debian/Ubuntu: python3-guestfs; "
                "Fedora/openSUSE: python3-libguestfs; Arch includes them in the libguestfs "
                "package). Check with: python3 -c 'import guestfs'")
    return ("libguestfs is not installed.", "Install libguestfs.")

