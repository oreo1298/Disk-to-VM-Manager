#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Rehome installer.
#
#   curl -fsSL https://raw.githubusercontent.com/oreo1298/Disk-to-VM-Manager/main/install.sh | bash
#   ./install.sh --help                  (from a git checkout)
#
# Installs the packages Rehome needs with your distribution's package manager,
# installs Rehome under /usr/local, and writes a manifest of every change so
# that --uninstall can reverse exactly those changes. Every command that needs
# root is printed, with the reason, before it runs.

set -euo pipefail

# Captured at top level: empty when the script is piped into bash.
SCRIPT_PATH="${BASH_SOURCE[0]:-}"

REPO="oreo1298/Disk-to-VM-Manager"
RAW_URL="https://raw.githubusercontent.com/${REPO}/main/install.sh"
RELEASES_API="${REHOME_RELEASES_API:-https://api.github.com/repos/${REPO}/releases/latest}"
MAIN_TARBALL="${REHOME_MAIN_TARBALL:-https://codeload.github.com/${REPO}/tar.gz/refs/heads/main}"

# Options.
MODE="install"            # install | update | uninstall | purge | detect
PREFIX="/usr/local"
ASSUME_YES=0
DRY_RUN=0
SKIP_DEPS=0
KEEP_DEPS=0
OPTIONAL="ask"            # ask | all | none
KVM_GROUP=1
KERNEL_FIX=1
FORCE=0
SOURCE_OVERRIDE="${REHOME_SOURCE:-}"

# State.
TMP=""
SUDO=()
NO_ROOT_TOOL=0
HAVE_TTY=0
CMD_STDIN=/dev/null
PY=""
PY_TOO_OLD=""
SRC=""
SRC_DESC=""
SRC_VERSION=""
OS_ID="" OS_LIKE="" OS_NAME="" FAMILY="" PM=""
TARGET_USER=""
APT_UPDATED=0
PRIV_ASK=1
PRIV_DECLINED=0
ADDED_PKGS=()
FAILED_PKGS=()
GROUPS_ADDED=()           # "user:group"
OVERRIDES_ADDED=()        # "path|previous mode|owner|group"
MANIFEST_WRITTEN=0

# --- output ------------------------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
    C_B=$'\e[1m' C_DIM=$'\e[2m' C_RED=$'\e[31;1m' C_YEL=$'\e[33;1m' C_GRN=$'\e[32;1m' C_BLU=$'\e[34;1m' C_0=$'\e[0m'
else
    C_B="" C_DIM="" C_RED="" C_YEL="" C_GRN="" C_BLU="" C_0=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$C_BLU" "$C_0" "$C_B" "$*" "$C_0"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✔%s %s\n' "$C_GRN" "$C_0" "$*"; }
warn() { printf '%sWarning:%s %s\n' "$C_YEL" "$C_0" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$C_RED" "$C_0" "$*" >&2; exit 1; }

show_cmd() {
    local out="" a
    for a in "$@"; do
        if [[ "$a" =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then out+="$a "; else out+="$(printf '%q' "$a") "; fi
    done
    printf '%s' "${out% }"
}

usage() {
    cat <<EOF
Rehome installer

Usage:
  curl -fsSL ${RAW_URL} | bash [-s -- OPTIONS]
  ./install.sh [OPTIONS]

Modes (default: install, or reinstall over an existing installation):
  --update            Fetch the latest Rehome and reinstall it; your VMs and settings are
                      kept. Says "already up to date" when nothing is newer.
  --uninstall         Reverse every change recorded in the install manifest: Rehome's
                      files, its polkit policy, the packages the installer added (you are
                      asked first), the kvm group membership and the kernel permission
                      override. Your VMs and settings are kept.
  --purge             Like --uninstall, and also delete your Rehome VMs, settings and
                      cache (~/.local/share/rehome, ~/.config/rehome, ~/.cache/rehome,
                      ~/.local/state/rehome) after you type "delete" (or with --yes).
  --detect            Print the detected distribution and package manager, then exit.

Options:
  -y, --yes           Do not ask: accept every confirmation (for scripts).
  -n, --dry-run       Only print what would be done. Nothing is installed or changed
                      (the source may still be downloaded to a temporary directory).
      --with-optional Also install the optional components (ARM guests, virt-v2v,
                      shared folders, fast NAT, ddrescue, E01 images).
      --no-optional   Do not install or offer optional components.
      --skip-deps     Do not install distribution packages; only Rehome's own files.
      --keep-deps     With --uninstall/--purge: keep the packages the installer added.
      --no-kvm-group  Do not offer to add you to the kvm group.
      --no-kernel-fix Do not offer to make the kernel image readable (Debian/Ubuntu).
      --prefix DIR    Install under DIR (default /usr/local).
      --source SRC    Install from SRC instead of the latest release: a source
                      directory, a .tar.gz file or an http(s)/file URL of one
                      (same as REHOME_SOURCE=SRC).
      --force         With --update: reinstall even if the version is the same or older.
  -h, --help          Show this help.

What it does:
  1. Detects the distribution from /etc/os-release (Arch, Debian/Ubuntu, Fedora and
     openSUSE families) and installs Python 3 if it is missing.
  2. Gets the source: the git checkout it runs from, \$REHOME_SOURCE, or the latest
     GitHub release (its SHA-256 is verified when the release publishes one), falling
     back to the main branch if there is no release yet.
  3. Installs the required packages listed in data/packages.toml (and optional ones if
     you choose), recording which ones were not installed before.
  4. Offers to add you to the kvm group, and on Debian/Ubuntu to make the running
     kernel readable for libguestfs (dpkg-statoverride), recording both.
  5. Installs Rehome with packaging/layout.py under the prefix, the polkit policy in
     /usr/share/polkit-1/actions, and writes the manifest
     PREFIX/share/rehome/install-manifest.json.
  6. Runs "rehome doctor --quick".

Every command that needs root is shown with its reason before it runs, and you are
asked to confirm it (the default answer is yes) unless you pass --yes.

Environment:
  REHOME_SOURCE        Same as --source.
  REHOME_RELEASES_API  Release metadata URL (default: GitHub's releases/latest API).
  NO_COLOR             Disable colours.
EOF
}

# --- prompts and privileged commands ----------------------------------------------

detect_tty() {
    if [[ -r /dev/tty ]] && { : </dev/tty; } 2>/dev/null; then
        HAVE_TTY=1
        CMD_STDIN=/dev/tty
    fi
}

# confirm QUESTION [Y|N]: default answer Y unless N is given.
confirm() {
    local q="$1" def="${2:-Y}" ans hint="[Y/n]"
    [[ "$def" == "N" ]] && hint="[y/N]"
    if ((ASSUME_YES)); then
        printf '    %s %s yes (--yes)\n' "$q" "$hint"
        return 0
    fi
    if ((!HAVE_TTY)); then
        die "Cannot ask \"$q\" without a terminal. Re-run with --yes to accept, or with --dry-run to only see what would be done."
    fi
    while true; do
        printf '    %s %s ' "$q" "$hint" >/dev/tty
        if ! IFS= read -r ans </dev/tty; then   # end of input is not consent
            printf '\n' >/dev/tty
            die "No answer; stopping."
        fi
        case "${ans,,}" in
            "") [[ "$def" == "Y" ]] && return 0 || return 1 ;;
            y|yes) return 0 ;;
            n|no) return 1 ;;
        esac
    done
}

# priv REASON CMD...: show the command and why, ask, then run it as root.
# Returns the command's status; sets PRIV_DECLINED=1 when the user says no.
priv() {
    local reason="$1"
    shift
    PRIV_DECLINED=0
    printf '\n    %sNeeds root:%s %s\n' "$C_YEL" "$C_0" "$reason"
    printf '    %s$ %s%s\n' "$C_B" "$(show_cmd "${SUDO[@]}" "$@")" "$C_0"
    if ((DRY_RUN)); then
        printf '    %s(dry run: not running it)%s\n' "$C_DIM" "$C_0"
        return 0
    fi
    if ((PRIV_ASK)) && ! confirm "Run this command?"; then
        PRIV_DECLINED=1
        info "Skipped."
        return 1
    fi
    if ((NO_ROOT_TOOL)); then
        die "This step needs root, but sudo is not installed. Run the installer as root, or install sudo."
    fi
    local rc=0
    "${SUDO[@]}" "$@" <"$CMD_STDIN" || rc=$?
    return "$rc"
}

setup_root_tool() {
    if ((EUID == 0)); then
        SUDO=()
    elif command -v sudo >/dev/null 2>&1; then
        SUDO=(sudo)
    else
        SUDO=(sudo)
        NO_ROOT_TOOL=1
    fi
    if ((EUID == 0)); then
        if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
            TARGET_USER="$SUDO_USER"
        else
            TARGET_USER="root"
        fi
    else
        TARGET_USER="${USER:-$(id -un)}"
    fi
}

# --- distribution ------------------------------------------------------------------

os_field() {
    local key="$1" file="$2" line val
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == "$key="* ]] || continue
        val="${line#*=}"
        val="${val%\"}" val="${val#\"}" val="${val%\'}" val="${val#\'}"
        printf '%s' "$val"
        return 0
    done <"$file"
}

