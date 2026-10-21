#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Runs as root inside an Ubuntu 24.04 container:  ct-deb.sh /pkgs/rehome_X_all.deb
# Installs the .deb with apt (dependencies resolved from the Ubuntu archive),
# runs rehome --version and rehome doctor --quick, verifies the files, then
# purges it and its dependencies.

set -uo pipefail
# shellcheck source=tests/packaging/lib.sh
. /src/tests/packaging/lib.sh
DEB="$1"
export DEBIAN_FRONTEND=noninteractive
mkdir -p /work

section "setup"
rm -f /etc/apt/apt.conf.d/docker-clean
apt-get update -qq >/dev/null
useradd -m -s /bin/bash tester
apt_installed >/work/pkgs-before
apt_manual >/work/manual-before
snapshot /work/s0

section "apt-get install ./$(basename "$DEB")"
dpkg-deb -I "$DEB"
cp "$DEB" /work/
apt-get install -y "/work/$(basename "$DEB")" >/work/apt.log 2>&1
rc=$?
tail -n 5 /work/apt.log
check "apt-get install exit 0 (dependencies resolved)" test "$rc" -eq 0
check "rehome is installed" test "$(dpkg-query -W -f='${db:Status-Status}' rehome)" = installed
ver="$(su - tester -c 'rehome --version' 2>&1)"
check "rehome --version: $ver" test "$ver" = "rehome $(dpkg-query -W -f='${Version}' rehome)"
out="$(su - tester -c 'NO_COLOR=1 rehome doctor --quick' 2>&1)"
printf '%s\n' "$out"
check "rehome doctor --quick ran" contains "$out" "Rehome host check"
check "doctor finds the permission helper and policy" contains "$out" "/usr/libexec/rehome/rehome-helper with its polkit policy is installed"
check "doctor finds QEMU" grep -q "QEMU .* (/usr/bin/qemu-system-x86_64)" <<<"$out"
mapfile -t files < <(dpkg -L rehome | while IFS= read -r f; do [[ -f "$f" ]] && echo "$f"; done)
check "package files = layout.py list (+ copyright)" same_files <(printf '%s\n' "${files[@]}") \
    <(python3 /src/packaging/layout.py list --prefix /usr --libexecdir /usr/libexec/rehome; echo /usr/share/doc/rehome/copyright)
check "dpkg --verify rehome" test -z "$(dpkg --verify rehome)"
check "helper shebang" test "$(head -1 /usr/libexec/rehome/rehome-helper)" = "#!/usr/bin/python3 -I"
check "no bytecode in the package directories" test -z "$(python3 -c 'import pathlib; print(*pathlib.Path("/usr/lib/rehome").rglob("__pycache__"))')"

section "apt-get purge rehome && apt-get autoremove"
apt-get purge -y rehome >/work/purge.log 2>&1
check "apt-get purge exit 0" test $? -eq 0
apt-get autoremove -y >>/work/purge.log 2>&1
check "apt-get autoremove exit 0" test $? -eq 0
tail -n 3 /work/purge.log
left=0
for f in "${files[@]}"; do [[ -e "$f" ]] && { left=$((left + 1)); echo "left: $f"; }; done
check "all ${#files[@]} files removed" test "$left" -eq 0
check "package directories removed" bash -c '! ls -d /usr/lib/rehome /usr/share/rehome /usr/libexec/rehome 2>/dev/null'
check "rehome is no longer installed" test -z "$(dpkg-query -W -f='${db:Status-Status}' rehome 2>/dev/null | grep -x installed)"
check_apt_restored /work/pkgs-before /work/manual-before
# Negative control: a package nothing pre-existing holds must be reported.
apt-get install -y -qq hello >/dev/null 2>&1
check "negative control: apt_kept.py flags an unexplained package (hello)" \
    test "$(/opt/rhtest/python3 -I /src/tests/packaging/apt_kept.py /work/pkgs-before hello)" = hello
apt-get purge -y -qq hello >/dev/null 2>&1
rm -rf /home/tester/.cache/rehome
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
printf '%s\n' "$d"
check_no_rehome_leftovers "$d"

finish
