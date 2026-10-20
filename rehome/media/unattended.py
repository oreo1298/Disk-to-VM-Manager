"""Unattended installation: answer files, answer media and installer boot.

``prepare(cfg, answers)`` turns a freshly created VM (installer ISO attached)
into one that installs itself:

* **Windows (Vista and later)** – an ``autounattend.xml`` on an extra CD.
  It creates a local administrator account, partitions the disk for the VM's
  firmware (GPT/UEFI or MBR/BIOS) and skips the OOBE screens with Microsoft's
  documented unattend settings. Nothing bypasses Windows 11's TPM or Secure
  Boot checks (the VM has a real swtpm and Secure Boot), no product key is
  invented, and no Microsoft-account workarounds are used. Windows DVDs ask
  "Press any key to boot from CD or DVD"; :func:`after_start` presses it.
* **Ubuntu live installers (subiquity, 20.04+ server, 24.04+ desktop)** – a
  NoCloud ``cidata`` CD with ``user-data`` (autoinstall) and ``meta-data``;
  the kernel and initrd are taken from the ISO and booted directly with the
  ``autoinstall`` argument.
* **Everything libosinfo has an install script for** (Fedora/RHEL family
  kickstart, openSUSE AutoYaST, Debian preseed, …) – the script is generated
  by libosinfo. Disk/CD-injected scripts go on an ``OEMDRV`` CD; initrd
  injected ones (preseed) are appended to the installer's initrd. The kernel
  is booted directly with the arguments the ISO's own boot menu uses plus the
  ones libosinfo asks for.

Linux installers are told to power the VM off when they finish (a reboot
would re-enter the installer, because QEMU keeps a ``-kernel`` for the life
of the process). The direct-boot setting is removed by the runner after the
first start, and the disk is first in the boot order, so starting the VM
again boots the installed system.

Passwords only ever exist in the generated files inside the VM directory
(mode 0600, directory 0700); they are never logged or put on a command line.
"""

from __future__ import annotations

import getpass
import gzip
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape as _xml_escape

from rehome.errors import Cancelled, RehomeError
from rehome.util.proc import run as run_cmd
from rehome.util.proc import which
from rehome.vm.config import CdRom, DirectBoot, VMConfig

log = logging.getLogger(__name__)

ANSWER_DIR = "unattended"
ANSWER_ISO = f"{ANSWER_DIR}/answers.iso"
STATE_FILE = "state.json"
SERIAL_CONSOLE = ["console=ttyS0,115200n8", "console=tty0"]


@dataclass
class Answers:
    user: str = ""                 # login name ("" = your login name)
    password: str = ""
    full_name: str = ""
    product_key: str = ""          # Windows only; leave empty for evaluation media
    locale: str = ""               # e.g. en_US ("" = this computer's)
    keyboard: str = ""             # e.g. us, de ("" = this computer's)
    timezone: str = ""             # IANA name, e.g. Europe/Berlin ("" = this computer's)
    hostname: str = ""             # "" = derived from the VM name
    edition: str = ""              # Windows image name ("Windows 11 Pro") or index ("6")
    profile: str = ""              # libosinfo script profile: desktop / jeos ("" = desktop)

    def __repr__(self) -> str:      # never show the password in logs or tracebacks
        safe = replace(self, password="***" if self.password else "",
                       product_key="***" if self.product_key else "")
        return "Answers(" + ", ".join(f"{k}={v!r}" for k, v in safe.__dict__.items()) + ")"


@dataclass
class Support:
    """Whether (and how) a VM can be installed unattended."""
    ok: bool
    method: str = ""               # windows / subiquity / osinfo
    reason: str = ""
    fix: str = ""


@dataclass
class _Plan:
    method: str
    iso: str
    installer_cd: str
    os_name: str = ""
    files: list[str] = field(default_factory=list)
    volume_id: str = ""
    kernel: str = ""               # path inside the ISO (no leading /)
    initrd: str = ""
    variant: str = ""              # subiquity: server / desktop
    version: str = ""
    languages: list[str] = field(default_factory=list)
    media: object = None           # Libosinfo.Media (identified) or None
    os_obj: object = None          # Libosinfo.Os
    scripts: list = field(default_factory=list)


# --- host defaults ---------------------------------------------------------------------

def host_locale() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        m = re.match(r"([a-z]{2,3}_[A-Z]{2})", v)
        if m:
            return m.group(1)
    return "en_US"


def host_timezone() -> str:
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz and not tz.startswith("/"):
        return tz
    try:
        t = Path("/etc/timezone").read_text().strip()
        if t:
            return t
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        m = re.search(r"zoneinfo/(.+)$", target)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "UTC"


def host_keyboard(locale: str = "") -> str:
    for f, key in (("/etc/default/keyboard", "XKBLAYOUT"), ("/etc/vconsole.conf", "XKBLAYOUT"),
                   ("/etc/vconsole.conf", "KEYMAP")):
        try:
            text = Path(f).read_text()
        except OSError:
            continue
        m = re.search(rf'^{key}="?([A-Za-z0-9_-]+)', text, re.M)
        if m:
            return m.group(1).split(",")[0].split("-")[0].lower()
    loc = locale or host_locale()
    country = loc.split("_")[-1].lower() if "_" in loc else "us"
    return {"gb": "gb", "us": "us"}.get(country, country)


def _hostname_from(name: str, limit: int) -> str:
    h = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    h = re.sub(r"^(microsoft|the)-", "", h)[:limit].strip("-")
    if not h or h.isdigit():
        h = "rehome-vm"
    return h


# --- validation ----------------------------------------------------------------------------

_WIN_BAD_USER = set('"/\\[]:;|=,+*?<>@')


