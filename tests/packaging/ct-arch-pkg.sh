#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Runs as root inside an Arch Linux container:  ct-arch-pkg.sh /pkgs/arch/rehome-X.tar.gz
# The tarball has GitHub's tag-archive layout (Disk-to-VM-Manager-X/), so
# makepkg finds the source locally and needs no network. python and fakeroot
# come from the test bundle (the Arch mirrors are unreachable from here).
# 1. bash -n, makepkg --printsrcinfo == committed .SRCINFO
# 2. makepkg -d builds the package; its files == layout.py list
# 3. pacman -U, pacman -Qkk, rehome --version, pacman -R: nothing left
# 4. install.sh's removal on pacman (pacman -Rsu keeps what others need),
#    with dummy packages built by makepkg

set -uo pipefail
# shellcheck source=tests/packaging/lib.sh
. /src/tests/packaging/lib.sh
TARBALL="$1"
export NO_COLOR=1
mkdir -p /work /root/.cache /root/.config /root/.local/share /root/.local/state
use_bundle_python /usr/local/bin
ln -sf /opt/rhtest/python3 /usr/local/bin/python
ln -sf /opt/rhtest/bin/fakeroot /usr/local/bin/fakeroot
ln -sf /opt/rhtest/python3 /usr/bin/python3        # the package's #!/usr/bin/python3 scripts
useradd -m builder
# Arch's makepkg.conf enables debug packages, which needs debugedit (part of
# base-devel, not in this image and not downloadable here). There is nothing
# to strip in an arch=any package, so turn it off for this build user.
# shellcheck disable=SC2016  # written literally into makepkg's config
echo 'OPTIONS=("${OPTIONS[@]/#debug/!debug}")' >/home/builder/.makepkg.conf
chown builder: /home/builder/.makepkg.conf
B=/work/build
mkdir -p "$B"
cp /src/packaging/arch/PKGBUILD "$B/"
cp "$TARBALL" "$B/"
chown -R builder: "$B"

section "PKGBUILD"
check "bash -n PKGBUILD" bash -n "$B/PKGBUILD"
su builder -c "cd $B && makepkg --printsrcinfo" >/work/srcinfo 2>/work/srcinfo.err
cat /work/srcinfo.err
check "makepkg --printsrcinfo == packaging/arch/.SRCINFO" same_content /work/srcinfo /src/packaging/arch/.SRCINFO
su builder -c "cd $B && makepkg -d --noconfirm" >/work/makepkg.log 2>&1
rc=$?
cat /work/makepkg.log
check "makepkg -d (offline, local source) exit 0" test "$rc" -eq 0
pkgs=("$B"/rehome-*-any.pkg.tar.zst)
PKG="${pkgs[0]}"
check "package built: $(basename "${PKG:-none}")" test -f "$PKG"
check "package files = layout.py list (--prefix /usr --libexecdir /usr/lib/rehome)" same_files \
    <(bsdtar -tf "$PKG" | grep -v '^\.' | grep -v '/$' | sed 's|^|/|') \
    <(python3 /src/packaging/layout.py list --prefix /usr --libexecdir /usr/lib/rehome)
bsdtar -xOf "$PKG" .PKGINFO | grep -E '^(pkgname|pkgver|arch|depend|optdepend) ' | head -n 40

section "pacman -U / -Qkk / -R"
mkdir -p /work/s
snapshot /work/s0
pacman -U --noconfirm -dd "$PKG" >/work/pacman.log 2>&1
check "pacman -U exit 0" test $? -eq 0
check "pacman -Qkk rehome" pacman -Qkk rehome
ver="$(rehome --version 2>&1)"
check "rehome --version: $ver" test "$ver" = "rehome $(python3 /src/packaging/layout.py version)"
check "helper at /usr/lib/rehome/rehome-helper with #!/usr/bin/python3 -I" test "$(head -1 /usr/lib/rehome/rehome-helper)" = "#!/usr/bin/python3 -I"
pacman -R --noconfirm rehome >>/work/pacman.log 2>&1
check "pacman -R exit 0" test $? -eq 0
rm -rf /root/.cache/rehome
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
[[ -n "$d" ]] && printf 'left behind:\n%s\n' "$d"
check "nothing left behind" test -z "$d"

section "install.sh removal on pacman: -Rsu keeps what other packages need"
D=/work/dummy
mkdir -p "$D"
for spec in "dummy-base||dummy-cap" "dummy-solo||" "dummy-user|dummy-cap|"; do
    IFS='|' read -r name dep prov <<<"$spec"
    [[ -n "$dep" ]] && dep="'$dep'"
    [[ -n "$prov" ]] && prov="'$prov'"
    mkdir -p "$D/$name"
    cat >"$D/$name/PKGBUILD" <<EOF
pkgname=$name
pkgver=1
pkgrel=1
pkgdesc='test package'
arch=('any')
license=('MIT')
depends=($dep)
provides=($prov)
package() { :; }
EOF
done
chown -R builder: "$D"
for name in dummy-base dummy-solo dummy-user; do
    su builder -c "cd $D/$name && makepkg -d --noconfirm" >/dev/null 2>&1 || fail "makepkg $name"
done
pacman -U --noconfirm "$D"/dummy-base/*.pkg.tar.zst "$D"/dummy-solo/*.pkg.tar.zst "$D"/dummy-user/*.pkg.tar.zst >/dev/null 2>&1
check "dummy packages installed" pacman -Q dummy-base dummy-solo dummy-user
snapshot /work/s0
/src/install.sh --yes --skip-deps >/work/install.log 2>&1
check "file-only install" test $? -eq 0
python3 - <<'EOF'
import json
p = "/usr/local/share/rehome/install-manifest.json"
d = json.load(open(p))
d["packages"]["installed"] = ["dummy-base", "dummy-solo"]
json.dump(d, open(p, "w"), indent=2)
EOF
out="$(/src/install.sh --uninstall --yes 2>&1)"
rc=$?
printf '%s\n' "$out"
check "uninstall exit 0" test "$rc" -eq 0
check "prints the exact removal command" contains "$out" "\$ pacman -Rsu --noconfirm dummy-base dummy-solo"
check "dummy-solo removed" bash -c '! pacman -Q dummy-solo >/dev/null 2>&1'
check "dummy-base kept (dummy-user needs dummy-cap)" pacman -Q dummy-base
check "reports what was kept" contains "$out" "Still installed (needed by other software, or removal was declined): dummy-base"
rm -rf /root/.cache/rehome
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
[[ -n "$d" ]] && printf 'left behind:\n%s\n' "$d"
check "nothing left behind" test -z "$d"

finish
