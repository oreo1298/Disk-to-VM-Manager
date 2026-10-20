#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build a source tarball of the working tree plus its SHA-256:
#
#   packaging/dist.sh [--outdir DIR] [--topdir NAME]
#
# Produces DIR/rehome-VERSION.tar.gz (top directory rehome-VERSION/ unless
# --topdir is given) and DIR/rehome-VERSION.tar.gz.sha256. Upload both to a
# GitHub release: install.sh downloads the tarball and verifies the checksum.
# Files come from `git ls-files` (tracked plus untracked-but-not-ignored) when
# run in a git checkout, so build artefacts and test images are left out.

set -euo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTDIR="$ROOT/dist"
TOPDIR=""
NAME=""

while (($#)); do
    case "$1" in
        --outdir) OUTDIR="$2"; shift ;;
        --topdir) TOPDIR="$2"; shift ;;
        --name) NAME="$2"; shift ;;
        -h|--help) sed -n '4,13p' "$0"; exit 0 ;;
        *) echo "dist.sh: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

PY="$(command -v python3)"
VERSION="$("$PY" "$ROOT/packaging/layout.py" version)"
TOPDIR="${TOPDIR:-rehome-$VERSION}"
NAME="${NAME:-rehome-$VERSION.tar.gz}"
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"

STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT
LIST="$STAGE/files"

# Only when ROOT is itself the top of a git checkout (not a copy that happens to
# sit inside another repository, where ls-files would list nothing).
if [[ "$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null)" == "$ROOT" ]]; then
    git -C "$ROOT" ls-files -z --cached --others --exclude-standard >"$LIST.raw"
else
    (cd "$ROOT" && find . -type f -not -path './.git/*' -not -path './dist/*' -not -path './build/*' \
        -not -path '*/__pycache__/*' -not -path './tests/.cache/*' -printf '%P\0') >"$LIST.raw"
fi
# Keep existing regular files only (deleted-but-tracked files are skipped).
: >"$LIST"
while IFS= read -r -d '' f; do
    [[ -f "$ROOT/$f" && ! -L "$ROOT/$f" ]] || continue
    case "$f" in dist/*|build/*|*/__pycache__/*|*.pyc) continue ;; esac
    printf '%s\0' "$f" >>"$LIST"
done <"$LIST.raw"

mkdir -p "$STAGE/tree/$TOPDIR"
tar -C "$ROOT" --null -T "$LIST" -cf - | tar -C "$STAGE/tree/$TOPDIR" -xf -

# Reproducible archive: fixed order, owner and timestamps.
EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}"
tar -C "$STAGE/tree" --sort=name --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner \
    --format=gnu -cf - "$TOPDIR" | gzip -9n >"$OUTDIR/$NAME"
(cd "$OUTDIR" && sha256sum "$NAME" >"$NAME.sha256")
echo "$OUTDIR/$NAME"