def validate(answers: Answers, method: str = "") -> None:
    """Check answers that do not depend on the VM (raises RehomeError)."""
    if not answers.password:
        raise RehomeError("An unattended installation needs a password for the new account.",
                          fix="Give a password (--password or --password-file).",
                          code="unattended-answers")
    if any(ord(c) < 32 or c == "\x7f" for c in answers.password):
        raise RehomeError("The password must not contain control characters or line breaks.",
                          code="unattended-answers")
    user = answers.user
    if user:
        if method == "windows":
            if len(user) > 20 or set(user) & _WIN_BAD_USER or user.strip(" .") != user:
                raise RehomeError(f"\"{user}\" cannot be a Windows user name.",
                                  fix="Use up to 20 letters or digits, without "
                                      "\" / \\ [ ] : ; | = , + * ? < > @.",
                                  code="unattended-answers")
            if user.lower() in ("administrator", "guest", "defaultaccount", "system"):
                raise RehomeError(f"\"{user}\" is a built-in Windows account.",
                                  fix="Choose another user name.", code="unattended-answers")
        elif method:
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user in ("root", "daemon",
                                                                                "bin", "sys"):
                raise RehomeError(f"\"{user}\" cannot be a Linux user name.",
                                  fix="Use lower-case letters, digits, - and _ (starting with a "
                                      "letter), at most 32 characters, and not \"root\".",
                                  code="unattended-answers")
    if answers.hostname and not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                                             answers.hostname):
        raise RehomeError(f"\"{answers.hostname}\" is not a valid computer name.",
                          fix="Use letters, digits and hyphens (not at the start or end).",
                          code="unattended-answers")
    if answers.product_key and not re.fullmatch(r"([A-Za-z0-9]{5}-){4}[A-Za-z0-9]{5}",
                                                answers.product_key.strip()):
        raise RehomeError("The product key must look like XXXXX-XXXXX-XXXXX-XXXXX-XXXXX.",
                          code="unattended-answers")
    if answers.timezone and not _valid_timezone(answers.timezone):
        raise RehomeError(f"\"{answers.timezone}\" is not a known time zone.",
                          fix="Use a name such as Europe/Berlin, America/New_York or UTC.",
                          code="unattended-answers")


def _valid_timezone(tz: str) -> bool:
    if tz in ("UTC", "Etc/UTC") or tz in WINDOWS_TIMEZONES or tz.endswith(" Standard Time"):
        return True
    if not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", tz):
        return False
    try:
        import zoneinfo
        zones = zoneinfo.available_timezones()
        return not zones or tz in zones
    except Exception:
        return True


def complete(answers: Answers, cfg: VMConfig, method: str) -> Answers:
    """Fill in defaults (user, locale, keyboard, time zone, host name) and validate."""
    a = replace(answers)
    if not a.user:
        login = ""
        try:
            login = getpass.getuser()
        except Exception:
            pass
        a.user = login if login and login != "root" else "user"
        if method != "windows":
            a.user = re.sub(r"[^a-z0-9_-]", "", a.user.lower()) or "user"
            if not re.match(r"[a-z_]", a.user):
                a.user = "u" + a.user
    loc = (a.locale or host_locale()).split(".")[0].replace("-", "_")
    if re.fullmatch(r"[a-z]{2,3}", loc):
        loc = f"{loc}_{loc.upper()}"
    if not re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}", loc):
        raise RehomeError(f"\"{a.locale}\" is not a locale Rehome understands.",
                          fix="Use the form en_US or de_DE.", code="unattended-answers")
    a.locale = loc
    a.keyboard = (a.keyboard or host_keyboard(loc)).lower()
    a.timezone = a.timezone or host_timezone()
    a.hostname = a.hostname or _hostname_from(cfg.name, 15 if method == "windows" else 63)
    if method == "windows" and len(a.hostname) > 15:
        raise RehomeError("Windows computer names can have at most 15 characters.",
                          fix="Choose a shorter host name.", code="unattended-answers")
    a.full_name = a.full_name or a.user
    a.product_key = a.product_key.strip().upper()
    validate(a, method)
    return a


# --- ISO helpers ------------------------------------------------------------------------------

def _xorriso() -> str:
    x = which("xorriso")
    if not x:
        raise RehomeError(
            "xorriso is not installed; Rehome needs it to read installer images and to "
            "build the answer disc.",
            fix="Install it: sudo apt install xorriso / sudo dnf install xorriso / "
                "sudo zypper install xorriso / sudo pacman -S libisoburn",
            code="missing-program")
    return x


def iso_files(iso: str) -> list[str]:
    """All file paths in an ISO (Rock Ridge names), without the leading slash."""
    cp = run_cmd([_xorriso(), "-indev", iso, "-find", "/", "-type", "f"],
                 what="Reading the installer image", timeout=300)
    out = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line.startswith("'"):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if parts:
            out.append(parts[0].lstrip("/"))
    return out


def iso_volume_id(iso: str) -> str:
    cp = run_cmd([_xorriso(), "-indev", iso, "-pvd_info"], check=False, timeout=120)
    m = re.search(r"^Volume Id\s*:\s*(.*?)\s*$", cp.stdout + "\n" + cp.stderr, re.M)
    return m.group(1) if m else ""


def iso_extract(iso: str, mapping: dict[str, Path]) -> None:
    """Extract ``{path in ISO: destination}``; destinations become user-writable."""
    if not mapping:
        return
    argv = [_xorriso(), "-osirrox", "on", "-indev", iso]
    for src, dst in mapping.items():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.unlink(missing_ok=True)
        argv += ["-extract", "/" + src.lstrip("/"), str(dst)]
    run_cmd(argv, what="Extracting the installer's kernel", timeout=600)
    for dst in mapping.values():
        os.chmod(dst, 0o600)


def iso_read(iso: str, path: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="rehome-iso-") as td:
        dst = Path(td) / "f"
        iso_extract(iso, {path: dst})
        return dst.read_bytes()


def make_iso(src_dir: Path, label: str, out: Path) -> None:
    """Build a small ISO 9660 + Joliet + Rock Ridge image from ``src_dir``."""
    out.unlink(missing_ok=True)
    tool = which("xorriso")
    if tool:
        argv = [tool, "-as", "mkisofs"]
    else:
        tool = which("genisoimage") or which("mkisofs")
        if not tool:
            _xorriso()                          # raises with install instructions
        argv = [tool]
    argv += ["-quiet", "-o", str(out), "-V", label, "-J", "-joliet-long", "-r",
             "-iso-level", "3", str(src_dir)]
    run_cmd(argv, what="Building the answer disc", timeout=300)
    os.chmod(out, 0o600)


# --- boot menu parsing ----------------------------------------------------------------------------

@dataclass
class BootEntry:
    title: str
    kernel: str
    initrd: str
    args: str


_SKIP_ENTRY = re.compile(r"rescue|check|test this|memtest|safe graphics|basic graphics|nomodeset|"
                         r"troubleshoot|expert|hd ?media|accessib|speech|dark|rd\.live\.check|"
                         r"\bfsck|firmware|uefi firmware|boot from|local|hdt", re.I)


def parse_grub(text: str) -> list[BootEntry]:
    entries: list[BootEntry] = []
    title = ""
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"menuentry\s+(?:--\S+\s+)*(['\"])(.*?)\1", line)
        if m:
            title = m.group(2)
            cur = {"title": title, "kernel": "", "initrd": "", "args": ""}
            continue
        if cur is None:
            continue
        m = re.match(r"(linux|linuxefi|linux16)\s+(\S+)\s*(.*)$", line)
        if m:
            cur["kernel"] = m.group(2).lstrip("/")
            cur["args"] = m.group(3).strip()
            continue
        m = re.match(r"(initrd|initrdefi|initrd16)\s+(\S+)", line)
        if m:
            cur["initrd"] = m.group(2).lstrip("/")
            continue
        if line.startswith("}"):
            if cur.get("kernel"):
                entries.append(BootEntry(**cur))
            cur = None
    return entries


