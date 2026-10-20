#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Runs as root inside a Fedora or openSUSE container:
#   ct-rpm.sh FAMILY PM /pkgs/rehome-X.noarch.rpm
# 1. rpm -i --nodeps (the dependencies cannot be downloaded here), verify the
#    files against layout.py, rpm -V, rehome --version, rpm -e: nothing left.
# 2. install.sh's package removal on rpm systems with dummy packages
#    (/pkgs/dummy): packages that other software still needs are kept, the
#    rest is removed with dnf/zypper (repositories disabled: they are
#    unreachable from this sandbox).

set -uo pipefail
# shellcheck source=tests/packaging/lib.sh
. /src/tests/packaging/lib.sh
FAMILY="$1"
PM="$2"
RPM="$3"
export NO_COLOR=1
mkdir -p /work /root/.cache /root/.config /root/.local/share /root/.local/state
use_bundle_python /usr/bin    # the package's scripts use #!/usr/bin/python3

section "rpm -i --nodeps $(basename "$RPM") ($FAMILY)"
rpm -qpi "$RPM"
# On a real system these directories belong to packages Rehome requires (polkit;
# hicolor-icon-theme via gtk4). With --nodeps they are absent, and rpm would
# leave the unowned parents it creates behind, so create them first.
mkdir -p /usr/share/polkit-1/actions /usr/share/icons/hicolor/scalable/apps /usr/share/icons/hicolor/symbolic/apps
note "pre-created /usr/share/polkit-1/actions and the hicolor icon directories (owned by polkit and hicolor-icon-theme on a real system)"
snapshot /work/s0
rpm -i --nodeps "$RPM"
check "rpm -i exit 0" test $? -eq 0
check "rpm -q rehome" rpm -q rehome
mapfile -t files < <(rpm -ql rehome | while IFS= read -r f; do [[ -f "$f" ]] && echo "$f"; done)
docs="$(rpm -ql rehome | grep -E '^/usr/share/(doc|licenses)/' | while IFS= read -r f; do [[ -f "$f" ]] && echo "$f"; done)"
libexec="$(rpm --eval '%{_libexecdir}')/rehome"
check "package files = layout.py list (libexecdir $libexec) + docs" same_files <(printf '%s\n' "${files[@]}") \
    <(python3 /src/packaging/layout.py list --prefix /usr --libexecdir "$libexec"; printf '%s\n' "$docs")
check "rpm -V --nodeps rehome (digests, modes)" test -z "$(rpm -V --nodeps rehome)"
ver="$(rehome --version 2>&1)"
check "rehome --version: $ver" test "$ver" = "rehome $(rpm -q --qf '%{VERSION}' rehome)"
check "polkit policy names the helper" grep -q "$libexec/rehome-helper" /usr/share/polkit-1/actions/io.github.oreo1298.Rehome.policy
check "no bytecode in the package directories" test -z "$(python3 -c 'import pathlib; print(*pathlib.Path("/usr/lib/rehome").rglob("__pycache__"))')"
rpm -e rehome
check "rpm -e exit 0" test $? -eq 0
left=0
for f in "${files[@]}"; do [[ -e "$f" ]] && { left=$((left + 1)); echo "left: $f"; }; done
check "all ${#files[@]} files removed" test "$left" -eq 0
rm -rf /root/.cache/rehome
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
[[ -n "$d" ]] && printf 'left behind:\n%s\n' "$d"
check "nothing left behind" test -z "$d"

section "install.sh: removing recorded packages that other software needs"
if ! ls /pkgs/dummy/*.rpm >/dev/null 2>&1; then
    note "no dummy packages built; skipping"
    finish
    exit
fi
case "$PM" in
    dnf) for f in /etc/yum.repos.d/*.repo; do sed -i 's/^enabled=1/enabled=0/' "$f"; done ;;
    zypper) zypper --non-interactive modifyrepo --all --disable >/dev/null ;;
esac
note "disabled the distribution repositories (unreachable from this sandbox)"
rpm -i --nodeps /pkgs/dummy/*.rpm
check "dummy packages installed" rpm -q dummy-base dummy-solo dummy-x dummy-y dummy-user
# shellcheck disable=SC1091
out="$(export REHOME_INSTALLER_NO_MAIN=1; . /src/install.sh; rpm_removable dummy-base dummy-solo dummy-x dummy-y)"
check "rpm_removable keeps what dummy-user needs (directly and through dummy-x): '$out'" test "$out" = dummy-solo

snapshot /work/s0
/src/install.sh --yes --skip-deps >/work/install.log 2>&1
check "file-only install" test $? -eq 0
# Pretend the installer had added these four packages.
python3 - <<'EOF'
import json
p = "/usr/local/share/rehome/install-manifest.json"
d = json.load(open(p))
d["packages"]["installed"] = ["dummy-base", "dummy-solo", "dummy-x", "dummy-y"]
json.dump(d, open(p, "w"), indent=2)
EOF
out="$(/src/install.sh --uninstall --yes 2>&1)"
rc=$?
printf '%s\n' "$out"
check "uninstall exit 0" test "$rc" -eq 0
check "explains which are kept" contains "$out" "Kept because other installed software needs them: dummy-base dummy-x dummy-y"
case "$PM" in
    dnf) want="\$ dnf remove -y dummy-solo" ;;
    zypper) want="\$ zypper --non-interactive remove --clean-deps dummy-solo" ;;
esac
check "prints the exact removal command: $want" contains "$out" "$want"
check "dummy-solo removed" bash -c '! rpm -q dummy-solo >/dev/null'
check "dummy-base, dummy-x, dummy-y kept" rpm -q dummy-base dummy-x dummy-y
rm -rf /root/.cache/rehome
rpm -e dummy-user dummy-x dummy-y dummy-base
snapshot /work/s1
d="$(snapdiff /work/s0 /work/s1)"
[[ -n "$d" ]] && printf 'left behind:\n%s\n' "$d"
check "nothing left behind" test -z "$d"

finish
