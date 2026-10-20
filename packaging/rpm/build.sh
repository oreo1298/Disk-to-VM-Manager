#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build the Rehome RPM (noarch) from the working tree with rpmbuild:
#
#   packaging/rpm/build.sh [--outdir DIR] [--suse] [--srpm]
#
#   --suse   evaluate the spec for openSUSE (suse_version=1600): openSUSE package
#            names in Requires; the file gets a ".suse" release suffix
#   --srpm   also write the source RPM (for COPR or the Open Build Service)
#
# The source tarball is made by packaging/dist.sh with the same top directory
# as GitHub's tag archives, so the spec builds unchanged from a release tag.
# On hosts that are not RPM-based (e.g. Ubuntu with the "rpm" package) the
# BuildRequires cannot be checked against the RPM database, so --nodeps is used.

set -euo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTDIR="$ROOT/dist"
SUSE=0
SRPM=0
while (($#)); do
    case "$1" in
        --outdir) OUTDIR="$2"; shift ;;
        --suse) SUSE=1 ;;
        --srpm) SRPM=1 ;;
        -h|--help) sed -n '4,15p' "$0"; exit 0 ;;
        *) echo "build.sh: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

command -v rpmbuild >/dev/null 2>&1 || { echo "rpmbuild is not installed (Debian/Ubuntu: apt install rpm; Fedora: dnf install rpm-build)" >&2; exit 1; }
SPEC="$ROOT/packaging/rpm/rehome.spec"
VERSION="$(python3 "$ROOT/packaging/layout.py" version)"
SPEC_VERSION="$(sed -n 's/^Version:[[:space:]]*//p' "$SPEC")"
if [[ "$VERSION" != "$SPEC_VERSION" ]]; then
    echo "rehome.spec has Version $SPEC_VERSION but rehome/__init__.py says $VERSION; update the spec (and its %changelog)." >&2
    exit 1
fi

TOP="$(mktemp -d)"
trap 'rm -rf -- "$TOP"' EXIT
mkdir -p "$TOP"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
"$ROOT/packaging/dist.sh" --outdir "$TOP/SOURCES" --topdir "Disk-to-VM-Manager-$VERSION" \
    --name "rehome-$VERSION.tar.gz" >/dev/null

args=(--define "_topdir $TOP")
if ((SUSE)); then
    args+=(--define "suse_version 1600" --define "dist .suse")
fi
if ! rpm -q rpm >/dev/null 2>&1; then
    args+=(--nodeps)
fi
mode=-bb
((SRPM)) && mode=-ba
rpmbuild "$mode" "${args[@]}" "$SPEC" >"$TOP/build.log" 2>&1 || { cat "$TOP/build.log" >&2; exit 1; }

mkdir -p "$OUTDIR"
for f in "$TOP"/RPMS/noarch/*.rpm "$TOP"/SRPMS/*.rpm; do
    [[ -f "$f" ]] || continue
    cp -- "$f" "$OUTDIR/"
    echo "$OUTDIR/$(basename -- "$f")"
done