def pick_boot_entry(entries: list[BootEntry], kernel: str = "", initrd: str = "") -> BootEntry | None:
    def ok(e: BootEntry) -> bool:
        return not _SKIP_ENTRY.search(e.title) and not _SKIP_ENTRY.search(e.args)
    pools = []
    if kernel and initrd:
        pools.append([e for e in entries if e.kernel == kernel and e.initrd == initrd])
    if kernel:
        pools.append([e for e in entries if e.kernel == kernel])
    pools.append(entries)
    for pool in pools:
        good = [e for e in pool if ok(e)]
        if good:
            installs = [e for e in good if re.search(r"install", e.title, re.I)]
            return (installs or good)[0]
    return None


GRUB_CONFIGS = ("boot/grub/grub.cfg", "EFI/BOOT/grub.cfg", "boot/grub2/grub.cfg",
                "EFI/boot/grub.cfg", "boot/x86_64/loader/grub.cfg")


def boot_entry(iso: str, files: list[str], kernel: str = "", initrd: str = "") -> BootEntry | None:
    fset = set(files)
    entries: list[BootEntry] = []
    for cfgfile in GRUB_CONFIGS:
        if cfgfile in fset:
            try:
                entries += parse_grub(iso_read(iso, cfgfile).decode("utf-8", "replace"))
            except RehomeError as e:
                log.debug("could not read %s: %s", cfgfile, e)
    return pick_boot_entry(entries, kernel, initrd)


def kernel_args(entry_args: str, extra: list[str]) -> str:
    """Insert ``extra`` before the ``---`` separator (installer-only arguments)."""
    tokens = entry_args.split()
    if "---" in tokens:
        i = tokens.index("---")
        before, after = tokens[:i], tokens[i + 1:]
        return " ".join(before + extra + ["---"] + after)
    return " ".join(tokens + extra)


