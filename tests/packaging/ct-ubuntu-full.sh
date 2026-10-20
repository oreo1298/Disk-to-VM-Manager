#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Runs as root inside an Ubuntu 24.04 container with the Ubuntu archive
# reachable. A normal user ("tester", passwordless sudo) installs Rehome from a
# git checkout with real dependency installation, then updates, uninstalls
# and purges it. A logging sudo shim proves that every command run with sudo
# was printed (with a reason) before it ran.

set -uo pipefail
# shellcheck source=tests/packaging/lib.sh
. /src/tests/packaging/lib.sh
export DEBIAN_FRONTEND=noninteractive
KERNEL="/boot/vmlinuz-$(uname -r)"
M=/usr/local/share/rehome/install-manifest.json
mkdir -p /work

as_tester() { su - tester -c "export NO_COLOR=1; $1"; }
installed_pkgs() { dpkg-query -W -f='${db:Status-Status} ${Package}\n' | awk '$1 == "installed" {print $2}' | sort; }
mjson() { python3 -c "import json,sys; d=json.load(open('$M')); print($1)"; }

section "setup"
rm -f /etc/apt/apt.conf.d/docker-clean          # keep .debs in the shared cache volume
apt-get update -qq >/dev/null
apt-get install -y -qq sudo >/dev/null
useradd -m -s /bin/bash tester
echo 'tester ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/tester
cat >/usr/local/bin/sudo <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >>/work/sudo.log
exec /usr/bin/sudo "$@"
EOF
chmod 0755 /usr/local/bin/sudo
: >/work/sudo.log
chmod 0666 /work/sudo.log
if getent group kvm >/dev/null; then note "kvm group exists in the image"
else groupadd --system kvm; note "created the kvm group (as on a desktop system with udev)"; fi
mkdir -p /boot
printf 'not a real kernel\n' >"$KERNEL"
chmod 0600 "$KERNEL"
note "fake kernel image $KERNEL with mode 0600 (the container has no /boot)"
tar -C /src --exclude=./tests/.cache --exclude=./.git -cf - . | (mkdir -p /home/tester/rehome && tar -C /home/tester/rehome -xf -)
chown -R tester:tester /home/tester/rehome
as_tester 'mkdir -p ~/.local/share/rehome/vms/demo ~/.config/rehome && echo "name = \"demo\"" > ~/.local/share/rehome/vms/demo/vm.toml && echo "update_check = false" > ~/.config/rehome/settings.toml'
DATA_SUM="$(cat /home/tester/.local/share/rehome/vms/demo/vm.toml /home/tester/.config/rehome/settings.toml | sha256sum)"
installed_pkgs >/work/pkgs-before
check "python3 is not installed before" test -z "$(grep -x python3 /work/pkgs-before)"
snapshot /work/s0

section "install (./install.sh --yes, as tester)"
as_tester 'cd ~/rehome && ./install.sh --yes' >/work/install.log 2>&1
rc=$?
cat /work/install.log
check "install exit 0" test "$rc" -eq 0
out="$(cat /work/install.log)"
check "python3 bootstrapped with a printed sudo command" contains "$out" "\$ sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3"
missing=0
while IFS= read -r line; do
    grep -qxF -- "    \$ sudo $line" /work/install.log || { missing=$((missing + 1)); echo "not printed first: sudo $line"; }
done </work/sudo.log
check "every one of $(wc -l </work/sudo.log) sudo commands was printed before it ran" test "$missing" -eq 0
check "every sudo command came with a reason" test "$(grep -c '^    Needs root: ' /work/install.log)" -ge "$(wc -l </work/sudo.log)"
check "manifest written" test -f "$M"
check "manifest version" test "$(mjson 'd["version"]')" = "$(python3 /src/packaging/layout.py version)"
mapfile -t added < <(mjson '"\n".join(d["packages"]["installed"])')
check "manifest records ${#added[@]} packages, including python3 and qemu-system-x86" \
    bash -c 'printf "%s\n" "$@" | grep -qx python3 && printf "%s\n" "$@" | grep -qx qemu-system-x86' _ "${added[@]}"
