#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Packaging and install.sh tests in containers.
#
#   tests/packaging/run.sh [TEST...]
#
# Tests (default: all):
#   lint           shellcheck install.sh, the build scripts, these tests, PKGBUILD
#   detect         install.sh --detect for sample os-release files (no Docker)
#   ubuntu-full    Ubuntu 24.04: install as a sudo user from a checkout with real
#                  dependencies, update, uninstall, purge (needs the Ubuntu archive)
#   ubuntu-pipe    Ubuntu 24.04: curl | bash as root from a mock GitHub release,
#                  --with-optional, checksum, --update, --purge, tampered checksum
#   deb            build the .deb, apt install it, rehome doctor, apt purge
#   rpm-fedora     build the .rpm, rpm -i/-V/-e on Fedora, package removal logic
#   rpm-suse       the same on openSUSE Tumbleweed (spec built with suse_version)
#   arch-pkg       makepkg the PKGBUILD offline on Arch, pacman -U/-R, .SRCINFO
#   distro-arch distro-debian distro-fedora distro-suse
#                  detection, exact --dry-run package commands, failing package
#                  installs (mirrors blocked here), file-only round trips
#
# Skips (exit 0) when Docker is not available. Logs: tests/.cache/packaging/logs.
# Images can be overridden with REHOME_TEST_IMAGE_{UBUNTU,DEBIAN,FEDORA,SUSE,ARCH}.

set -uo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
CACHE="$ROOT/tests/.cache/packaging"
LOGS="$CACHE/logs"
PKGS="$CACHE/pkgs"
SRV="$CACHE/srv"
BUNDLE="$CACHE/bundle"
SNAP="$CACHE/snapshot/src"     # the source tree every test uses (see snapshot_source)
IMG_UBUNTU="${REHOME_TEST_IMAGE_UBUNTU:-mirror.gcr.io/library/ubuntu:24.04}"
IMG_DEBIAN="${REHOME_TEST_IMAGE_DEBIAN:-mirror.gcr.io/library/debian:trixie}"
IMG_FEDORA="${REHOME_TEST_IMAGE_FEDORA:-mirror.gcr.io/library/fedora:latest}"
IMG_SUSE="${REHOME_TEST_IMAGE_SUSE:-mirror.gcr.io/opensuse/tumbleweed:latest}"
IMG_ARCH="${REHOME_TEST_IMAGE_ARCH:-mirror.gcr.io/library/archlinux:latest}"
APT_CACHE_VOLUME="rehome-test-apt-cache"
ALL=(lint detect distro-arch distro-debian distro-fedora distro-suse arch-pkg rpm-fedora rpm-suse
     deb ubuntu-pipe ubuntu-full)

RESULTS=()
FAILED=0
SERVER_PID=""
mkdir -p "$LOGS"

record() {   # NAME STATUS DETAIL
    RESULTS+=("$(printf '%-14s %-5s %s' "$1" "$2" "$3")")
    [[ "$2" == "FAIL" ]] && FAILED=$((FAILED + 1))
    printf '%-14s %-5s %s\n' "$1" "$2" "$3"
}

next_version() {   # 0.1.1 -> 0.1.2
    python3 -c 'import sys; p = sys.argv[1].split("."); p[-1] = str(int(p[-1]) + 1); print(".".join(p))' "$1"
}

have_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

# ct NAME IMAGE SCRIPT ARGS...: run tests/packaging/SCRIPT inside IMAGE.
ct() {
    local name="$1" image="$2" script="$3"
    shift 3
    local -a vols=(-v "$SNAP:/src:ro" -v "$BUNDLE:/opt/rhtest:ro" -v "$PKGS:/pkgs:ro")
    [[ "$image" == "$IMG_UBUNTU" ]] && vols+=(-v "$APT_CACHE_VOLUME:/var/cache/apt/archives")
    local log="$LOGS/$name.log" start=$SECONDS
    local v1
    v1="$(python3 "$SNAP/packaging/layout.py" version)"
    docker run --rm --network host "${vols[@]}" -e "REHOME_V1=$v1" -e "REHOME_V2=$(next_version "$v1")" \
        "$image" bash "/src/tests/packaging/$script" "$@" >"$log" 2>&1
    local rc=$? summary
    summary="$(grep -E '^RESULT:' "$log" | tail -n 1)"
    if ((rc == 0)) && [[ -n "$summary" ]]; then
        record "$name" PASS "${summary#RESULT: } in $((SECONDS - start))s ($log)"
    else
        record "$name" FAIL "${summary:-no result, exit $rc} in $((SECONDS - start))s ($log)"
        grep -E '^FAIL:' "$log" | sed 's/^/    /'
    fi
}

ensure_bundle() {
    [[ -x "$BUNDLE/python3" && -x "$BUNDLE/bin/python3.12" ]] && return 0
    echo "Building the test Python bundle from $IMG_UBUNTU ..."
    "$ROOT/tests/packaging/make-bundle.sh" "$BUNDLE" >"$LOGS/bundle.log" 2>&1
}