# --- crypt(3) SHA-512 (no `crypt` module in Python 3.13) -------------------------------------

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SHA512_PERM = [(0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26),
                (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
                (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57), (37, 58, 16),
                (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41)]


def sha512_crypt(password: str, salt: str | None = None, rounds: int = 5000) -> str:
    """``$6$`` password hash as produced by ``crypt(3)``/``openssl passwd -6``."""
    if salt is None:
        salt = "".join(secrets.choice(_ITOA64) for _ in range(16))
    salt = salt[:16]
    p, s = password.encode(), salt.encode()
    b = hashlib.sha512(p + s + p).digest()
    a = hashlib.sha512(p + s)
    n = len(p)
    while n > 64:
        a.update(b)
        n -= 64
    a.update(b[:n])
    n = len(p)
    while n:
        a.update(b if n & 1 else p)
        n >>= 1
    a_digest = a.digest()
    dp = hashlib.sha512(p * len(p)).digest()
    p_bytes = (dp * (len(p) // 64 + 1))[:len(p)]
    ds = hashlib.sha512(s * (16 + a_digest[0])).digest()
    s_bytes = ds[:len(s)]
    c = a_digest
    for i in range(rounds):
        h = hashlib.sha512()
        h.update(p_bytes if i & 1 else c)
        if i % 3:
            h.update(s_bytes)
        if i % 7:
            h.update(p_bytes)
        h.update(c if i & 1 else p_bytes)
        c = h.digest()
    out = []
    for b2, b1, b0 in _SHA512_PERM:
        w = (c[b2] << 16) | (c[b1] << 8) | c[b0]
        for _ in range(4):
            out.append(_ITOA64[w & 0x3F])
            w >>= 6
    w = c[63]
    for _ in range(2):
        out.append(_ITOA64[w & 0x3F])
        w >>= 6
    prefix = "$6$" + (f"rounds={rounds}$" if rounds != 5000 else "")
    return f"{prefix}{salt}${''.join(out)}"


# --- initrd injection --------------------------------------------------------------------------

def cpio_newc(files: dict[str, bytes], mode: int = 0o100600) -> bytes:
    out = bytearray()

    def entry(ino: int, name: str, data: bytes, fmode: int) -> None:
        nameb = name.encode() + b"\0"
        fields = [ino, fmode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(nameb), 0]
        out.extend(b"070701" + "".join(f"{v:08X}" for v in fields).encode() + nameb)
        out.extend(b"\0" * (-len(out) % 4))
        out.extend(data)
        out.extend(b"\0" * (-len(out) % 4))

    for i, (name, data) in enumerate(files.items(), start=1):
        entry(i, name.lstrip("/"), data, mode)
    entry(0, "TRAILER!!!", b"", 0)
    return bytes(out)


def inject_initrd(initrd: Path, files: dict[str, bytes]) -> None:
    """Append a (gzip-compressed) cpio archive; the kernel unpacks all archives in order."""
    with open(initrd, "ab") as f:
        size = f.tell()
        f.write(b"\0" * (-size % 4))
        f.write(gzip.compress(cpio_newc(files), mtime=0))
    os.chmod(initrd, 0o600)


# --- planning --------------------------------------------------------------------------------

def _unsupported(what: str, fix: str = "") -> RehomeError:
    return RehomeError(what, fix=fix or "Create the VM without --unattended and install the "
                                        "operating system by hand.",
                       code="unattended-unsupported")


def _installer_cd(cfg: VMConfig) -> CdRom | None:
    cds = [c for c in cfg.cdroms if c.file and c.file != ANSWER_ISO]
    for c in sorted(cds, key=lambda c: (c.boot_index == 0, c.boot_index)):
        return c
    return None


def _ubuntu_version(vol: str) -> str:
    m = re.search(r"\b(\d\d\.\d\d)(?:\.\d+)*\b", vol)
    return m.group(1) if m else ""


def _is_subiquity(files: list[str], vol: str) -> bool:
    if "casper/vmlinuz" not in files:
        return False
    layered = any(f.startswith("casper/") and f.endswith(".squashfs") and
                  os.path.basename(f) != "filesystem.squashfs" for f in files)
    return layered or bool(re.match(r"Ubuntu-Server\b", vol))


def plan(cfg: VMConfig) -> _Plan:
    """Work out how ``cfg`` can be installed unattended (raises if it cannot)."""
    cd = _installer_cd(cfg)
    if cd is None:
        raise _unsupported("This VM has no installer disc, so there is nothing to install.",
                           fix="Create the VM from an installer ISO.")
    iso = str(cfg.resolve(cd.file))
    if not os.path.isfile(iso):
        raise RehomeError(f"The installer image {iso} is missing.",
                          fix="Insert the ISO again in the VM settings.")
    os_name = cfg.os.name or "This operating system"
    fam = cfg.os.family
    if fam == "windows" and cfg.os.distro != "reactos":
        build = int(cfg.os.build or 0) if str(cfg.os.build or "").isdigit() else 0
        if build and build < 6000:
            raise _unsupported(f"{os_name} uses an older answer-file format that Rehome does "
                               "not generate (only Windows Vista and later are supported).")
        langs = []
        try:
            from rehome.media import osinfo
            if osinfo.available():
                Lib = osinfo._lib()
                m = Lib.Media.create_from_location(iso, None)
                if osinfo._db().identify_media(m):
                    langs = list(m.get_languages() or [])
        except Exception as e:  # not identifiable: fine, defaults apply
            log.debug("osinfo media check failed: %s", e)
        vol = ""
        if not langs:
            vol = iso_volume_id(iso)
            mm = re.search(r"_([A-Za-z]{2}-[A-Za-z]{2})_", vol)
            if mm:
                langs = [mm.group(1)]
        return _Plan("windows", iso, cd.id, os_name=os_name, languages=langs, volume_id=vol)
    if fam != "linux":
        raise _unsupported(f"Rehome cannot install {os_name} automatically.")
    files = iso_files(iso)
    vol = iso_volume_id(iso)
    if _is_subiquity(files, vol):
        fset = set(files)
        initrd = "casper/initrd" if "casper/initrd" in fset else \
            next((f for f in files if f.startswith("casper/initrd")), "")
        server = "casper/ubuntu-server-minimal.squashfs" in fset or \
            bool(re.search(r"server", vol, re.I))
        version = _ubuntu_version(vol) or cfg.os.version
        from rehome.media.catalog import vkey
        if not server and vkey(version) < (24, 4):
            raise _unsupported(f"{os_name}: Ubuntu Desktop before 24.04 cannot install "
                               "automatically.", fix="Use Ubuntu Desktop 24.04 or later, or "
                                                     "Ubuntu Server.")
        if not initrd:
            raise _unsupported("The Ubuntu installer image has no initrd in /casper.")
        return _Plan("subiquity", iso, cd.id, os_name=os_name, files=files, volume_id=vol,
                     kernel="casper/vmlinuz", initrd=initrd,
                     variant="server" if server else "desktop", version=version)
    return _osinfo_plan(cfg, iso, cd.id, files, vol, os_name)


SCRIPT_FAMILIES = {
    # family: label of the answer CD (None = initrd injection), kernel-argument
    # rewrites (point the installer at the CD by label), and rewrites that make
    # the installer power off instead of rebooting into itself again.
    "kickstart": {"label": "OEMDRV", "aliases": ["ks.cfg"],
                  "cmdline": [(r"\b((?:inst\.)?ks)=hd:[^:\s]+:", r"\1=hd:LABEL=OEMDRV:")],
                  "poweroff": [(r"(?m)^reboot\b.*$", "poweroff")],
                  "poweroff_marker": r"(?m)^poweroff\b"},
    "autoyast": {"label": "OEMDRV", "aliases": [],
                 "cmdline": [(r"\bautoyast=device://[^/\s]+/", "autoyast=label://OEMDRV/")],
                 "poweroff": [(r"<final_reboot[^>]*>\s*true\s*</final_reboot>",
                               '<final_halt config:type="boolean">true</final_halt>')],
                 "poweroff_marker": r"<final_halt"},
    "preseed": {"label": None, "aliases": [], "cmdline": [],
                "append": "\n# Added by Rehome: power off when done (the VM would otherwise "
                          "boot the installer again)\n"
                          "d-i debian-installer/exit/poweroff boolean true\n",
                "poweroff_marker": r"debian-installer/exit/poweroff"},
}


def script_family(script) -> str:
    sid = script.get_id() or ""
    for fam in SCRIPT_FAMILIES:
        if fam in sid:
            return fam
    return ""


def _os_scripts(os_obj, Lib, depth: int = 6) -> list:
    """Install scripts of an OS, inherited through derives-from (win11 -> win10)."""
    seen = set()
    while os_obj is not None and depth > 0 and os_obj.get_id() not in seen:
        seen.add(os_obj.get_id())
        scripts = list(os_obj.get_install_script_list().get_elements())
        if scripts:
            return scripts
        rel = os_obj.get_related(Lib.ProductRelationship.DERIVES_FROM).get_elements()
        os_obj = rel[0] if rel else None
        depth -= 1
    return []


def _os_lookup(db, ident: str):
    if not ident:
        return None
    try:
        o = db.get_os(ident)
        if o:
            return o
    except Exception:
        pass
    for o in db.get_os_list().get_elements():
        if o.get_short_id() == ident or o.get_id() == ident:
            return o
    return None


def _osinfo_plan(cfg: VMConfig, iso: str, cd_id: str, files: list[str], vol: str,
                 os_name: str) -> _Plan:
    from rehome.media import osinfo
    if not osinfo.available():
        raise _unsupported(f"Automatic installation of {os_name} needs libosinfo, which is not "
                           "installed.", fix="Install libosinfo and its GObject introspection "
                                             "data (run `rehome doctor`).")
    Lib, db = osinfo._lib(), osinfo._db()
    media = None
    try:
        m = Lib.Media.create_from_location(iso, None)
        if db.identify_media(m):
            media = m
    except Exception as e:
        log.debug("osinfo media detection failed: %s", e)
    if media is not None:
        os_obj = media.get_os()
        os_name = os_obj.get_name() or os_name
        if not media.supports_installer_script():
            raise _unsupported(
                f"This {os_name} image cannot install automatically (it is a live or "
                "desktop image without support for answer files).",
                fix="Use the project's DVD, server or network-install image, or install by "
                    "hand (Quick Create: use Fedora Server instead of Fedora Workstation).")
        scripts = list(media.get_install_script_list().get_elements()) or \
            _os_scripts(os_obj, Lib)
        kernel, initrd = media.get_kernel_path() or "", media.get_initrd_path() or ""
    else:
        os_obj = _os_lookup(db, cfg.os.osinfo_id)
        guess = osinfo.guess_from_label(vol)
        ge = osinfo.by_short_id(guess) if guess else None
        if os_obj is None or ge is None or ge.distro != (os_obj.get_distro() or ""):
            raise _unsupported(f"Rehome does not recognise this installer image ({vol or iso}), "
                               "so it cannot prepare an automatic installation for it.",
                               fix="Update osinfo-db, or install the system by hand.")
        scripts = _os_scripts(os_obj, Lib)
        kernel = initrd = ""
    scripts = [s for s in scripts if script_family(s)]
    if not scripts:
        raise _unsupported(f"Rehome has no automatic-installation script for {os_name}.")
    fset = set(files)
    if kernel not in fset or (initrd and initrd not in fset):
        # libosinfo did not say, or matched a media entry of another
        # architecture: use what the disc's own boot menu boots.
        entry = boot_entry(iso, files)
        if entry:
            kernel, initrd = entry.kernel, entry.initrd
    if kernel not in fset or (initrd and initrd not in fset):
        raise _unsupported(f"Rehome could not find the installer kernel on the {os_name} image.")
    return _Plan("osinfo", iso, cd_id, os_name=os_name, files=files, volume_id=vol,
                 kernel=kernel, initrd=initrd, media=media, os_obj=os_obj, scripts=scripts)


def check(cfg: VMConfig) -> Support:
    """Can ``cfg`` be installed unattended? (Reads the installer ISO; no changes.)"""
    try:
        p = plan(cfg)
    except RehomeError as e:
        return Support(False, reason=e.message, fix=e.fix or "")
    what = {"windows": "a Windows answer file (autounattend.xml)",
            "subiquity": "Ubuntu autoinstall",
            "osinfo": "libosinfo's install script"}[p.method]
    return Support(True, method=p.method, reason=f"{p.os_name} will be installed with {what}.")


def check_iso(iso: str, os_id: str = "") -> Support:
    """Like :func:`check`, before a VM exists (for the New VM wizard)."""
    from rehome.media import osinfo
    from rehome.vm.config import OsInfo
    cfg = VMConfig(name="check")
    cfg.cdroms.append(CdRom(id="cd0", file=os.path.abspath(iso), boot_index=1))
    e = None
    if osinfo.available():
        e = (osinfo.by_short_id(os_id) or osinfo.KNOWN_EXTRA.get(os_id)) if os_id else \
            osinfo.identify_media(iso).os
    if e is None:
        return Support(False, reason="Rehome could not identify the operating system on this "
                                     "image.", fix="Choose the operating system explicitly.")
    f = e.facts()
    cfg.os = OsInfo(family=f.get("family", "other"), name=e.name, version=str(f.get("version", "")),
                    distro=f.get("distro", ""), osinfo_id=f.get("osinfo_id", ""),
                    build=str(f.get("build", "") or ""), edition=f.get("edition", ""))
    return check(cfg)


# --- Windows ----------------------------------------------------------------------------------

# IANA -> Windows time zone IDs (from CLDR windowsZones.xml; common zones only).
WINDOWS_TIMEZONES = {
    "UTC": "UTC", "Etc/UTC": "UTC", "Etc/GMT": "UTC",
    "Europe/London": "GMT Standard Time", "Europe/Dublin": "GMT Standard Time",
    "Europe/Lisbon": "GMT Standard Time", "Atlantic/Reykjavik": "Greenwich Standard Time",
    "Europe/Berlin": "W. Europe Standard Time", "Europe/Amsterdam": "W. Europe Standard Time",
    "Europe/Rome": "W. Europe Standard Time", "Europe/Stockholm": "W. Europe Standard Time",
    "Europe/Vienna": "W. Europe Standard Time", "Europe/Zurich": "W. Europe Standard Time",
    "Europe/Oslo": "W. Europe Standard Time", "Europe/Luxembourg": "W. Europe Standard Time",
    "Europe/Paris": "Romance Standard Time", "Europe/Brussels": "Romance Standard Time",
    "Europe/Madrid": "Romance Standard Time", "Europe/Copenhagen": "Romance Standard Time",
    "Europe/Warsaw": "Central European Standard Time",
    "Europe/Zagreb": "Central European Standard Time",
    "Europe/Prague": "Central Europe Standard Time", "Europe/Budapest": "Central Europe Standard Time",
    "Europe/Belgrade": "Central Europe Standard Time",
    "Europe/Bratislava": "Central Europe Standard Time",
    "Europe/Ljubljana": "Central Europe Standard Time",
    "Europe/Athens": "GTB Standard Time", "Europe/Bucharest": "GTB Standard Time",
    "Europe/Helsinki": "FLE Standard Time", "Europe/Kyiv": "FLE Standard Time",
    "Europe/Kiev": "FLE Standard Time", "Europe/Riga": "FLE Standard Time",
    "Europe/Sofia": "FLE Standard Time", "Europe/Tallinn": "FLE Standard Time",
    "Europe/Vilnius": "FLE Standard Time", "Europe/Istanbul": "Turkey Standard Time",
    "Europe/Moscow": "Russian Standard Time",
    "America/New_York": "Eastern Standard Time", "America/Toronto": "Eastern Standard Time",
    "America/Detroit": "Eastern Standard Time", "America/Chicago": "Central Standard Time",
    "America/Winnipeg": "Central Standard Time", "America/Denver": "Mountain Standard Time",
    "America/Edmonton": "Mountain Standard Time", "America/Phoenix": "US Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time", "America/Vancouver": "Pacific Standard Time",
    "America/Anchorage": "Alaskan Standard Time", "Pacific/Honolulu": "Hawaiian Standard Time",
    "America/Halifax": "Atlantic Standard Time", "America/St_Johns": "Newfoundland Standard Time",
    "America/Mexico_City": "Central Standard Time (Mexico)",
    "America/Sao_Paulo": "E. South America Standard Time",
    "America/Argentina/Buenos_Aires": "Argentina Standard Time",
    "America/Bogota": "SA Pacific Standard Time", "America/Lima": "SA Pacific Standard Time",
    "America/Santiago": "Pacific SA Standard Time", "America/Caracas": "Venezuela Standard Time",
    "Asia/Tokyo": "Tokyo Standard Time", "Asia/Seoul": "Korea Standard Time",
    "Asia/Shanghai": "China Standard Time", "Asia/Hong_Kong": "China Standard Time",
    "Asia/Taipei": "Taipei Standard Time", "Asia/Singapore": "Singapore Standard Time",
    "Asia/Kuala_Lumpur": "Singapore Standard Time", "Asia/Manila": "Singapore Standard Time",
    "Asia/Kolkata": "India Standard Time", "Asia/Calcutta": "India Standard Time",
    "Asia/Dubai": "Arabian Standard Time", "Asia/Jerusalem": "Israel Standard Time",
    "Asia/Bangkok": "SE Asia Standard Time", "Asia/Jakarta": "SE Asia Standard Time",
    "Asia/Ho_Chi_Minh": "SE Asia Standard Time", "Asia/Karachi": "Pakistan Standard Time",
    "Asia/Tehran": "Iran Standard Time", "Asia/Dhaka": "Bangladesh Standard Time",
    "Australia/Sydney": "AUS Eastern Standard Time",
    "Australia/Melbourne": "AUS Eastern Standard Time",
    "Australia/Brisbane": "E. Australia Standard Time",
    "Australia/Adelaide": "Cen. Australia Standard Time",
    "Australia/Perth": "W. Australia Standard Time",
    "Pacific/Auckland": "New Zealand Standard Time",
    "Africa/Johannesburg": "South Africa Standard Time", "Africa/Cairo": "Egypt Standard Time",
    "Africa/Lagos": "W. Central Africa Standard Time", "Africa/Nairobi": "E. Africa Standard Time",
    "Africa/Casablanca": "Morocco Standard Time",
}

_WIN_ARCH = {"x86_64": "amd64", "i386": "x86", "i686": "x86", "aarch64": "arm64"}


def _win_locale(loc: str) -> str:
    return loc.replace("_", "-")


def windows_timezone(tz: str) -> str:
    if tz in WINDOWS_TIMEZONES:
        return WINDOWS_TIMEZONES[tz]
    if tz.endswith(" Standard Time") or tz == "UTC":
        return tz
    return ""


def autounattend_xml(*, arch: str, uefi: bool, ui_language: str, locale: str, user: str,
                     full_name: str, password: str, hostname: str, timezone: str = "",
                     product_key: str = "", edition: str = "") -> str:
    """Windows Setup answer file (Windows Vista and later, incl. 10/11/Server)."""
    e = _xml_escape
    pa = _WIN_ARCH.get(arch, "amd64")
    comp = (f'processorArchitecture="{pa}" publicKeyToken="31bf3856ad364e35" '
            'language="neutral" versionScope="nonSxS"')
    win_tz = windows_timezone(timezone)
    if uefi:
        parts = """
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>300</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Label>System</Label><Format>FAT32</Format></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID><Label>Windows</Label><Letter>C</Letter><Format>NTFS</Format></ModifyPartition>
          </ModifyPartitions>"""
        install_part = 3
    else:
        parts = """
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type><Size>500</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Label>System Reserved</Label><Format>NTFS</Format><Active>true</Active></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID><Label>Windows</Label><Letter>C</Letter><Format>NTFS</Format></ModifyPartition>
          </ModifyPartitions>"""
        install_part = 2
    install_from = ""
    if edition:
        key = "/IMAGE/INDEX" if edition.isdigit() else "/IMAGE/NAME"
        install_from = f"""
            <InstallFrom>
              <MetaData wcm:action="add"><Key>{key}</Key><Value>{e(edition)}</Value></MetaData>
            </InstallFrom>"""
    product = ""
    if product_key:
        product = f"""
          <ProductKey><Key>{e(product_key)}</Key><WillShowUI>OnError</WillShowUI></ProductKey>"""
    tz_xml = f"\n      <TimeZone>{e(win_tz)}</TimeZone>" if win_tz else ""
    intl = (f"<InputLocale>{e(locale)}</InputLocale><SystemLocale>{e(locale)}</SystemLocale>"
            f"<UILanguage>{e(ui_language)}</UILanguage><UserLocale>{e(locale)}</UserLocale>")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Generated by Rehome. Contains the account password: keep it private. -->
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" {comp}>
      <SetupUILanguage><UILanguage>{e(ui_language)}</UILanguage></SetupUILanguage>
      {intl}
    </component>
    <component name="Microsoft-Windows-Setup" {comp}>
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>{parts}
        </Disk>
        <WillShowUI>OnError</WillShowUI>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>{install_from}
          <InstallTo><DiskID>0</DiskID><PartitionID>{install_part}</PartitionID></InstallTo>
          <WillShowUI>OnError</WillShowUI>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <FullName>{e(full_name)}</FullName>{product}
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {comp}>
      <ComputerName>{e(hostname)}</ComputerName>{tz_xml}
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {comp}>
      {intl}
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {comp}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword><Value>{e(password)}</Value><PlainText>true</PlainText></AdministratorPassword>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{e(user)}</Name>
            <DisplayName>{e(full_name)}</DisplayName>
            <Group>Administrators</Group>
            <Password><Value>{e(password)}</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>{tz_xml}
    </component>
  </settings>
</unattend>
"""


def _build_windows(cfg: VMConfig, p: _Plan, a: Answers) -> tuple[dict, str, DirectBoot | None, list[str], dict]:
    langs = [l.replace("_", "-") for l in p.languages if l]
    ui = langs[0] if langs else "en-US"
    edition = a.edition
    server = cfg.os.edition == "server"
    if not edition and server:
        edition = "2"          # Standard (Desktop Experience) on Server 2016-2025 media
    tz = windows_timezone(a.timezone)
    xml = autounattend_xml(arch=cfg.machine.arch, uefi=cfg.machine.firmware != "bios",
                           ui_language=ui, locale=_win_locale(a.locale), user=a.user,
                           full_name=a.full_name, password=a.password, hostname=a.hostname,
                           timezone=a.timezone, product_key=a.product_key, edition=edition)
    notes = [
        f"Windows Setup runs by itself and creates the administrator account \"{a.user}\" "
        "with the password you chose.",
        "Windows shows \"Press any key to boot from CD or DVD\" when the VM starts. Rehome "
        "presses a key for you when it starts the VM after creating it (--start); if you start "
        "it another way, click into the console and press a key within 5 seconds.",
    ]
    if server and not a.edition:
        notes.append("Installing Windows Server Standard with the Desktop Experience (image 2 "
                     "on the DVD); choose another with --edition.")
    if not a.product_key and not edition:
        notes.append("Evaluation media install without a product key. Retail media that holds "
                     "several editions makes Setup ask for a key and an edition (choose \"I don't "
                     "have a product key\"); give --product-key or --edition to avoid that.")
    if not tz:
        notes.append(f"Rehome does not know the Windows name of the time zone {a.timezone}; "
                     "set it in Windows after the installation.")
    if not langs:
        notes.append("Could not tell the language of this Windows image; using en-US for "
                     "Setup. If Setup reports a language error, give the image's language "
                     "with --locale.")
    state = {"press_boot_key": True}
    return {"autounattend.xml": xml.encode()}, "UNATTEND", None, notes, state


# --- Ubuntu autoinstall -------------------------------------------------------------------------

def _yaml_scalar(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict) and not v:
        return "{}"
    if isinstance(v, list) and not v:
        return "[]"
    return json.dumps(str(v))          # a JSON string is a valid YAML double-quoted scalar


def to_yaml(obj, indent: int = 0) -> str:
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict) and v:
                sub = to_yaml(v, indent + 1)
                lines.append(f"{pad}- {sub.lstrip()}")
            else:
                lines.append(f"{pad}- {_yaml_scalar(v)}")
    return "\n".join(lines)


def autoinstall_user_data(a: Answers, *, server: bool, version: str) -> str:
    from rehome.media.catalog import vkey
    ai: dict = {
        "version": 1,
        "locale": f"{a.locale}.UTF-8",
        "keyboard": {"layout": a.keyboard},
        "timezone": a.timezone,
        "identity": {"hostname": a.hostname, "realname": a.full_name, "username": a.user,
                     "password": sha512_crypt(a.password)},
        "storage": {"layout": {"name": "direct"}},
        "shutdown": "poweroff",
    }
    if vkey(version) >= (23, 4) or not version:
        # Install from the ISO alone when the archive cannot be reached.
        ai["apt"] = {"geoip": False, "fallback": "offline-install"}
    if server:
        ai["ssh"] = {"install-server": True, "allow-pw": True}
    return "#cloud-config\n# Generated by Rehome.\n" + to_yaml({"autoinstall": ai}) + "\n"


def _build_subiquity(cfg: VMConfig, p: _Plan, a: Answers, work: Path):
    server = p.variant == "server"
    user_data = autoinstall_user_data(a, server=server, version=p.version)
    meta = f"instance-id: rehome-{cfg.short_id}\nlocal-hostname: {a.hostname}\n"
    boot = _extract_boot(p, work)
    entry = boot_entry(p.iso, p.files, p.kernel, p.initrd)
    boot.append = kernel_args(entry.args if entry else "---", ["autoinstall", *SERIAL_CONSOLE])
    notes = [f"Ubuntu installs by itself and creates the user \"{a.user}\" (with sudo) with the "
             "password you chose.",
             "When the installation is finished the VM turns off. Start it again to use the "
             "installed system."]
    if server:
        notes.append("An SSH server is installed; password logins are allowed.")
    else:
        notes.append("Automatic installation of Ubuntu Desktop is newer than Ubuntu Server's; if "
                     "the installer stops with a question, answer it in the console.")
    return {"user-data": user_data.encode(), "meta-data": meta.encode()}, "cidata", boot, notes, {}


# --- libosinfo install scripts --------------------------------------------------------------

def _target_disk(cfg: VMConfig) -> str:
    bus = cfg.disks[0].bus if cfg.disks else "virtio"
    return {"virtio": "/dev/vda", "nvme": "/dev/nvme0n1"}.get(bus, "/dev/sda")


def _extract_boot(p: _Plan, work: Path) -> DirectBoot:
    bdir = work / "boot"
    bdir.mkdir(mode=0o700, exist_ok=True)
    mapping = {p.kernel: bdir / "vmlinuz"}
    if p.initrd:
        mapping[p.initrd] = bdir / "initrd"
    iso_extract(p.iso, mapping)
    return DirectBoot(kernel=f"{ANSWER_DIR}/boot/vmlinuz",
                      initrd=f"{ANSWER_DIR}/boot/initrd" if p.initrd else "", once=True)


def _pick_script(scripts: list, profile: str):
    order = [profile] if profile else ["desktop", "jeos"]
    for want in order:
        for s in scripts:
            if s.get_profile() == want:
                return s
    if profile:
        have = sorted({s.get_profile() for s in scripts})
        raise _unsupported(f"There is no \"{profile}\" install profile for this system "
                           f"(available: {', '.join(have)}).", fix="Choose another profile.")
    return scripts[0]


def _build_osinfo(cfg: VMConfig, p: _Plan, a: Answers, work: Path):
    from rehome.media import osinfo
    Lib = osinfo._lib()
    if "'" in a.password:
        raise RehomeError("The password must not contain a single quote (') for this "
                          "operating system's installer.", fix="Choose another password.",
                          code="unattended-answers")
    script = _pick_script(p.scripts, a.profile)
    fam = script_family(script)
    spec = SCRIPT_FAMILIES[fam]
    c = Lib.InstallConfig()
    c.set_user_login(a.user)
    c.set_user_password(a.password)
    c.set_admin_password(a.password)
    c.set_user_realname(a.full_name)
    c.set_hostname(a.hostname)
    c.set_l10n_language(a.locale)
    c.set_l10n_keyboard(a.keyboard)
    c.set_l10n_timezone(a.timezone)
    c.set_hardware_arch(cfg.machine.arch)
    c.set_target_disk(_target_disk(cfg))
    methods = script.get_injection_methods()
    M = Lib.InstallScriptInjectionMethod
    if spec["label"] and methods & (M.CDROM | M.DISK):
        inj = M.CDROM if methods & M.CDROM else M.DISK
    elif methods & M.INITRD:
        inj = M.INITRD
    else:
        raise _unsupported(f"Rehome cannot pass the install script to the {p.os_name} installer.")
    script.set_preferred_injection_method(inj)
    script.set_installation_source(Lib.InstallScriptInstallationSource.MEDIA)
    try:
        if p.media is not None:
            text = script.generate_for_media(p.media, c, None)
            cmdline = script.generate_command_line_for_media(p.media, c) or ""
        else:
            text = script.generate(p.os_obj, c, None)
            cmdline = script.generate_command_line(p.os_obj, c) or ""
    except Exception as e:     # GLib.Error; never include the config (it has the password)
        raise RehomeError("libosinfo could not generate the install script.",
                          details=f"{script.get_id()}: {getattr(e, 'message', type(e).__name__)}")
    for rx, repl in spec.get("poweroff", []):
        text = re.sub(rx, repl, text)
    text += spec.get("append", "")
    if not re.search(spec["poweroff_marker"], text):
        log.warning("install script %s: could not make the installer power off at the end",
                    script.get_id())
    for rx, repl in spec["cmdline"]:
        cmdline = re.sub(rx, repl, cmdline)
    name = script.get_expected_filename()
    files = {name: text.encode()}
    boot = _extract_boot(p, work)
    label = spec["label"]
    if inj == M.INITRD:
        inject_initrd(work / "boot" / "initrd", files)
        files = {}
        label = None
    else:
        for alias in spec["aliases"]:
            files.setdefault(alias, text.encode())
    entry = boot_entry(p.iso, p.files, p.kernel, p.initrd)
    boot.append = kernel_args(entry.args if entry else "", cmdline.split() + SERIAL_CONSOLE)
    notes = [f"{p.os_name} installs by itself using libosinfo's \"{script.get_profile()}\" "
             f"profile and creates the user \"{a.user}\" with the password you chose (the same "
             "password is set for root/the administrator).",
             "When the installation is finished the VM turns off. Start it again to use the "
             "installed system."]
    if script.get_needs_internet():
        notes.append("The installer downloads packages from the internet, so the VM needs "
                     "network access.")
    return files, label, boot, notes, {"script": script.get_id(),
                                       "injection": "initrd" if inj == M.INITRD else "cdrom"}


# --- prepare / after start / cleanup -------------------------------------------------------------

def _workdir(cfg: VMConfig) -> Path:
    work = cfg.dir / ANSWER_DIR
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(mode=0o700)
    os.chmod(work, 0o700)
    return work


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _apply(c: VMConfig, installer_cd: str, boot: DirectBoot | None, with_cd: bool) -> None:
    from rehome.vm.hardware import add_cdrom
    c.cdroms = [cd for cd in c.cdroms if cd.file != ANSWER_ISO]
    inst = next((cd for cd in c.cdroms if cd.id == installer_cd), None)
    if with_cd:
        add_cdrom(c, ANSWER_ISO, boot_index=0, bus=inst.bus if inst else None)
    if boot is not None:
        c.direct_boot = boot
        # The installer is started through the kernel; afterwards the disk
        # must boot first. The installer disc stays as a fallback boot device.
        for dev in [*c.disks, *c.cdroms]:
            dev.boot_index = 0
        n = 1
        if c.disks:
            c.disks[0].boot_index = n
            n += 1
        if inst:
            inst.boot_index = n
    elif c.direct_boot and c.direct_boot.kernel.startswith(ANSWER_DIR + "/"):
        c.direct_boot = None


def _save(cfg: VMConfig, fn: Callable[[VMConfig], None]) -> None:
    from rehome.vm import library
    with library.locked(cfg):
        fresh = library.reload(cfg) if (cfg.dir / library.CONFIG_NAME).exists() else cfg
        fn(fresh)
        library.save(fresh)
    if fresh is not cfg:
        fn(cfg)


def _state_path(cfg: VMConfig) -> Path:
    return cfg.dir / ANSWER_DIR / STATE_FILE


def read_state(cfg: VMConfig) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def _write_state(cfg: VMConfig, state: dict) -> None:
    _write_private(_state_path(cfg), json.dumps(state, indent=2).encode())


def prepare(cfg: VMConfig, answers: Answers) -> list[str]:
    """Generate the answer media for ``cfg``, attach it and save the VM.

    Returns plain-English notes for the user. Raises :class:`RehomeError`
    (code ``unattended-unsupported`` or ``unattended-answers``) when the
    system cannot be installed unattended or an answer is invalid.
    """
    from rehome.vm import runner
    if runner.is_running(cfg):
        raise RehomeError(f"\"{cfg.name}\" is running.",
                          fix="Shut the VM down before preparing an automatic installation.")
    p = plan(cfg)
    a = complete(answers, cfg, p.method)
    work = _workdir(cfg)
    try:
        if p.method == "windows":
            files, label, boot, notes, extra = _build_windows(cfg, p, a)
        elif p.method == "subiquity":
            files, label, boot, notes, extra = _build_subiquity(cfg, p, a, work)
        else:
            files, label, boot, notes, extra = _build_osinfo(cfg, p, a, work)
        if label:
            media = work / "media"
            media.mkdir(mode=0o700)
            for name, data in files.items():
                _write_private(media / name, data)
            make_iso(media, label, cfg.dir / ANSWER_ISO)
        state = {"method": p.method, "installer_cd": p.installer_cd, "label": label or "",
                 "answer_iso": ANSWER_ISO if label else "", "press_boot_key": False,
                 "boot_key_pressed": False, "prepared": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 **extra}
        _write_state(cfg, state)
        _save(cfg, lambda c: _apply(c, p.installer_cd, boot, bool(label)))
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    log.info("prepared unattended install of %s (%s)", cfg.name, p.method)
    return notes


def press_boot_key(cfg: VMConfig, duration: float | None = None, interval: float = 1.0,
                   cancel: Callable[[], bool] | None = None, key: str = "spc") -> int:
    """Keep pressing a key while the VM's firmware may show "Press any key to boot from CD".

    Returns the number of key presses sent. ``duration`` defaults to 30 s with
    KVM and 150 s without (firmware is much slower under emulation).
    """
    from rehome.vm import runner
    if duration is None:
        accel = ""
        try:
            accel = json.loads((runner.runtime(cfg).run / "state.json").read_text()).get("accel", "")
        except (OSError, ValueError):
            pass
        duration = 30.0 if accel == "kvm" else 150.0
    sent = 0
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if cancel and cancel():
            raise Cancelled()
        if not runner.is_running(cfg):
            break
        try:
            runner.send_keys(cfg, [key])
            sent += 1
        except Exception as e:   # QMP busy or the VM is just starting
            log.debug("send-key failed: %s", e)
        time.sleep(interval)
    return sent


def after_start(cfg: VMConfig, cancel: Callable[[], bool] | None = None,
                status: Callable[[str], None] | None = None) -> list[str]:
    """Call right after starting a VM prepared by :func:`prepare` (no-op otherwise).

    For Windows this presses a key at the "Press any key to boot from CD or
    DVD" prompt, once, at the first start after preparing.
    """
    st = read_state(cfg)
    if not st.get("press_boot_key") or st.get("boot_key_pressed"):
        return []
    if status:
        status("Pressing a key so the VM starts Windows Setup from the DVD")
    n = press_boot_key(cfg, cancel=cancel)
    st["boot_key_pressed"] = True
    _write_state(cfg, st)
    if n == 0:
        return ["Rehome could not press a key in the VM. If it shows \"Press any key to boot "
                "from CD or DVD\", press a key in the console, or restart the VM."]
    return ["Rehome pressed a key so the VM boots Windows Setup from the DVD."]


def pending(cfg: VMConfig) -> bool:
    """True if an unattended install was prepared and its files are still in place."""
    return bool(read_state(cfg))


def cleanup(cfg: VMConfig) -> None:
    """Remove the answer disc and generated files (after the installation finished)."""
    from rehome.vm import runner
    if runner.is_running(cfg):
        raise RehomeError(f"\"{cfg.name}\" is running.", fix="Shut the VM down first.")

    def fn(c: VMConfig) -> None:
        c.cdroms = [cd for cd in c.cdroms if cd.file != ANSWER_ISO]
        if c.direct_boot and c.direct_boot.kernel.startswith(ANSWER_DIR + "/"):
            c.direct_boot = None
    _save(cfg, fn)
    shutil.rmtree(cfg.dir / ANSWER_DIR, ignore_errors=True)
