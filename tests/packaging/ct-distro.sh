#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Runs as root inside an Arch/Debian/Fedora/openSUSE/Ubuntu container:
#   ct-distro.sh FAMILY PM
# /src is the repository (read-only); /opt/rhtest the Python bundle.
#
# 1. distro detection
# 2. --dry-run without python3: the exact bootstrap command
# 3. a real run without python3 (mirrors blocked): clear error, nothing changed
# 4. --dry-run with python3: the exact package commands, required and optional
# 5. declining the first root command (through a pseudo-terminal): nothing changed
# 6. a real install whose package installs fail (mirrors blocked): the failed
#    names are reported, Rehome's files are installed anyway, uninstall is clean
# 7. a file-only round trip (--skip-deps): install, rehome --version, update,
#    uninstall, purge; the filesystem ends exactly as it started

set -uo pipefail
# shellcheck source=tests/packaging/lib.sh
. /src/tests/packaging/lib.sh
FAMILY="$1"
PM="$2"
export NO_COLOR=1
INSTALL=/src/install.sh
mkdir -p /work
# A real home has the XDG base directories; Rehome creates only its own
# "rehome" folders inside them (which --purge removes).
mkdir -p /root/.cache /root/.config /root/.local/share /root/.local/state

case "$PM" in
    pacman) PMCMD="pacman -S --needed --noconfirm"; PYPKG=python ;;
    apt) PMCMD="env DEBIAN_FRONTEND=noninteractive apt-get install -y"; PYPKG=python3 ;;
    dnf) PMCMD="dnf install -y"; PYPKG=python3 ;;
    zypper) PMCMD="zypper --non-interactive install"; PYPKG=python3 ;;
esac

installed() {   # independent of install.sh's own check
    case "$FAMILY" in
        debian) [[ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null)" == installed ]] ;;
        arch) pacman -T "$1" >/dev/null 2>&1 ;;
        *) rpm -q --whatprovides "$1" >/dev/null 2>&1 ;;
    esac
}

expected_cmd() {   # --required|--optional → "$ PMCMD pkgs-not-installed..."
    local p line="\$ $PMCMD"
    for p in $(python3 /src/packaging/layout.py deps --family "$FAMILY" "$1"); do
        installed "$p" || line+=" $p"
    done
    printf '%s' "$line"
}

section "1. detection"
out="$("$INSTALL" --detect)"
printf '%s\n' "$out"
check "family is $FAMILY" contains "$out" "family=$FAMILY"
check "package manager is $PM" contains "$out" "package_manager=$PM"

section "2. dry run without python3"
if command -v python3 >/dev/null 2>&1; then
    note "python3 is already installed in this image; skipping"
else
    out="$("$INSTALL" --dry-run --yes 2>&1)"
    rc=$?
    printf '%s\n' "$out"
    check "exit status 0" test "$rc" -eq 0
    check "shows the exact python bootstrap command" contains "$out" "\$ $PMCMD $PYPKG"
    check "explains why it stops" contains "$out" "Python 3 is not installed yet"

    section "3. real run without python3 (package mirrors blocked)"
    snapshot /work/s0
    out="$("$INSTALL" --yes --skip-deps 2>&1)"
    rc=$?
    printf '%s\n' "$out"
    snapshot /work/s1
    check "fails (exit $rc)" test "$rc" -ne 0
    check "prints the bootstrap command before running it" contains "$out" "\$ $PMCMD $PYPKG"
    check "clear error message" contains "$out" "Python 3.11 or newer is still not available"
    d="$(snapdiff /work/s0 /work/s1)"
    [[ -n "$d" ]] && printf 'changed:\n%s\n' "$d"
    check "nothing on disk changed" test -z "$d"
fi

use_bundle_python /usr/local/bin || { fail "no Python bundle"; finish; exit; }
check "python3 >= 3.11 available (bundle)" python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'

section "4. dry run with python3: exact package commands"
out="$("$INSTALL" --dry-run --yes 2>&1)"
printf '%s\n' "$out"
req="$(expected_cmd --required)"
check "required packages command: $req" grep -qxF "    $req" <<<"$out"
check "optional components not installed without --with-optional" contains "$out" "Not installing these"
if [[ "$PM" == "apt" ]]; then
    check "apt-get update shown first" contains "$out" "\$ env DEBIAN_FRONTEND=noninteractive apt-get update"
fi
out="$("$INSTALL" --dry-run --yes --with-optional 2>&1)"
opt="$(expected_cmd --optional)"
check "optional packages command: $opt" grep -qxF "    $opt" <<<"$out"
check "layout.py install command shown" contains "$out" "\$ $(command -v python3) -I /src/packaging/layout.py install --prefix /usr/local"
check "dry run changed nothing" test ! -e /usr/local/share/rehome

