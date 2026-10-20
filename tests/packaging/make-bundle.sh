#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build a relocatable Python 3.12 (+ fakeroot) from Ubuntu 24.04 packages into
# DIR (default tests/.cache/packaging/bundle). The Arch, Debian, Fedora and
# openSUSE test images have no python3, and their package mirrors may be
# unreachable, so the tests mount this bundle at /opt/rhtest to run the parts of
# install.sh and makepkg that need Python. It is test scaffolding only; real
# installs use the distribution's python3.

set -euo pipefail

ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="${1:-$ROOT/tests/.cache/packaging/bundle}"
IMAGE="${REHOME_TEST_IMAGE_UBUNTU:-mirror.gcr.io/library/ubuntu:24.04}"
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"

docker run --rm --network host -v "$DIR:/out" "$IMAGE" bash -euc '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends python3.12-minimal libpython3.12-stdlib fakeroot libexpat1 >/dev/null
    rm -rf /out/*
    mkdir -p /out/bin /out/lib /out/libfakeroot
    cp /usr/bin/python3.12 /out/bin/
    cp -a /usr/lib/python3.12 /out/lib/
    cp -L /usr/lib/x86_64-linux-gnu/libexpat.so.1 /out/lib/
    cp /usr/bin/faked-sysv /out/bin/
    cp /usr/lib/x86_64-linux-gnu/libfakeroot/libfakeroot-sysv.so /out/libfakeroot/
    sed -e "s|^FAKED=.*|FAKED=/opt/rhtest/bin/faked-sysv|" \
        -e "s|^PATHS=.*|PATHS=/opt/rhtest/libfakeroot|" /usr/bin/fakeroot-sysv >/out/bin/fakeroot
    chmod 0755 /out/bin/fakeroot
    printf "%s\n" "#!/bin/sh" \
        "LD_LIBRARY_PATH=/opt/rhtest/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH} exec /opt/rhtest/bin/python3.12 \"\$@\"" \
        >/out/python3
    chmod 0755 /out/python3
    chown -R '"$(id -u):$(id -g)"' /out
'
echo "$DIR"
