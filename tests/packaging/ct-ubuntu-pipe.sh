#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Runs as root inside an Ubuntu 24.04 container:  ct-ubuntu-pipe.sh BASE_URL
# BASE_URL serves (tests/packaging/run.sh starts it on the host):
#   install.sh, release-v1.json / release-v2.json / release-bad.json (GitHub
#   "latest release" API answers whose assets point at tarballs + .sha256).
# Covers the one-liner `curl ... | bash` as root with real dependency
# installation (including --with-optional), checksum verification, --update
# through the release API, --purge (packages, files and data), a tampered
# checksum and the fallback to the main branch when no release exists.

set -uo pipefail
# shellcheck source=tests/packaging/lib.sh
. /src/tests/packaging/lib.sh
BASE="$1"
export DEBIAN_FRONTEND=noninteractive NO_COLOR=1
M=/usr/local/share/rehome/install-manifest.json
mkdir -p /work /root/.cache /root/.config /root/.local/share /root/.local/state

installed_pkgs() { dpkg-query -W -f='${db:Status-Status} ${Package}\n' | awk '$1 == "installed" {print $2}' | sort; }
mjson() { python3 -c "import json,sys; d=json.load(open('$M')); print($1)"; }
pipe() {   # pipe RELEASE_JSON ARGS...: curl BASE/install.sh | bash -s -- ARGS
    local rel="$1"
    shift
    curl -fsSL "$BASE/install.sh" | REHOME_RELEASES_API="$BASE/$rel" bash -s -- "$@"
}

section "setup"
rm -f /etc/apt/apt.conf.d/docker-clean
apt-get update -qq >/dev/null
apt-get install -y -qq curl >/dev/null
installed_pkgs >/work/pkgs-before
snapshot /work/s0

section "install: curl | bash -s -- --yes --with-optional (latest release v0.1.0)"
out="$(pipe release-v1.json --yes --with-optional 2>&1)"
rc=$?
printf '%s\n' "$out"
check "exit 0" test "$rc" -eq 0
check "looked up the latest release" contains "$out" "Looking up the latest release ($BASE/release-v1.json)"
check "SHA-256 verified" contains "$out" "SHA-256 verified"
check "manifest source is the release" contains "$(mjson 'd["source"]')" "release v0.1.0"
check "version 0.1.0" test "$(/usr/local/bin/rehome --version)" = "rehome 0.1.0"
pk="$(mjson '" ".join(d["packages"]["installed"])')"
# shellcheck disable=SC2016
check "optional packages recorded (passt, virtiofsd, virt-v2v)" bash -c 'for p in passt virtiofsd virt-v2v python3; do [[ " $1 " == *" $p "* ]] || exit 1; done' _ "$pk"
check "root is not added to the kvm group" test "$(mjson 'd["groups"]')" = "[]"
check "doctor ran" contains "$out" "Rehome host check"
check "the installer keeps a copy of itself" test -x /usr/local/share/rehome/installer/install.sh

section "update through the release API (v0.1.1)"
out="$(REHOME_RELEASES_API="$BASE/release-v2.json" /usr/local/share/rehome/installer/install.sh --update --yes 2>&1)"
rc=$?
printf '%s\n' "$out"
check "exit 0" test "$rc" -eq 0
check "SHA-256 verified" contains "$out" "SHA-256 verified"
check "version 0.1.1" test "$(/usr/local/bin/rehome --version)" = "rehome 0.1.1"
check "packages record kept" test "$(mjson '" ".join(d["packages"]["installed"])')" = "$pk"
out="$(pipe release-v2.json --update --yes 2>&1)"
check "one-liner --update: already up to date" contains "$out" "Rehome 0.1.1 is already up to date."

section "purge with the one-liner"
mapfile -t files < <(mjson '"\n".join(d["files"])')
out="$(pipe release-v2.json --purge --yes 2>&1)"
rc=$?
printf '%s\n' "$out"
check "exit 0" test "$rc" -eq 0
left=0
for f in "${files[@]}"; do [[ -e "$f" ]] && { left=$((left + 1)); echo "left: $f"; }; done
check "all ${#files[@]} files removed" test "$left" -eq 0
check "packages removed with apt-get autoremove --purge" contains "$out" "\$ env DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y"
installed_pkgs >/work/pkgs-after
check "installed package set is exactly as before" cmp -s /work/pkgs-before /work/pkgs-after
check "root's Rehome data deleted" bash -c '! ls -d /root/.cache/rehome /root/.local/share/rehome 2>/dev/null'
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
printf '%s\n' "$d"
note "$(grep -c '^[+-]' <<<"$d") paths differ from before the install (left by the distribution packages' maintainer scripts)"
check "nothing of Rehome's is left" test -z "$(grep -i rehome <<<"$d")"

section "tampered release: checksum mismatch"
out="$(pipe release-bad.json --yes --skip-deps 2>&1)"
rc=$?
printf '%s\n' "$out"
check "fails" test "$rc" -ne 0
check "reports the SHA-256 mismatch" contains "$out" "SHA-256 mismatch for rehome-0.1.0.tar.gz"
check "nothing installed" test ! -e /usr/local/share/rehome
check "tells what it changed before stopping (python3 bootstrap)" contains "$out" "packages installed: python3"

section "no release published: falls back to the main branch"
out="$(REHOME_RELEASES_API="$BASE/no-such-release.json" REHOME_MAIN_TARBALL="$BASE/main.tar.gz" \
    bash -c "curl -fsSL '$BASE/install.sh' | bash -s -- --dry-run --yes --skip-deps" 2>&1)"
printf '%s\n' "$out"
check "warns and uses the main branch" contains "$out" "No published release was found; installing the development version from the main branch."
check "dry run installs nothing" test ! -e /usr/local/share/rehome

finish