family_of() {
    case "$1" in
        arch|archarm|manjaro|manjaro-arm|endeavouros|garuda|artix|cachyos|arcolinux) echo arch ;;
        debian|ubuntu|linuxmint|pop|elementary|zorin|kali|raspbian|neon|devuan|mx|deepin|parrot) echo debian ;;
        fedora|rhel|centos|rocky|almalinux|nobara|ultramarine) echo fedora ;;
        suse|opensuse|opensuse-*|sles|sled|sle-micro) echo suse ;;
        *) echo "" ;;
    esac
}

detect_distro() {
    local f="${REHOME_OS_RELEASE:-}" id
    if [[ -z "$f" ]]; then
        for f in /etc/os-release /usr/lib/os-release; do [[ -r "$f" ]] && break; done
    fi
    if [[ -r "$f" ]]; then
        OS_ID="$(os_field ID "$f")"
        OS_LIKE="$(os_field ID_LIKE "$f")"
        OS_NAME="$(os_field PRETTY_NAME "$f")"
    fi
    OS_ID="${OS_ID,,}" OS_LIKE="${OS_LIKE,,}"
    OS_NAME="${OS_NAME:-${OS_ID:-unknown Linux}}"
    FAMILY=""
    for id in $OS_ID $OS_LIKE; do
        FAMILY="$(family_of "$id")"
        [[ -n "$FAMILY" ]] && break
    done
    PM=""
    case "$FAMILY" in
        arch) command -v pacman >/dev/null 2>&1 && PM=pacman ;;
        debian) command -v apt-get >/dev/null 2>&1 && PM=apt ;;
        fedora) if command -v dnf >/dev/null 2>&1; then PM=dnf; elif command -v dnf5 >/dev/null 2>&1; then PM=dnf5; fi ;;
        suse) command -v zypper >/dev/null 2>&1 && PM=zypper ;;
    esac
    return 0
}

python_package() {
    case "$FAMILY" in arch) echo python ;; *) echo python3 ;; esac
}

# The exact install / query commands per package manager.
pm_install_cmd() {
    case "$PM" in
        pacman) printf '%s\n' pacman -S --needed --noconfirm ;;
        apt)
            if ((HAVE_TTY)); then printf '%s\n' apt-get install -y
            else printf '%s\n' env DEBIAN_FRONTEND=noninteractive apt-get install -y; fi ;;
        dnf|dnf5) printf '%s\n' "$PM" install -y ;;
        zypper) printf '%s\n' zypper --non-interactive install ;;
    esac
}

pkg_installed() {
    case "$FAMILY" in
        debian) [[ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null)" == "installed" ]] ;;
        arch) pacman -T "$1" >/dev/null 2>&1 ;;
        fedora|suse) rpm -q --whatprovides "$1" >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

apt_update_once() {
    ((APT_UPDATED)) && return 0
    APT_UPDATED=1
    local cmd=(apt-get update)
    ((HAVE_TTY)) || cmd=(env DEBIAN_FRONTEND=noninteractive apt-get update)
    if ! priv "Refresh the package lists so apt finds the current package versions" "${cmd[@]}"; then
        ((PRIV_DECLINED)) || warn "apt-get update failed; trying to install with the existing package lists."
    fi
    return 0
}

