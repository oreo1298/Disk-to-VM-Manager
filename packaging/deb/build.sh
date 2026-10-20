#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build rehome_<version>_all.deb from the working tree with dpkg-deb:
#
#   packaging/deb/build.sh [--outdir DIR]       (default: dist/)
#
# The files come from `layout.py install --destdir` (PREFIX=/usr,
# LIBEXECDIR=/usr/libexec/rehome); Depends/Recommends come from the "debian"
# entries of data/packages.toml (required -> Depends, optional -> Recommends).
# Needs: python3 >= 3.11, dpkg-deb (>= 1.19 for --root-owner-group), gzip.

set -euo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTDIR="$ROOT/dist"
while (($#)); do
    case "$1" in
        --outdir) OUTDIR="$2"; shift ;;
        -h|--help) sed -n '4,11p' "$0"; exit 0 ;;
        *) echo "build.sh: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

PY="$(command -v python3)"
LAYOUT=("$PY" "$ROOT/packaging/layout.py")
VERSION="$("${LAYOUT[@]}" version)"
# Debian versions may not contain '-' without a revision; map PEP 440-ish
# pre-releases (0.2.0rc1) to sort before the release (0.2.0~rc1).
DEB_VERSION="$(sed -E 's/^([0-9.]+)[.-]?((a|b|rc|alpha|beta|dev)[0-9.]*)$/\1~\2/' <<<"$VERSION")"

STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT
PKG="$STAGE/rehome"

"${LAYOUT[@]}" install --prefix /usr --libexecdir /usr/libexec/rehome --python /usr/bin/python3 \
    --destdir "$PKG" --quiet

# Debian policy: copyright file in /usr/share/doc/<package>.
install -d -m 0755 "$PKG/usr/share/doc/rehome"
cat >"$PKG/usr/share/doc/rehome/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Rehome
Source: https://github.com/oreo1298/Disk-to-VM-Manager

Files: *
Copyright: Rehome contributors
License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation, either version 3 of the License, or (at your option) any later
 version.
 .
 On Debian systems, the complete text of the GNU General Public License
 version 3 can be found in "/usr/share/common-licenses/GPL-3".
EOF
chmod 0644 "$PKG/usr/share/doc/rehome/copyright"

deps() {   # --required|--optional → "a, b, c" with python3 versioned
    "${LAYOUT[@]}" deps --family debian "$1" --format lines \
        | sed -e 's/^python3$/python3 (>= 3.11)/' | paste -sd, - | sed 's/,/, /g'
}
DEPENDS="$(deps --required)"
RECOMMENDS="$(deps --optional)"
SIZE_KB="$(du -sk --apparent-size "$PKG/usr" | cut -f1)"

install -d -m 0755 "$PKG/DEBIAN"
sed -e "s|@VERSION@|$DEB_VERSION|" -e "s|@DEPENDS@|$DEPENDS|" -e "s|@RECOMMENDS@|$RECOMMENDS|" \
    -e "s|@INSTALLED_SIZE@|$SIZE_KB|" "$ROOT/packaging/deb/control.in" >"$PKG/DEBIAN/control"
(cd "$PKG" && find usr -type f -print0 | sort -z | xargs -0 md5sum) >"$PKG/DEBIAN/md5sums"
chmod 0644 "$PKG/DEBIAN/control" "$PKG/DEBIAN/md5sums"

mkdir -p "$OUTDIR"
OUT="$OUTDIR/rehome_${DEB_VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT" >/dev/null
echo "$OUT"
