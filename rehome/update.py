"""Opt-in check for new Rehome releases.

Off by default. When enabled (``update_check = true`` in
``~/.config/rehome/settings.toml``, set with ``rehome update-check --enable``),
:func:`maybe_check` makes at most one anonymous request per day to GitHub's
"latest release" API. The request carries no identifiers: no cookies, no
machine or user IDs, only ``User-Agent: rehome/<version>``. Nothing is sent
anywhere else and nothing is installed automatically.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from rehome import __version__, paths
from rehome.util import toml

REPO = "oreo1298/Disk-to-VM-Manager"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
INSTALL_SH_URL = f"https://raw.githubusercontent.com/{REPO}/main/install.sh"
SETTINGS_FILE = "settings.toml"
SETTING_KEY = "update_check"
CHECK_INTERVAL = 24 * 3600
STAMP_FILE = "update-check.json"
MANIFEST_NAME = "install-manifest.json"


# --- settings ---------------------------------------------------------------------

def settings_path() -> Path:
    return paths.config_dir() / SETTINGS_FILE


def load_settings() -> dict:
    try:
        return toml.load(settings_path())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A broken settings file must not break the app; treat as defaults.
        return {}


def is_enabled() -> bool:
    return load_settings().get(SETTING_KEY) is True


def set_enabled(enabled: bool) -> None:
    """Persist the opt-in, keeping every other setting in the file."""
    data = load_settings()
    data[SETTING_KEY] = bool(enabled)
    toml.dump_atomic(data, settings_path())


# --- versions -----------------------------------------------------------------------

_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(.*)$")


def parse_version(text: str) -> tuple:
    """Sortable key for versions like ``0.2.0``, ``v1.4``, ``1.0.0rc1``, ``1.0.0-beta.2``.

    A release sorts after its pre-releases (``1.0.0rc1 < 1.0.0``).
    """
    m = _VERSION_RE.match(text.strip())
    if not m:
        return ((0, 0, 0, 0), 0, text.strip())
    nums = [int(x) for x in m.group(1).split(".")]
    nums = (nums + [0, 0, 0, 0])[:max(4, len(nums))]
    pre = m.group(2).lstrip(".-+_")
    return (tuple(nums), 0 if pre else 1, pre)


def compare_versions(a: str, b: str) -> int:
    ka, kb = parse_version(a), parse_version(b)
    return (ka > kb) - (ka < kb)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return compare_versions(candidate, current) > 0


# --- check ----------------------------------------------------------------------------

@dataclass
class UpdateResult:
    current: str
    checked: bool = False          # False when the check is off (nothing was sent)
    latest: str | None = None
    available: bool = False
    url: str | None = None         # release page
    error: str | None = None       # network/parse problem (never raised)

    def to_dict(self) -> dict:
        return asdict(self)


def _fetch_latest(timeout: float, opener) -> dict:
    req = urllib.request.Request(RELEASES_API, headers={
        "User-Agent": f"rehome/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check(force: bool = False, timeout: float = 5.0, opener=None) -> UpdateResult:
    """Ask GitHub for the latest release.

    Does nothing (``checked=False``) unless the user opted in or *force* is
    set, which is how an explicit ``rehome update-check`` counts as consent.
    Network and parse errors are reported in ``error``, never raised.
    """
    result = UpdateResult(current=__version__)
    if not force and not is_enabled():
        return result
    opener = opener or urllib.request.urlopen
    result.checked = True
    try:
        data = _fetch_latest(timeout, opener)
    except urllib.error.HTTPError as e:
        result.error = "No release has been published yet." if e.code == 404 \
            else f"GitHub answered HTTP {e.code}."
        return result
    except (urllib.error.URLError, OSError, ValueError) as e:
        result.error = f"Could not reach GitHub: {getattr(e, 'reason', e)}"
        return result
    tag = (data.get("tag_name") or "").strip() if isinstance(data, dict) else ""
    if not tag:
        result.error = "The release information had no version."
        return result
    result.latest = tag.lstrip("vV")
    result.url = data.get("html_url") or RELEASES_PAGE
    result.available = is_newer(result.latest)
    _write_stamp(result)
    return result


def _stamp_path() -> Path:
    return paths.cache_dir() / STAMP_FILE


def _write_stamp(result: UpdateResult) -> None:
    try:
        p = _stamp_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"time": time.time(), "latest": result.latest,
                                 "url": result.url}))
    except OSError:
        pass


def maybe_check(now: float | None = None, opener=None) -> UpdateResult | None:
    """For the GUI: check at most once a day, and only when opted in.

    Returns None when no check was due (or the user did not opt in).
    """
    if not is_enabled():
        return None
    now = time.time() if now is None else now
    try:
        last = json.loads(_stamp_path().read_text()).get("time", 0)
    except (OSError, ValueError, AttributeError):
        last = 0
    if isinstance(last, (int, float)) and 0 <= now - last < CHECK_INTERVAL:
        return None
    return check(opener=opener)


# --- how Rehome was installed -------------------------------------------------------------

@dataclass
class InstallMethod:
    kind: str                 # "install.sh", "git", "package"
    detail: str = ""
    update_commands: tuple[str, ...] = ()


def find_manifest() -> Path | None:
    candidates = [paths.data_files_dir() / MANIFEST_NAME,
                  Path("/usr/local/share/rehome") / MANIFEST_NAME]
    for c in candidates:
        if c.is_file():
            return c
    return None


def install_method() -> InstallMethod:
    root = paths.source_root()
    if (root / ".git").exists():
        return InstallMethod("git", str(root), (f"git -C {root} pull",))
    manifest = find_manifest()
    if manifest is not None:
        installer = manifest.parent / "installer" / "install.sh"
        cmds = [f"curl -fsSL {INSTALL_SH_URL} | bash -s -- --update"]
        if installer.is_file():
            cmds.append(f"{installer} --update")
        return InstallMethod("install.sh", str(manifest), tuple(cmds))
    from rehome.host.system import detect_os_release
    family = detect_os_release().family
    cmds = {
        "arch": ("Update the AUR package, e.g. `yay -Syu rehome` or `paru -Syu rehome`",),
        "debian": ("Download the new rehome_*_all.deb from the release page, then "
                   "`sudo apt install ./rehome_<version>_all.deb`",),
        "fedora": ("`sudo dnf upgrade rehome` (if installed from a repository), or download "
                   "the new .rpm and run `sudo dnf install ./rehome-<version>.noarch.rpm`",),
        "suse": ("`sudo zypper update rehome` (if installed from a repository), or download "
                 "the new .rpm and run `sudo zypper install ./rehome-<version>.noarch.rpm`",),
    }.get(family, ("Update Rehome with the tool you installed it with.",))
    return InstallMethod("package", family, cmds)