# install_packages REASON PKG...: install the ones not installed yet, record them.
install_packages() {
    local reason="$1" p
    shift
    local -a missing=() cmd=()
    for p in "$@"; do pkg_installed "$p" || missing+=("$p"); done
    if ((${#missing[@]} == 0)); then
        ok "Already installed: $*"
        return 0
    fi
    if (($# > ${#missing[@]})); then
        info "Already installed: $(($# - ${#missing[@]})) of $# packages; ${#missing[@]} to install."
    fi
    [[ "$PM" == "apt" ]] && apt_update_once
    mapfile -t cmd < <(pm_install_cmd)
    if priv "$reason" "${cmd[@]}" "${missing[@]}"; then
        ((DRY_RUN)) && return 0
        record_installed "${missing[@]}"
        return 0
    fi
    if ((PRIV_DECLINED)); then
        FAILED_PKGS+=("${missing[@]}")
        warn "Skipped installing: ${missing[*]}"
        return 0
    fi
    if ((${#missing[@]} == 1)); then
        record_installed "${missing[@]}"
        return 0
    fi
    warn "Installing all ${#missing[@]} packages at once failed (usually one name is unknown to this release). Trying them one at a time to find out which."
    local saved_ask=$PRIV_ASK
    PRIV_ASK=0
    for p in "${missing[@]}"; do
        pkg_installed "$p" && { record_installed "$p"; continue; }
        priv "Install $p (one at a time after the combined install failed)" "${cmd[@]}" "$p" || true
        record_installed "$p"
    done
    PRIV_ASK=$saved_ask
}

record_installed() {
    local p
    for p in "$@"; do
        if pkg_installed "$p"; then
            ADDED_PKGS+=("$p")
        else
            FAILED_PKGS+=("$p")
        fi
    done
}

# --- python -------------------------------------------------------------------------

find_python() {
    local cand ver
    PY="" PY_TOO_OLD=""
    for cand in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
        [[ -n "$cand" && -x "$cand" ]] || continue
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PY="$cand"
            return 0
        fi
        ver="$("$cand" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo "?")"
        PY_TOO_OLD="$cand ($ver)"
    done
    return 1
}

ensure_python() {
    find_python && return 0
    if [[ -n "$PY_TOO_OLD" ]]; then
        die "Rehome needs Python 3.11 or newer, but this system has $PY_TOO_OLD. Upgrade to a newer release of $OS_NAME."
    fi
    if [[ -z "$PM" ]]; then
        die "Python 3.11 or newer is required. Install it with your package manager, then run this installer again."
    fi
    step "Installing Python 3 (needed by the installer and by Rehome)"
    local pkg
    pkg="$(python_package)"
    install_packages "Install Python 3, which the installer and Rehome itself need" "$pkg"
    if ((DRY_RUN)); then
        say ""
        info "Python 3 is not installed yet, and the rest of the dry run needs it to read"
        info "data/packages.toml. After installing $pkg, run the dry run again to see the rest."
        exit 0
    fi
    find_python || die "Python 3.11 or newer is still not available after installing $pkg. Install it, then run this installer again."
}

# --- source -------------------------------------------------------------------------

download() {   # URL OUT
    local url="$1" out="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 --connect-timeout 20 -H "User-Agent: rehome-installer" -o "$out" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --user-agent=rehome-installer -O "$out" "$url"
    elif [[ -n "$PY" ]]; then
        "$PY" -I -c '
import shutil, sys, urllib.request
req = urllib.request.Request(sys.argv[1], headers={"User-Agent": "rehome-installer"})
with urllib.request.urlopen(req, timeout=60) as r, open(sys.argv[2], "wb") as f:
    shutil.copyfileobj(r, f)' "$url" "$out"
    else
        die "Neither curl nor wget is installed, so the source cannot be downloaded."
    fi
}

sha256_of() {
    "$PY" -I -c '
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())' "$1"
}

# verify_sha256 FILE CHECKSUM_FILE NAME: CHECKSUM_FILE holds "HASH" or "HASH  NAME" lines.
verify_sha256() {
    local file="$1" sums="$2" name="$3" want="" got h n
    while read -r h n || [[ -n "$h" ]]; do
        [[ "$h" =~ ^[0-9a-fA-F]{64}$ ]] || continue
        n="${n#\*}"
        if [[ -z "$n" || "$n" == "$name" || "$(basename -- "$n")" == "$name" ]]; then want="${h,,}"; break; fi
    done <"$sums"
    [[ -n "$want" ]] || die "The checksum file does not list $name."
    got="$(sha256_of "$file")"
    if [[ "$got" != "$want" ]]; then
        die "SHA-256 mismatch for $name: expected $want, got $got. The download is corrupt or was tampered with; nothing was installed."
    fi
    ok "SHA-256 verified ($got)"
}

extract_source() {   # TARBALL
    local dir="$TMP/src" d
    rm -rf "$dir"
    mkdir -p "$dir"
    tar -xzf "$1" -C "$dir" --no-same-owner --no-same-permissions || die "Could not extract $1."
    for d in "$dir" "$dir"/*/; do
        d="${d%/}"
        if [[ -f "$d/packaging/layout.py" && -f "$d/rehome/__init__.py" ]]; then
            SRC="$d"
            return 0
        fi
    done
    die "$1 does not contain the Rehome source."
}

release_info() {   # JSON → lines: tag, html_url, tarball url, tarball name, checksum url
    "$PY" -I -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
tag = d.get("tag_name") or ""
assets = d.get("assets") or []
tar = next((a for a in assets if a.get("name", "").startswith("rehome-") and a["name"].endswith(".tar.gz")), None)
sums = None
if tar:
    sums = next((a for a in assets if a.get("name") in (tar["name"] + ".sha256", "SHA256SUMS", "sha256sums.txt")), None)
print(tag)
print(d.get("html_url") or "")
print(tar["browser_download_url"] if tar else "")
print(tar["name"] if tar else "")
print(sums["browser_download_url"] if sums else "")' "$1"
}

fetch_source() {
    local s="$SOURCE_OVERRIDE"
    step "Getting the Rehome source"
    if [[ -n "$s" ]]; then
        if [[ -d "$s" ]]; then
            SRC="$(cd "$s" && pwd)"
            SRC_DESC="directory $SRC"
        elif [[ -f "$s" ]]; then
            if [[ -f "$s.sha256" ]]; then verify_sha256 "$s" "$s.sha256" "$(basename -- "$s")"; fi
            extract_source "$s"
            SRC_DESC="archive $(cd "$(dirname -- "$s")" && pwd)/$(basename -- "$s")"
        elif [[ "$s" =~ ^(https?|file):// ]]; then
            info "Downloading $s"
            download "$s" "$TMP/source.tar.gz" || die "Could not download $s."
            if download "$s.sha256" "$TMP/source.tar.gz.sha256" 2>/dev/null; then
                verify_sha256 "$TMP/source.tar.gz" "$TMP/source.tar.gz.sha256" "$(basename -- "${s%%\?*}")"
            else
                info "No checksum published next to it ($s.sha256); relying on the transport."
            fi
            extract_source "$TMP/source.tar.gz"
            SRC_DESC="$s"
        else
            die "--source/REHOME_SOURCE must be a directory, a .tar.gz file or an http(s)/file URL: $s"
        fi
    elif [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]] \
        && [[ -f "$(dirname -- "$SCRIPT_PATH")/rehome/__init__.py" && -f "$(dirname -- "$SCRIPT_PATH")/packaging/layout.py" ]]; then
        SRC="$(cd "$(dirname -- "$SCRIPT_PATH")" && pwd)"
        SRC_DESC="checkout $SRC"
        if command -v git >/dev/null 2>&1 && git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
            SRC_DESC="git checkout $SRC ($(git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo unknown))"
        fi
    else
        local tag="" html="" url="" name="" sums=""
        info "Looking up the latest release ($RELEASES_API)"
        if download "$RELEASES_API" "$TMP/release.json" 2>/dev/null; then
            { read -r tag; read -r html; read -r url; read -r name; read -r sums; } < <(release_info "$TMP/release.json") || true
        fi
        if [[ -n "$tag" && -n "$url" ]]; then
            info "Downloading release $tag ($name)${html:+; release notes: $html}"
            download "$url" "$TMP/$name" || die "Could not download $url."
            if [[ -n "$sums" ]]; then
                download "$sums" "$TMP/$name.sums" || die "Could not download the release checksum $sums."
                verify_sha256 "$TMP/$name" "$TMP/$name.sums" "$name"
            else
                warn "Release $tag publishes no SHA-256 checksum; relying on HTTPS."
            fi
            extract_source "$TMP/$name"
            SRC_DESC="release $tag ($url)"
        elif [[ -n "$tag" ]]; then
            url="https://codeload.github.com/${REPO}/tar.gz/refs/tags/${tag}"
            info "Downloading release $tag (no checksum is published for GitHub's automatic archives; relying on HTTPS)"
            download "$url" "$TMP/source.tar.gz" || die "Could not download $url."
            extract_source "$TMP/source.tar.gz"
            SRC_DESC="release $tag ($url)"
        else
            warn "No published release was found; installing the development version from the main branch."
            download "$MAIN_TARBALL" "$TMP/source.tar.gz" || die "Could not download $MAIN_TARBALL. Check your internet connection."
            extract_source "$TMP/source.tar.gz"
            SRC_DESC="main branch ($MAIN_TARBALL)"
        fi
    fi
    [[ -f "$SRC/packaging/layout.py" && -f "$SRC/rehome/__init__.py" ]] || die "$SRC is not a Rehome source tree."
    SRC_VERSION="$("$PY" -I "$SRC/packaging/layout.py" version)"
    ok "Rehome $SRC_VERSION from $SRC_DESC"
}

# --- manifest ------------------------------------------------------------------------

manifest_path() { printf '%s/share/rehome/install-manifest.json' "$PREFIX"; }

# Prints shell-safe "KEY=value" lines; arrays one item per line in M_* variables.
read_manifest() {
    "$PY" -I -c '
import json, shlex, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("app") != "rehome":
    sys.exit("not a Rehome manifest")
def out(k, v): print(f"{k}={shlex.quote(str(v))}")
out("M_VERSION", d.get("version", ""))
out("M_PREFIX", d.get("prefix", ""))
out("M_LIBEXECDIR", d.get("libexecdir", ""))
out("M_SOURCE", d.get("source", ""))
out("M_FAMILY", (d.get("distro") or {}).get("family", ""))
out("M_NFILES", len(d.get("files", [])))
pk = d.get("packages") or {}
out("M_PM", pk.get("manager", ""))
print("M_PKGS=(" + " ".join(shlex.quote(p) for p in pk.get("installed", [])) + ")")
print("M_GROUPS=(" + " ".join(shlex.quote(g["user"] + ":" + g["group"]) for g in d.get("groups", [])) + ")")
print("M_OVERRIDES=(" + " ".join(shlex.quote("|".join([o["path"], str(o.get("previous_mode", "")), o.get("owner", "root"), o.get("group", "root")])) for o in d.get("statoverrides", [])) + ")")
' "$1"
}

write_manifest_data() {   # OUT [OLD_MANIFEST]
    local out="$1" old="${2:-}"
    RH_PKGS="$(printf '%s\n' "${ADDED_PKGS[@]+"${ADDED_PKGS[@]}"}")" \
    RH_GROUPS="$(printf '%s\n' "${GROUPS_ADDED[@]+"${GROUPS_ADDED[@]}"}")" \
    RH_OVERRIDES="$(printf '%s\n' "${OVERRIDES_ADDED[@]+"${OVERRIDES_ADDED[@]}"}")" \
    RH_SOURCE="$SRC_DESC" RH_FAMILY="$FAMILY" RH_PM="$PM" RH_ID="$OS_ID" RH_NAME="$OS_NAME" \
    RH_OLD="$old" RH_INSTALLER="$SCRIPT_PATH" \
    "$PY" -I -c '
import json, os, sys
env = os.environ
def lines(k): return [x for x in env.get(k, "").splitlines() if x]
old = {}
if env.get("RH_OLD") and os.path.exists(env["RH_OLD"]):
    with open(env["RH_OLD"], encoding="utf-8") as f:
        old = json.load(f)
pk = old.get("packages") or {}
pkgs = list(pk.get("installed", []))
pkgs += [p for p in lines("RH_PKGS") if p not in pkgs]
groups = list(old.get("groups", []))
for g in lines("RH_GROUPS"):
    user, group = g.split(":", 1)
    if {"user": user, "group": group} not in groups:
        groups.append({"user": user, "group": group})
ovr = list(old.get("statoverrides", []))
for o in lines("RH_OVERRIDES"):
    path, prev, owner, group = o.split("|")
    if all(x["path"] != path for x in ovr):
        ovr.append({"path": path, "previous_mode": prev, "owner": owner, "group": group, "mode": "0644"})
data = {
    "source": env.get("RH_SOURCE", ""),
    "installer": "install.sh",
    "distro": {"id": env.get("RH_ID", ""), "name": env.get("RH_NAME", ""), "family": env.get("RH_FAMILY", "")},
    "packages": {"manager": env.get("RH_PM") or pk.get("manager", ""), "installed": pkgs},
    "groups": groups,
    "statoverrides": ovr,
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
' "$out"
}

# --- install steps -------------------------------------------------------------------

load_components() {   # sets COMP_* arrays from data/packages.toml for $FAMILY
    COMP_KEYS=() COMP_REQ=() COMP_LABEL=() COMP_PKGS=()
    local key req label pkgs
    while IFS=$'\t' read -r key req label pkgs; do
        COMP_KEYS+=("$key") COMP_REQ+=("$req") COMP_LABEL+=("$label") COMP_PKGS+=("$pkgs")
    done < <("$PY" -I "$SRC/packaging/layout.py" deps --family "$FAMILY" --all --format tsv)
}

print_manual_deps() {
    "$PY" -I -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    comps = tomllib.load(f)
for key, c in comps.items():
    if not c.get("required"):
        continue
    print("  - " + c.get("label", key))
    for fam, name in (("arch", "Arch"), ("debian", "Debian"), ("fedora", "Fedora"), ("suse", "openSUSE")):
        print("      " + (name + ":").ljust(10) + " " + " ".join(c.get(fam, [])))
' "$SRC/data/packages.toml"
}

install_dependencies() {
    if ((SKIP_DEPS)); then
        step "Skipping distribution packages (--skip-deps)"
        return 0
    fi
    if [[ -z "$PM" ]]; then
        step "Dependencies"
        if [[ -n "$FAMILY" ]]; then
            warn "$OS_NAME is a ${FAMILY}-family distribution, but its package manager was not found (an immutable or minimal system?)."
        else
            warn "Rehome does not know how to install packages on $OS_NAME (ID=${OS_ID:-?}${OS_LIKE:+, ID_LIKE=$OS_LIKE})."
        fi
        say "Install these with your distribution's tools (package names on the supported"
        say "distributions are shown for reference), then run this installer again with --skip-deps:"
        print_manual_deps
        exit 1
    fi
    load_components
    local i n=${#COMP_KEYS[@]} ans
    local -a req_pkgs=() opt_idx=() chosen=() nopkgs=()
    for ((i = 0; i < n; i++)); do
        if [[ -z "${COMP_PKGS[i]}" ]]; then nopkgs+=("${COMP_LABEL[i]}"); continue; fi
        if [[ "${COMP_REQ[i]}" == "1" ]]; then
            # shellcheck disable=SC2206  # package names never contain spaces or globs
            req_pkgs+=(${COMP_PKGS[i]})
        else
            opt_idx+=("$i")
        fi
    done
    step "Installing required packages ($PM)"
    ((${#nopkgs[@]})) && warn "No $FAMILY package is known for: ${nopkgs[*]}"
    install_packages "Install the ${#req_pkgs[@]} packages Rehome needs (QEMU, UEFI firmware, swtpm, libguestfs, GTK 4, ...)" "${req_pkgs[@]}"

    if ((${#opt_idx[@]})); then
        case "$OPTIONAL" in
            all) chosen=("${opt_idx[@]}") ;;
            none) ;;
            ask)
                step "Optional components"
                for ((i = 0; i < ${#opt_idx[@]}; i++)); do
                    printf '    %d) %s  %s(%s)%s\n' "$((i + 1))" "${COMP_LABEL[opt_idx[i]]}" "$C_DIM" "${COMP_PKGS[opt_idx[i]]}" "$C_0"
                done
                if ((ASSUME_YES)) || ((!HAVE_TTY)); then
                    info "Not installing these (use --with-optional to include them)."
                else
                    printf '    Install which? [a]ll, [n]one, or numbers like "1 3" [n]: ' >/dev/tty
                    IFS= read -r ans </dev/tty || ans=""
                    case "${ans,,}" in
                        a|all) chosen=("${opt_idx[@]}") ;;
                        ""|n|none) ;;
                        *)
                            for i in ${ans//,/ }; do
                                if [[ "$i" =~ ^[0-9]+$ ]] && ((i >= 1 && i <= ${#opt_idx[@]})); then
                                    chosen+=("${opt_idx[i - 1]}")
                                else
                                    warn "Ignoring \"$i\"."
                                fi
                            done ;;
                    esac
                fi ;;
        esac
        if ((${#chosen[@]})); then
            local -a opt_pkgs=() labels=()
            local joined
            for i in "${chosen[@]}"; do
                # shellcheck disable=SC2206
                opt_pkgs+=(${COMP_PKGS[i]})
                labels+=("${COMP_LABEL[i]%% (*}")
            done
            step "Installing optional packages"
            joined="$(IFS=';'; echo "${labels[*]}")"
            install_packages "Install the optional components you chose: ${joined//;/, }" "${opt_pkgs[@]}"
        fi
    fi
    if ((${#FAILED_PKGS[@]})); then
        warn "These packages could not be installed: ${FAILED_PKGS[*]}"
        if [[ "$PM" == "pacman" ]]; then
            info "If pacman said \"target not found\" for packages that exist, its package databases"
            info "are missing or stale: update the system with \"sudo pacman -Syu\", then run this again."
        fi
        info "Rehome will still be installed; \"rehome doctor\" explains what does not work without them."
    fi
}

in_group() { id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qx "$2"; }

setup_kvm_group() {
    ((KVM_GROUP)) || return 0
    [[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]] || return 0
    getent group kvm >/dev/null 2>&1 || return 0
    in_group "$TARGET_USER" kvm && return 0
    step "KVM access"
    info "/dev/kvm usually belongs to the kvm group. Members can run VMs with hardware"
    info "acceleration without root."
    if priv "Add $TARGET_USER to the kvm group (takes effect after logging out and back in)" usermod -aG kvm "$TARGET_USER"; then
        ((DRY_RUN)) || GROUPS_ADDED+=("$TARGET_USER:kvm")
    elif ((!PRIV_DECLINED)); then
        warn "Could not add $TARGET_USER to the kvm group."
    fi
}

setup_kernel_readable() {
    ((KERNEL_FIX)) || return 0
    [[ "$FAMILY" == "debian" ]] || return 0
    command -v dpkg-statoverride >/dev/null 2>&1 || return 0
    local -a kernels=() k
    local newest mode shown=0
    [[ -f "/boot/vmlinuz-$(uname -r)" ]] && kernels+=("/boot/vmlinuz-$(uname -r)")
    newest="$(find /boot -maxdepth 1 -name 'vmlinuz-*' -type f 2>/dev/null | sort -V | tail -n 1 || true)"
    [[ -n "$newest" && "$newest" != "${kernels[0]:-}" ]] && kernels+=("$newest")
    for k in "${kernels[@]+"${kernels[@]}"}"; do
        mode="$(stat -c %a "$k")"
        ((8#$mode & 4)) && continue                       # already world-readable
        if dpkg-statoverride --list "$k" >/dev/null 2>&1; then
            info "$k has a permission override already; leaving it alone."
            continue
        fi
        if ((!shown)); then
            step "Kernel image permissions (libguestfs)"
            info "libguestfs builds a small appliance from your kernel to inspect and repair"
            info "imported systems. $OS_NAME installs the kernel readable only by root;"
            info "libguestfs' documentation recommends making it readable. Without this,"
            info "rehome doctor reports \"libguestfs appliance\" and imports cannot be inspected."
            shown=1
        fi
        if priv "Make $k readable by everyone (mode 0644), kept across kernel package updates" \
            dpkg-statoverride --update --add root root 0644 "$k"; then
            ((DRY_RUN)) || OVERRIDES_ADDED+=("$k|$mode|root|root")
        elif ((!PRIV_DECLINED)); then
            warn "Could not change the permissions of $k."
        fi
    done
}

warn_distro_package() {
    local found=""
    case "$FAMILY" in
        debian) pkg_installed rehome && found="the rehome .deb" ;;
        arch) pacman -Q rehome >/dev/null 2>&1 && found="the rehome pacman package" ;;
        fedora|suse) rpm -q rehome >/dev/null 2>&1 && found="the rehome RPM" ;;
    esac
    [[ -n "$found" ]] && warn "$found is also installed; the copy in $PREFIX/bin comes first in PATH. Consider removing one of them."
    return 0
}

install_files() {
    local old="" data="$TMP/manifest-data.json" manifest
    manifest="$(manifest_path)"
    [[ -f "$manifest" ]] && old="$manifest"
    write_manifest_data "$data" "$old"
    step "Installing Rehome $SRC_VERSION into $PREFIX"
    local nfiles
    nfiles="$("$PY" -I "$SRC/packaging/layout.py" list --prefix "$PREFIX" --installer-copy | wc -l)"
    info "$nfiles files: the program in $PREFIX/lib/rehome, data in $PREFIX/share/rehome,"
    info "the launcher $PREFIX/bin/rehome, the permission helper in $PREFIX/libexec/rehome,"
    info "the polkit policy in /usr/share/polkit-1/actions and the desktop entry and icons."
    if priv "Install Rehome's files and record them in $manifest" \
        "$PY" -I "$SRC/packaging/layout.py" install --prefix "$PREFIX" --python "$PY" \
        --installer-copy --manifest --manifest-data "$data" --quiet; then
        ((DRY_RUN)) && return 0
        MANIFEST_WRITTEN=1
        ok "Installed; manifest written to $manifest"
    else
        ((PRIV_DECLINED)) && die "Cancelled: Rehome's files were not installed."
        die "Installing Rehome's files failed."
    fi
}

run_doctor() {
    local rehome="$PREFIX/bin/rehome"
    local -a cmd=("$rehome" doctor --quick)
    step "Checking this computer (rehome doctor --quick)"
    if ((DRY_RUN)); then
        info "(dry run: would run $(show_cmd "${cmd[@]}"))"
        return 0
    fi
    if ((EUID == 0)) && [[ "$TARGET_USER" != "root" ]] && command -v sudo >/dev/null 2>&1; then
        cmd=(sudo -u "$TARGET_USER" -H -- "${cmd[@]}")    # as you, not as root
    fi
    printf '    %s$ %s%s\n\n' "$C_DIM" "$(show_cmd "${cmd[@]}")" "$C_0"
    if ! "${cmd[@]}" </dev/null; then
        say ""
        info "rehome doctor found problems; see above for what they mean and how to fix them."
    fi
}

summary_install() {
    local manifest
    manifest="$(manifest_path)"
    step "Done"
    if ((DRY_RUN)); then
        say "Dry run finished; nothing was changed."
        return 0
    fi
    say "${C_GRN}Rehome $SRC_VERSION is installed.${C_0}"
    info "Start it from your applications menu (\"Rehome\") or run: $PREFIX/bin/rehome"
    case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) warn "$PREFIX/bin is not in your PATH." ;; esac
    info "Update:    curl -fsSL $RAW_URL | bash -s -- --update"
    info "           (or $PREFIX/share/rehome/installer/install.sh --update)"
    info "Uninstall: $PREFIX/share/rehome/installer/install.sh --uninstall"
    info "Manifest:  $manifest"
    ((${#ADDED_PKGS[@]})) && info "Packages added by the installer: ${ADDED_PKGS[*]}"
    if ((${#GROUPS_ADDED[@]})); then
        say ""
        say "${C_YEL}Log out and back in${C_0} so that your new kvm group membership takes effect."
    fi
    if ((${#FAILED_PKGS[@]})); then
        say ""
        warn "Not installed: ${FAILED_PKGS[*]} (see rehome doctor above)."
    fi
}

do_install() {
    ensure_python
    fetch_source
    warn_distro_package
    install_dependencies
    setup_kvm_group
    setup_kernel_readable
    install_files
    run_doctor
    summary_install
}

compare_versions() {   # A B → prints -1, 0 or 1
    "$PY" -I -c '
import re, sys
def key(s):
    s = s.strip().lstrip("vV")
    m = re.match(r"(\d+(?:\.\d+)*)(.*)", s)
    nums = [int(x) for x in m.group(1).split(".")] if m else [0]
    pre = (m.group(2) if m else s).lstrip(".-+")
    return nums + [0] * (4 - len(nums)), pre == "", pre
a, b = key(sys.argv[1]), key(sys.argv[2])
print((a > b) - (a < b))' "$1" "$2"
}

do_update() {
    local manifest
    manifest="$(manifest_path)"
    if [[ ! -f "$manifest" ]]; then
        die "No installation made by install.sh was found ($manifest is missing). If you installed a distribution package, update it with your package manager; otherwise run the installer without --update."
    fi
    ensure_python
    local M_VERSION="" M_PREFIX="" M_LIBEXECDIR="" M_SOURCE="" M_FAMILY="" M_NFILES="" M_PM="" mdata
    local -a M_PKGS=() M_GROUPS=() M_OVERRIDES=()
    mdata="$(read_manifest "$manifest")" || die "Cannot read $manifest."
    eval "$mdata"
    info "Installed: Rehome $M_VERSION (from $M_SOURCE)"
    fetch_source
    local cmp
    cmp="$(compare_versions "$SRC_VERSION" "$M_VERSION")"
    if ((cmp == 0 && !FORCE)); then
        say ""
        say "${C_GRN}Rehome $M_VERSION is already up to date.${C_0} (Use --force to reinstall it anyway.)"
        return 0
    fi
    if ((cmp < 0 && !FORCE)); then
        die "The source has Rehome $SRC_VERSION, which is older than the installed $M_VERSION. Use --force to downgrade."
    fi
    info "Updating Rehome $M_VERSION → $SRC_VERSION. Your VMs and settings are not touched."
    [[ "$OPTIONAL" == "ask" ]] && OPTIONAL=none
    install_dependencies
    setup_kvm_group
    setup_kernel_readable
    install_files
    run_doctor
    summary_install
}

# --- uninstall -----------------------------------------------------------------------

user_home() { getent passwd "$1" 2>/dev/null | cut -d: -f6 || true; }

user_data_dirs() {
    local home x
    home="$(user_home "$TARGET_USER")"
    [[ -n "$home" ]] || home="$HOME"
    local data="$home/.local/share" config="$home/.config" cache="$home/.cache" state="$home/.local/state"
    if ((EUID != 0)); then   # running as that user: honour their XDG variables
        x="${XDG_DATA_HOME:-}" && [[ "$x" == /* ]] && data="$x"
        x="${XDG_CONFIG_HOME:-}" && [[ "$x" == /* ]] && config="$x"
        x="${XDG_CACHE_HOME:-}" && [[ "$x" == /* ]] && cache="$x"
        x="${XDG_STATE_HOME:-}" && [[ "$x" == /* ]] && state="$x"
    fi
    printf '%s\n' "$data/rehome" "$config/rehome" "$cache/rehome" "$state/rehome"
}

rehome_vms_running() {
    command -v pgrep >/dev/null 2>&1 || return 1
    pgrep -u "$TARGET_USER" -f 'qemu-system.*rehome' >/dev/null 2>&1
}

# rpm_removable PKG...: the subset that no other installed package needs.
rpm_removable() {
    local -a cands=("$@") keep=() keep_now=() caps=() next=()
    local out cap p prov _
    for _ in 1 2 3 4 5 6 7 8; do
        ((${#cands[@]})) || break
        out="$(LC_ALL=C rpm -e --test "${cands[@]}" 2>&1)" && break
        mapfile -t caps < <(sed -n 's/^[[:space:]]*\(.*\) is needed by (installed) .*/\1/p' <<<"$out" | sort -u)
        ((${#caps[@]})) || { keep+=("${cands[@]}"); cands=(); break; }
        keep_now=()
        for cap in "${caps[@]}"; do
            while IFS= read -r prov; do
                for p in "${cands[@]}"; do [[ "$p" == "$prov" ]] && keep_now+=("$p"); done
            done < <(rpm -q --whatprovides "$cap" --qf '%{NAME}\n' 2>/dev/null)
        done
        ((${#keep_now[@]})) || { keep+=("${cands[@]}"); cands=(); break; }
        next=()
        for p in "${cands[@]}"; do
            if printf '%s\n' "${keep_now[@]}" | grep -qx -- "$p"; then keep+=("$p"); else next+=("$p"); fi
        done
        cands=("${next[@]+"${next[@]}"}")
    done
    printf '%s\n' "${cands[@]+"${cands[@]}"}"
}

remove_packages() {   # PKG... (only the ones still installed)
    local -a pkgs=("$@") still=() cmd=() removable=()
    local p failed=0
    # Configuration files are kept, also with --purge: purging a dependency can
    # delete system files it adopted (e.g. the locales package's purge removes
    # /etc/default/locale), and --purge is about Rehome's own data.
    step "Removing the packages the installer added"
    info "${#pkgs[@]} package(s): ${pkgs[*]}"
    info "Packages that other installed software still needs are kept."
    case "$PM" in
        apt)
            info "They are marked as automatically installed and removed with \"apt-get autoremove\","
            info "which only removes packages nothing else depends on (apt lists them first)."
            if ! confirm "Remove them?"; then info "Kept the packages."; return 0; fi
            PRIV_ASK=0
            if priv "Mark the packages the installer added as automatically installed" apt-mark auto "${pkgs[@]}"; then
                cmd=(apt-get autoremove)
                ((ASSUME_YES || !HAVE_TTY)) && cmd=(env DEBIAN_FRONTEND=noninteractive "${cmd[@]}" -y)
                if ! priv "Remove them and the dependencies only they needed" "${cmd[@]}"; then
                    failed=1
                    priv "Restore the packages' manual-install marks, since removal did not happen" apt-mark manual "${pkgs[@]}" || true
                fi
            else
                failed=1
            fi
            PRIV_ASK=1 ;;
        pacman)
            if ! confirm "Remove them?"; then info "Kept the packages."; return 0; fi
            cmd=(pacman -Rsu)
            ((ASSUME_YES)) && cmd+=(--noconfirm)
            PRIV_ASK=0
            priv "Remove them and dependencies nothing else needs (-u skips any that other packages need)" "${cmd[@]}" "${pkgs[@]}" || failed=1
            PRIV_ASK=1 ;;
        dnf|dnf5|zypper)
            mapfile -t removable < <(rpm_removable "${pkgs[@]}")
            if ((${#removable[@]} < ${#pkgs[@]})); then
                local -a needed=()
                for p in "${pkgs[@]}"; do printf '%s\n' "${removable[@]+"${removable[@]}"}" | grep -qx -- "$p" || needed+=("$p"); done
                info "Kept because other installed software needs them: ${needed[*]}"
            fi
            ((${#removable[@]})) || return 0
            if ! confirm "Remove ${removable[*]}?"; then info "Kept the packages."; return 0; fi
            if [[ "$PM" == "zypper" ]]; then
                cmd=(zypper)
                ((ASSUME_YES || !HAVE_TTY)) && cmd+=(--non-interactive)
                cmd+=(remove --clean-deps)
            else
                cmd=("$PM" remove)
                ((ASSUME_YES || !HAVE_TTY)) && cmd+=(-y)
            fi
            PRIV_ASK=0
            priv "Remove them and the dependencies only they needed" "${cmd[@]}" "${removable[@]}" || failed=1
            PRIV_ASK=1 ;;
        *)
            warn "Unknown package manager \"$PM\"; remove these yourself if you no longer need them: ${pkgs[*]}"
            return 0 ;;
    esac
    ((DRY_RUN)) && return 0
    for p in "${pkgs[@]}"; do pkg_installed "$p" && still+=("$p"); done
    if ((${#still[@]})); then
        info "Still installed (needed by other software, or removal was declined): ${still[*]}"
    else
        ok "Removed: ${pkgs[*]}"
    fi
    ((failed)) && warn "The package manager reported a problem; see its output above."
    return 0
}

purge_user_data() {
    local -a dirs=() existing=()
    local d
    mapfile -t dirs < <(user_data_dirs)
    for d in "${dirs[@]}"; do [[ -e "$d" ]] && existing+=("$d"); done
    step "Deleting your Rehome data (--purge)"
    if ((${#existing[@]} == 0)); then
        info "No Rehome data found for $TARGET_USER."
        return 0
    fi
    if rehome_vms_running; then
        die "Rehome VMs are still running for $TARGET_USER. Shut them down, then run --purge again."
    fi
    say "    ${C_RED}This permanently deletes your Rehome VMs (including their disks), checkpoints and settings:${C_0}"
    for d in "${existing[@]}"; do
        info "  $d ($(du -sh -- "$d" 2>/dev/null | cut -f1 || true))"
    done
    info "Disks and images outside these folders (for example a linked physical disk) are not touched."
    if ((DRY_RUN)); then
        info "(dry run: would run $(show_cmd rm -rf --one-file-system -- "${existing[@]}"))"
        return 0
    fi
    if ((!ASSUME_YES)); then
        ((HAVE_TTY)) || die "Cannot ask for confirmation without a terminal; re-run with --yes to delete the data."
        local ans
        printf '    Type "delete" to delete them: ' >/dev/tty
        IFS= read -r ans </dev/tty || ans=""
        [[ "$ans" == "delete" ]] || { info "Not deleted."; return 0; }
    fi
    for d in "${existing[@]}"; do
        [[ "$d" == */rehome && "$d" != "/rehome" ]] || die "Refusing to delete unexpected path $d."
        printf '    $ %s\n' "$(show_cmd rm -rf --one-file-system -- "$d")"
        rm -rf --one-file-system -- "$d"
    done
    ok "Deleted your Rehome data."
}

do_uninstall() {
    local manifest
    manifest="$(manifest_path)"
    if [[ ! -f "$manifest" ]]; then
        local hint=""
        case "$FAMILY" in
            debian) pkg_installed rehome && hint=" The rehome .deb is installed: remove it with \"sudo apt remove rehome\"." ;;
            arch) pacman -Q rehome >/dev/null 2>&1 && hint=" The rehome package is installed: remove it with \"sudo pacman -R rehome\"." ;;
            fedora|suse) rpm -q rehome >/dev/null 2>&1 && hint=" The rehome RPM is installed: remove it with your package manager." ;;
        esac
        if [[ "$MODE" == "purge" ]]; then
            say "Rehome is not installed under $PREFIX (no $manifest).$hint"
            purge_user_data
            return 0
        fi
        die "No installation made by install.sh was found ($manifest is missing; use --prefix if you installed elsewhere).$hint"
    fi
    find_python || die "Python 3.11 or newer is needed to read $manifest, but it is not installed."
    # shellcheck disable=SC2034  # all set by the manifest; some unused here
    local M_VERSION="" M_PREFIX="" M_LIBEXECDIR="" M_SOURCE="" M_FAMILY="" M_NFILES="" M_PM="" mdata
    local -a M_PKGS=() M_GROUPS=() M_OVERRIDES=()
    mdata="$(read_manifest "$manifest")" || die "Cannot read $manifest."
    eval "$mdata"
    [[ -n "$M_PM" && -z "$PM" ]] && PM="$M_PM"
    [[ -n "$M_FAMILY" ]] && FAMILY="$M_FAMILY"
    local layout="$PREFIX/share/rehome/installer/layout.py"
    if [[ ! -f "$layout" ]]; then
        fetch_source
        layout="$SRC/packaging/layout.py"
    fi
    local -a pkgs=() groups=() overrides=()
    local p g o
    for p in "${M_PKGS[@]+"${M_PKGS[@]}"}"; do pkg_installed "$p" && pkgs+=("$p"); done
    for g in "${M_GROUPS[@]+"${M_GROUPS[@]}"}"; do in_group "${g%%:*}" "${g#*:}" && groups+=("$g"); done
    for o in "${M_OVERRIDES[@]+"${M_OVERRIDES[@]}"}"; do overrides+=("$o"); done

    step "Uninstalling Rehome $M_VERSION from $PREFIX"
    info "Recorded in $manifest:"
    info "  - $M_NFILES files (program, data, helper, polkit policy, desktop entry, icons)"
    ((${#overrides[@]})) && info "  - kernel permission override(s): $(for o in "${overrides[@]}"; do printf '%s ' "${o%%|*}"; done)"
    ((${#groups[@]})) && info "  - group membership(s) added: ${groups[*]}"
    if ((${#pkgs[@]})); then
        if ((KEEP_DEPS)); then info "  - ${#pkgs[@]} packages the installer added (kept: --keep-deps)"
        else info "  - ${#pkgs[@]} packages the installer added: ${pkgs[*]}"; fi
    fi
    if rehome_vms_running; then
        warn "Rehome VMs are running. They keep running, but you cannot manage them until Rehome is reinstalled."
    fi

    # 1. Files first: this needs Python, which may be one of the packages removed below.
    if ! priv "Remove Rehome's files listed in the manifest (and the manifest itself)" \
        "$PY" -I "$layout" uninstall --manifest "$manifest"; then
        ((PRIV_DECLINED)) && die "Cancelled; nothing was removed."
        die "Removing Rehome's files failed; nothing else was changed. The manifest is still at $manifest."
    fi
    # 2. Kernel permission overrides.
    local path prev rest
    for o in "${overrides[@]+"${overrides[@]}"}"; do
        path="${o%%|*}" rest="${o#*|}" prev="${rest%%|*}"
        if dpkg-statoverride --list "$path" >/dev/null 2>&1; then
            priv "Remove the permission override for $path that the installer added" dpkg-statoverride --remove "$path" || true
        fi
        if [[ -f "$path" && -n "$prev" && "$(stat -c %a "$path")" != "$prev" ]]; then
            priv "Restore the original permissions ($prev) of $path" chmod "$prev" "$path" || true
        fi
    done
    # 3. Group memberships.
    for g in "${groups[@]+"${groups[@]}"}"; do
        priv "Remove ${g%%:*} from the ${g#*:} group (the installer added it)" gpasswd -d "${g%%:*}" "${g#*:}" || true
    done
    # 4. Packages.
    if ((${#pkgs[@]})); then
        if ((KEEP_DEPS)); then
            step "Keeping the packages the installer added (--keep-deps)"
            info "${pkgs[*]}"
        else
            remove_packages "${pkgs[@]}"
        fi
    fi
    if [[ "$MODE" == "purge" ]]; then
        purge_user_data
    fi
    step "Done"
    if ((DRY_RUN)); then
        say "Dry run finished; nothing was changed."
        return 0
    fi
    say "${C_GRN}Rehome has been uninstalled.${C_0}"
    if [[ "$MODE" != "purge" ]]; then
        local -a dirs=() existing=()
        local d
        mapfile -t dirs < <(user_data_dirs)
        for d in "${dirs[@]}"; do [[ -e "$d" ]] && existing+=("$d"); done
        if ((${#existing[@]})); then
            info "Your VMs and settings were kept:"
            for d in "${existing[@]}"; do info "  $d"; done
            info "Reinstall Rehome to use them again, or delete them with --purge."
        fi
    fi
}

# --- main ----------------------------------------------------------------------------

cleanup() {
    local rc=$?
    if ((rc != 0)) && ((!MANIFEST_WRITTEN)) && ((!DRY_RUN)) && [[ "$MODE" == "install" || "$MODE" == "update" ]]; then
        if ((${#ADDED_PKGS[@]} || ${#GROUPS_ADDED[@]} || ${#OVERRIDES_ADDED[@]})); then
            printf '\n%sThe installer stopped before it finished.%s Changes made so far (not recorded in a manifest):\n' "$C_YEL" "$C_0" >&2
            ((${#ADDED_PKGS[@]})) && printf '  packages installed: %s\n' "${ADDED_PKGS[*]}" >&2
            ((${#GROUPS_ADDED[@]})) && printf '  group memberships added: %s\n' "${GROUPS_ADDED[*]}" >&2
            ((${#OVERRIDES_ADDED[@]})) && printf '  kernel permission overrides: %s\n' "${OVERRIDES_ADDED[*]%%|*}" >&2
            printf 'Run the installer again to finish, or undo these by hand.\n' >&2
        fi
    fi
    [[ -n "$TMP" && -d "$TMP" ]] && rm -rf -- "$TMP"
    return "$rc"
}

parse_args() {
    while (($#)); do
        case "$1" in
            --update) MODE=update ;;
            --uninstall) MODE=uninstall ;;
            --purge) MODE=purge ;;
            --detect) MODE=detect ;;
            -y|--yes) ASSUME_YES=1 ;;
            -n|--dry-run) DRY_RUN=1 ;;
            --with-optional) OPTIONAL=all ;;
            --no-optional) OPTIONAL=none ;;
            --skip-deps) SKIP_DEPS=1 ;;
            --keep-deps) KEEP_DEPS=1 ;;
            --no-kvm-group) KVM_GROUP=0 ;;
            --no-kernel-fix) KERNEL_FIX=0 ;;
            --force) FORCE=1 ;;
            --prefix) (($# >= 2)) || die "--prefix needs a directory"; PREFIX="$2"; shift ;;
            --prefix=*) PREFIX="${1#*=}" ;;
            --source) (($# >= 2)) || die "--source needs a value"; SOURCE_OVERRIDE="$2"; shift ;;
            --source=*) SOURCE_OVERRIDE="${1#*=}" ;;
            -h|--help) usage; exit 0 ;;
            *) die "Unknown option: $1 (see --help)" ;;
        esac
        shift
    done
    [[ "$PREFIX" == /* ]] || die "--prefix must be an absolute path."
    PREFIX="${PREFIX%/}"
    [[ -n "$PREFIX" ]] || die "--prefix / is not supported."
    if [[ -n "$SOURCE_OVERRIDE" && -e "$SOURCE_OVERRIDE" && "$SOURCE_OVERRIDE" != /* ]]; then
        SOURCE_OVERRIDE="$(cd "$(dirname -- "$SOURCE_OVERRIDE")" && pwd)/$(basename -- "$SOURCE_OVERRIDE")"
    fi
}

main() {
    parse_args "$@"
    detect_distro
    if [[ "$MODE" == "detect" ]]; then
        printf 'name=%s\nid=%s\nid_like=%s\nfamily=%s\npackage_manager=%s\n' \
            "$OS_NAME" "$OS_ID" "$OS_LIKE" "${FAMILY:-unknown}" "${PM:-none}"
        return 0
    fi
    detect_tty
    setup_root_tool
    TMP="$(mktemp -d "${TMPDIR:-/tmp}/rehome-install.XXXXXXXX")"
    trap cleanup EXIT
    trap 'exit 130' INT TERM

    say "${C_B}Rehome installer${C_0}"
    info "System:  $OS_NAME (${FAMILY:-unknown family}, package manager: ${PM:-none found})"
    info "Prefix:  $PREFIX"
    if ((EUID == 0)); then info "Running as root; user: $TARGET_USER"
    else info "Commands that need root are run with sudo; you are asked before each one."; fi
    ((DRY_RUN)) && say "${C_YEL}Dry run:${C_0} only showing what would be done."
    case "$MODE" in
        install) do_install ;;
        update) do_update ;;
        uninstall|purge) do_uninstall ;;
    esac
}

# Only run when executed (tests may source this file with REHOME_INSTALLER_NO_MAIN=1).
# Keeping the call on the last line also means a truncated download never runs.
[[ "${REHOME_INSTALLER_NO_MAIN:-0}" == "1" ]] || main "$@"