bad=0
for p in "${added[@]}"; do
    grep -qx "$p" /work/pkgs-before && { bad=1; echo "recorded but was already installed: $p"; }
    [[ "$(dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null)" == installed ]] || { bad=1; echo "recorded but not installed: $p"; }
done
check "recorded packages = installed by the installer, none pre-existing" test "$bad" -eq 0
check "manifest records the kvm group" test "$(mjson '[g["user"]+":"+g["group"] for g in d["groups"]]')" = "['tester:kvm']"
check "tester is in the kvm group" bash -c 'id -nG tester | tr " " "\n" | grep -qx kvm'
check "manifest records the statoverride with the old mode" test "$(mjson '[(o["path"], o["previous_mode"]) for o in d["statoverrides"]]')" = "[('$KERNEL', '600')]"
check "kernel statoverride active" dpkg-statoverride --list "$KERNEL"
check "kernel image readable (0644)" test "$(stat -c %a "$KERNEL")" = 644
mapfile -t files < <(mjson '"\n".join(d["files"])')
nf=0
for f in "${files[@]}"; do [[ -f "$f" ]] || { nf=$((nf + 1)); echo "missing $f"; }; done
check "all ${#files[@]} manifest files exist" test "$nf" -eq 0
check "polkit policy is well-formed XML" python3 -c 'import sys, xml.dom.minidom as m; m.parse(sys.argv[1])' /usr/share/polkit-1/actions/io.github.oreo1298.Rehome.policy
check "rehome --version as tester" test "$(as_tester 'rehome --version')" = "rehome $(python3 /src/packaging/layout.py version)"
check "rehome doctor --quick ran" contains "$out" "Rehome host check"
check "summary tells the user to log out for the kvm group" contains "$out" "Log out and back in"

section "interactive dry run through a pseudo-terminal (optional components 1 and 5)"
dry="$(printf '1 5\n' | su - tester -c 'cd ~/rehome && NO_COLOR=1 script -qec "./install.sh --dry-run" /dev/null' 2>&1)"
printf '%s\n' "$dry"
check "asked which optional components to install" contains "$dry" "Install which? [a]ll, [n]one"
check "installs exactly the chosen ones (qemu-system-arm, passt)" contains "$dry" "\$ sudo apt-get install -y qemu-system-arm passt"
check "required packages already present" contains "$dry" "Already installed:"

section "update to 0.1.1 (one profile removed, one added)"
as_tester 'cp -a ~/rehome ~/rehome-next && sed -i "s/^__version__ = .*/__version__ = \"0.1.1\"/" ~/rehome-next/rehome/__init__.py && echo "# new" > ~/rehome-next/data/profiles/zz-test.toml && rm ~/rehome-next/data/profiles/generic.toml'
: >/work/sudo.log
as_tester 'cd ~/rehome && REHOME_SOURCE=~/rehome-next ./install.sh --update --yes' >/work/update.log 2>&1
rc=$?
cat /work/update.log
check "update exit 0" test "$rc" -eq 0
check "manifest version 0.1.1" test "$(mjson 'd["version"]')" = 0.1.1
check "rehome --version 0.1.1" test "$(as_tester 'rehome --version')" = "rehome 0.1.1"
check "new profile installed" test -f /usr/local/share/rehome/profiles/zz-test.toml
check "removed profile pruned" test ! -e /usr/local/share/rehome/profiles/generic.toml
check "packages record kept" test "$(mjson 'len(d["packages"]["installed"])')" = "${#added[@]}"
check "VMs and settings untouched" test "$(cat /home/tester/.local/share/rehome/vms/demo/vm.toml /home/tester/.config/rehome/settings.toml | sha256sum)" = "$DATA_SUM"
missing=0
while IFS= read -r line; do grep -qxF -- "    \$ sudo $line" /work/update.log || missing=$((missing + 1)); done </work/sudo.log
check "update: every sudo command printed first" test "$missing" -eq 0
out="$(as_tester 'cd ~/rehome && REHOME_SOURCE=~/rehome-next ./install.sh --update --yes' 2>&1)"
check "second update says already up to date" contains "$out" "Rehome 0.1.1 is already up to date."
out="$(as_tester 'cd ~/rehome && ./install.sh --update --yes' 2>&1)"
rc=$?
check "update to an older source is refused" bash -c "test $rc -ne 0 && grep -q 'older than the installed 0.1.1' <<<\"\$1\"" _ "$out"