# The repository may change while the tests run (other work in progress), so
# every package is built from, and every container sees, one snapshot of it:
# the files `git ls-files` lists (tracked + untracked-not-ignored).
snapshot_source() {
    rm -rf "$CACHE/snapshot"
    mkdir -p "$CACHE/snapshot"
    "$ROOT/packaging/dist.sh" --outdir "$CACHE/snapshot" --topdir src --name src.tar.gz >/dev/null &&
        tar -C "$CACHE/snapshot" -xzf "$CACHE/snapshot/src.tar.gz"
}

BUILT=0
build_packages() {
    ((BUILT)) && return 0
    BUILT=1
    local ver
    {
        snapshot_source || return 1
        ver="$(python3 "$SNAP/packaging/layout.py" version)"
        rm -rf "$PKGS"
        mkdir -p "$PKGS/arch" "$PKGS/dummy"
        "$SNAP/packaging/deb/build.sh" --outdir "$PKGS"
        if command -v rpmbuild >/dev/null 2>&1; then
            "$SNAP/packaging/rpm/build.sh" --outdir "$PKGS"
            "$SNAP/packaging/rpm/build.sh" --outdir "$PKGS" --suse
            build_dummy_rpms "$PKGS/dummy"
        fi
        "$SNAP/packaging/dist.sh" --outdir "$PKGS/arch" --topdir "Disk-to-VM-Manager-$ver" --name "rehome-$ver.tar.gz"
    } >"$LOGS/build.log" 2>&1 || { cat "$LOGS/build.log"; return 1; }
}

build_dummy_rpms() {   # OUTDIR: tiny packages to test the reverse-dependency logic
    local out="$1" top spec
    top="$(mktemp -d)"
    for spec in "dummy-base||dummy-cap" "dummy-solo||" "dummy-y||" "dummy-x|dummy-y|" "dummy-user|dummy-cap dummy-x|"; do
        local name dep prov
        IFS='|' read -r name dep prov <<<"$spec"
        {
            printf 'Name: %s\nVersion: 1\nRelease: 1\nSummary: test\nLicense: MIT\nBuildArch: noarch\n' "$name"
            [[ -n "$dep" ]] && printf 'Requires: %s\n' "$dep"
            [[ -n "$prov" ]] && printf 'Provides: %s\n' "$prov"
            printf '%%description\ntest\n%%files\n'
        } >"$top/$name.spec"
        rpmbuild -bb --nodeps --define "_topdir $top" "$top/$name.spec" >/dev/null
    done
    cp "$top"/RPMS/noarch/*.rpm "$out/"
    rm -rf "$top"
}

start_release_server() {   # mock GitHub: release JSON + assets, served on 127.0.0.1
    local ver ver2 port v2dir
    ver="$(python3 "$SNAP/packaging/layout.py" version)"
    ver2="$(next_version "$ver")"
    rm -rf "$SRV"
    mkdir -p "$SRV/v1" "$SRV/v2" "$SRV/bad"
    cp "$SNAP/install.sh" "$SRV/install.sh"
    "$SNAP/packaging/dist.sh" --outdir "$SRV/v1" >/dev/null
    v2dir="$(mktemp -d)"
    tar -C "$v2dir" -xzf "$SRV/v1/rehome-$ver.tar.gz"
    sed -i "s/^__version__ = .*/__version__ = \"$ver2\"/" "$v2dir/rehome-$ver/rehome/__init__.py"
    mv "$v2dir/rehome-$ver" "$v2dir/rehome-$ver2"
    tar -C "$v2dir" -czf "$SRV/v2/rehome-$ver2.tar.gz" "rehome-$ver2"
    (cd "$SRV/v2" && sha256sum "rehome-$ver2.tar.gz" >"rehome-$ver2.tar.gz.sha256")
    rm -rf "$v2dir"
    cp "$SRV/v1/rehome-$ver.tar.gz" "$SRV/bad/"
    printf '%064d  rehome-%s.tar.gz\n' 0 "$ver" >"$SRV/bad/rehome-$ver.tar.gz.sha256"
    cp "$SRV/v1/rehome-$ver.tar.gz" "$SRV/main.tar.gz"
    port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
    BASE="http://127.0.0.1:$port"
    release_json "v$ver" "$BASE/v1/rehome-$ver.tar.gz" >"$SRV/release-v1.json"
    release_json "v$ver2" "$BASE/v2/rehome-$ver2.tar.gz" >"$SRV/release-v2.json"
    release_json "v$ver" "$BASE/bad/rehome-$ver.tar.gz" >"$SRV/release-bad.json"
    python3 -m http.server --bind 127.0.0.1 --directory "$SRV" "$port" >"$LOGS/http.log" 2>&1 &
    SERVER_PID=$!
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        curl -fsS -o /dev/null --noproxy '*' "$BASE/install.sh" 2>/dev/null && return 0
        sleep 0.5
    done
    echo "mock release server did not start" >&2
    return 1
}