section "5. declining the first root command"
if command -v script >/dev/null 2>&1; then
    snapshot /work/s0
    out="$(printf 'n\n' | script -qec "$INSTALL --skip-deps --no-kvm-group --no-kernel-fix" /dev/null 2>&1)"
    printf '%s\n' "$out"
    snapshot /work/s1
    check "the command and its reason were shown" contains "$out" "Needs root: Install Rehome's files"
    check "it asked" contains "$out" "Run this command? [Y/n]"
    check "declining stops the installer" contains "$out" "Cancelled: Rehome's files were not installed."
    check "nothing on disk changed" test -z "$(snapdiff /work/s0 /work/s1)"
else
    note "script(1) is not installed in this image; skipping the pseudo-terminal test"
fi

section "6. real install with package installs failing (mirrors blocked)"
snapshot /work/s0
out="$("$INSTALL" --yes --no-optional 2>&1)"
rc=$?
printf '%s\n' "$out"
check "exit status 0 (Rehome installed without the packages)" test "$rc" -eq 0
check "falls back to one package at a time" contains "$out" "Trying them one at a time"
missing=()
for p in $(python3 /src/packaging/layout.py deps --family "$FAMILY" --required); do installed "$p" || missing+=("$p"); done
check "reports exactly which packages failed (${#missing[@]})" contains "$out" "These packages could not be installed: ${missing[*]}"
m=/usr/local/share/rehome/install-manifest.json
check "manifest written" test -f "$m"
check "manifest records no packages" python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1]))["packages"]["installed"] != [])' "$m"
check "rehome doctor ran" contains "$out" "Rehome host check"
out="$("$INSTALL" --uninstall --yes 2>&1)"
rc=$?
printf '%s\n' "$out"
check "uninstall exit 0" test "$rc" -eq 0
rm -rf /root/.cache/rehome /root/.local/share/rehome /root/.config/rehome /root/.local/state/rehome
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
[[ -n "$d" ]] && printf 'left behind:\n%s\n' "$d"
check "nothing left behind" test -z "$d"

section "7. file-only round trip (--skip-deps)"
snapshot /work/s0
out="$("$INSTALL" --yes --skip-deps 2>&1)"
rc=$?
printf '%s\n' "$out"
check "install exit 0" test "$rc" -eq 0
check "manifest written" test -f "$m"
mapfile -t files < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["files"]))' "$m")
missing_files=0
for f in "${files[@]}"; do [[ -f "$f" ]] || { missing_files=$((missing_files + 1)); echo "missing: $f"; }; done
check "all ${#files[@]} manifest files exist" test "$missing_files" -eq 0
check "manifest lists exactly the layout's files" same_files <(printf '%s\n' "${files[@]}") \
    <(python3 /src/packaging/layout.py list --prefix /usr/local --installer-copy; echo "$m")
check "helper shebang uses python -I" grep -qx "#!$(command -v python3) -I" <(head -1 /usr/local/libexec/rehome/rehome-helper)
check "polkit policy names the helper" grep -q "/usr/local/libexec/rehome/rehome-helper" /usr/share/polkit-1/actions/io.github.oreo1298.Rehome.policy
ver="$(/usr/local/bin/rehome --version 2>&1)"
check "rehome --version: $ver" test "$ver" = "rehome $(python3 /src/packaging/layout.py version)"
check "no bytecode next to the installed sources" test -z "$(python3 -c 'import pathlib; print(*pathlib.Path("/usr/local/lib/rehome").rglob("__pycache__"))')"
snapshot /work/s-installed
d="$(snapdiff /work/s0 /work/s-installed)"
check "positive control: the snapshot sees the installed files" contains "$d" "+ f /usr/local/bin/rehome"

# Update from a copy with a bumped version.
rm -rf /work/src2
cp -a /src /work/src2
sed -i 's/^__version__ = .*/__version__ = "99.0.0"/' /work/src2/rehome/__init__.py
out="$(REHOME_SOURCE=/work/src2 /usr/local/share/rehome/installer/install.sh --update --yes --skip-deps 2>&1)"
check "update to 99.0.0" contains "$out" "Rehome 99.0.0 is installed."
check "rehome --version after update" test "$(/usr/local/bin/rehome --version 2>&1)" = "rehome 99.0.0"
out="$(REHOME_SOURCE=/work/src2 /usr/local/share/rehome/installer/install.sh --update --yes --skip-deps 2>&1)"
check "second update: already up to date" contains "$out" "Rehome 99.0.0 is already up to date."

out="$(/usr/local/share/rehome/installer/install.sh --uninstall --yes 2>&1)"
rc=$?
printf '%s\n' "$out"
check "uninstall exit 0" test "$rc" -eq 0
for f in "${files[@]}"; do [[ -e "$f" ]] && fail "still present: $f"; done
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
printf 'after uninstall, differences:\n%s\n' "$d"
check "only the user's Rehome cache remains (kept by uninstall)" test -z "$(grep -v ' /root/.cache/rehome' <<<"$d")"
check "uninstall says where the data is" contains "$out" "/root/.cache/rehome"
out="$("$INSTALL" --purge --yes 2>&1)"
printf '%s\n' "$out"
snapshot /work/s2
d="$(snapdiff /work/s0 /work/s2)"
[[ -n "$d" ]] && printf 'left behind:\n%s\n' "$d"
check "after --purge the filesystem is exactly as before" test -z "$d"

finish