section "uninstall (installed copy of install.sh, as tester)"
mapfile -t files < <(mjson '"\n".join(d["files"])')
mapfile -t dirs < <(mjson '"\n".join(d["directories"])')
: >/work/sudo.log
as_tester '/usr/local/share/rehome/installer/install.sh --uninstall --yes' >/work/uninstall.log 2>&1
rc=$?
cat /work/uninstall.log
check "uninstall exit 0" test "$rc" -eq 0
left=0
for f in "${files[@]}"; do [[ -e "$f" ]] && { left=$((left + 1)); echo "left: $f"; }; done
check "all ${#files[@]} manifest files removed" test "$left" -eq 0
for d in /usr/local/lib/rehome /usr/local/share/rehome /usr/local/libexec/rehome "${dirs[@]}"; do
    [[ -e "$d" ]] && { left=$((left + 1)); echo "left dir: $d"; }
done
check "Rehome's directories and the ones the installer created are gone" test "$left" -eq 0
still=0
for p in "${added[@]}"; do
    [[ "$(dpkg-query -W -f='${db:Status-Status}' "$p" 2>/dev/null)" == installed ]] && { still=$((still + 1)); echo "still installed: $p"; }
done
check "all ${#added[@]} packages the installer added were removed" test "$still" -eq 0
installed_pkgs >/work/pkgs-after
check "installed package set is exactly as before the install" cmp -s /work/pkgs-before /work/pkgs-after
check "tester removed from the kvm group" bash -c '! id -nG tester | tr " " "\n" | grep -qx kvm'
check "statoverride removed" bash -c "! dpkg-statoverride --list '$KERNEL' >/dev/null"
check "kernel mode restored to 0600" test "$(stat -c %a "$KERNEL")" = 600
check "VMs and settings kept" test "$(cat /home/tester/.local/share/rehome/vms/demo/vm.toml /home/tester/.config/rehome/settings.toml | sha256sum)" = "$DATA_SUM"
out="$(cat /work/uninstall.log)"
check "says where the VMs are" contains "$out" "/home/tester/.local/share/rehome"
missing=0
while IFS= read -r line; do grep -qxF -- "    \$ sudo $line" /work/uninstall.log || { missing=$((missing + 1)); echo "not printed: $line"; }; done </work/sudo.log
check "uninstall: every one of $(wc -l </work/sudo.log) sudo commands printed first" test "$missing" -eq 0

section "purge (no installation left: deletes the data only)"
out="$(as_tester 'cd ~/rehome && ./install.sh --purge --yes' 2>&1)"
printf '%s\n' "$out"
check "reports no installation" contains "$out" "Rehome is not installed under /usr/local"
check "user data deleted" bash -c '! ls -d /home/tester/.local/share/rehome /home/tester/.config/rehome /home/tester/.cache/rehome 2>/dev/null'

section "filesystem compared with before the install"
rm -rf /home/tester/rehome-next
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
printf '%s\n' "$d"
note "$(grep -c '^[+-]' <<<"$d") paths differ; these are left by the distribution packages' own maintainer scripts (dpkg keeps them after removal, e.g. new system users' directories), not by Rehome"
check "nothing of Rehome's is left" test -z "$(grep -i rehome <<<"$d")"
check "nothing left under /usr/local" test -z "$(grep ' /usr/local' <<<"$d")"

finish