release_json() {   # TAG TARBALL_URL
    python3 -c '
import json, sys
tag, url = sys.argv[1], sys.argv[2]
name = url.rsplit("/", 1)[1]
print(json.dumps({"tag_name": tag, "html_url": "https://github.com/oreo1298/Disk-to-VM-Manager/releases/tag/" + tag,
                  "assets": [{"name": name, "browser_download_url": url},
                             {"name": name + ".sha256", "browser_download_url": url + ".sha256"}]}))' "$1" "$2"
}

cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null; return 0; }
trap cleanup EXIT

t_shellcheck() {
    if ! command -v shellcheck >/dev/null 2>&1; then record lint SKIP "shellcheck is not installed"; return; fi
    local log="$LOGS/shellcheck.log"
    if (cd "$ROOT" && shellcheck -x install.sh packaging/dist.sh packaging/deb/build.sh packaging/rpm/build.sh \
            tests/packaging/*.sh && shellcheck -s bash -e SC2034,SC2154,SC2164 packaging/arch/PKGBUILD) >"$log" 2>&1; then
        record lint PASS "install.sh, build scripts, tests, PKGBUILD"
    else
        record lint FAIL "see $log"
    fi
}

t_detect() {
    local tmp out bad=0 spec id like fam
    tmp="$(mktemp)"
    for spec in arch::arch manjaro:arch:arch endeavouros:arch:arch garuda:arch:arch cachyos:arch:arch \
        debian::debian ubuntu:debian:debian "linuxmint:ubuntu debian:debian" "pop:ubuntu debian:debian" \
        fedora::fedora "rocky:rhel centos fedora:fedora" "opensuse-tumbleweed:opensuse suse:suse" \
        "opensuse-leap:suse opensuse:suse" sles::suse gentoo::unknown nixos::unknown "void::unknown"; do
        IFS=: read -r id like fam <<<"$spec"
        printf 'ID="%s"\nID_LIKE="%s"\nPRETTY_NAME="Test %s"\n' "$id" "$like" "$id" >"$tmp"
        out="$(REHOME_OS_RELEASE="$tmp" bash "$ROOT/install.sh" --detect)"
        grep -qx "family=$fam" <<<"$out" || { bad=$((bad + 1)); echo "detect $id ($like): got $(grep family= <<<"$out"), want $fam"; }
    done
    rm -f "$tmp"
    if ((bad)); then record detect FAIL "$bad wrong"; else record detect PASS "17 os-release samples"; fi
}

run_test() {
    case "$1" in
        lint) t_shellcheck ;;
        detect) t_detect ;;
        *)
            if ! have_docker; then record "$1" SKIP "Docker is not available"; return; fi
            ensure_bundle || { record "$1" FAIL "could not build the Python bundle ($LOGS/bundle.log)"; return; }
            build_packages || { record "$1" FAIL "package build failed ($LOGS/build.log)"; return; }
            local ver
            ver="$(python3 "$SNAP/packaging/layout.py" version)"
            case "$1" in
                distro-arch) ct "$1" "$IMG_ARCH" ct-distro.sh arch pacman ;;
                distro-debian) ct "$1" "$IMG_DEBIAN" ct-distro.sh debian apt ;;
                distro-fedora) ct "$1" "$IMG_FEDORA" ct-distro.sh fedora dnf ;;
                distro-suse) ct "$1" "$IMG_SUSE" ct-distro.sh suse zypper ;;
                arch-pkg) ct "$1" "$IMG_ARCH" ct-arch-pkg.sh "/pkgs/arch/rehome-$ver.tar.gz" ;;
                rpm-fedora) ct "$1" "$IMG_FEDORA" ct-rpm.sh fedora dnf "/pkgs/rehome-$ver-1.noarch.rpm" ;;
                rpm-suse) ct "$1" "$IMG_SUSE" ct-rpm.sh suse zypper "/pkgs/rehome-$ver-1.suse.noarch.rpm" ;;
                deb) ct "$1" "$IMG_UBUNTU" ct-deb.sh "/pkgs/rehome_${ver}_all.deb" ;;
                ubuntu-full) ct "$1" "$IMG_UBUNTU" ct-ubuntu-full.sh ;;
                ubuntu-pipe)
                    if [[ -z "$SERVER_PID" ]]; then
                        start_release_server || { record "$1" FAIL "mock release server"; return; }
                    fi
                    ct "$1" "$IMG_UBUNTU" ct-ubuntu-pipe.sh "$BASE" ;;
                *) echo "unknown test: $1 (see --help)" >&2; FAILED=$((FAILED + 1)) ;;
            esac ;;
    esac
}

case "${1:-}" in -h|--help) sed -n '4,25p' "$0"; exit 0 ;; esac
tests=("$@")
((${#tests[@]})) || tests=("${ALL[@]}")
if ! have_docker; then
    echo "SKIP: Docker is not available; only the shellcheck and detect tests run."
fi
for t in "${tests[@]}"; do run_test "$t"; done

echo
echo "Summary:"
printf '  %s\n' "${RESULTS[@]}"
((FAILED == 0))
