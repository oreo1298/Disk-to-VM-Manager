# shellcheck shell=bash
# Helpers for the container tests (sourced inside the containers).

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); printf 'PASS: %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$*"; }
note() { printf 'NOTE: %s\n' "$*"; }
section() { printf '\n######## %s\n' "$*"; }

# check DESCRIPTION CMD...: PASS if CMD succeeds.
check() {
    local desc="$1"
    shift
    if "$@"; then pass "$desc"; else fail "$desc"; fi
}

contains() { grep -qF -- "$2" <<<"$1"; }   # contains TEXT NEEDLE

finish() {
    printf '\nRESULT: %d passed, %d failed\n' "$PASS" "$FAIL"
    ((FAIL == 0))
}

# snapshot OUT: every file and directory on the root filesystem except volatile
# state (caches, logs, package-manager databases) and the test mounts. Uses the
# mounted Python bundle (some images have no find); falls back to find(1).
snapshot() {
    if [[ -x /opt/rhtest/python3 ]]; then
        /opt/rhtest/python3 -I /src/tests/packaging/snapshot.py "$1.unsorted" && LC_ALL=C sort "$1.unsorted" >"$1"
        rm -f "$1.unsorted"
    else
        find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /tmp \
            -o -path /var/tmp -o -path /var/cache -o -path /var/log -o -path /src -o -path /pkgs \
            -o -path /opt/rhtest -o -path /results -o -path /work -o -path /var/lib/apt/lists \
            -o -path /var/lib/dpkg -o -path /var/lib/pacman -o -path /var/lib/rpm \
            -o -path /usr/lib/sysimage -o -path /var/lib/dnf -o -path /var/lib/zypp \
            -o -path /etc/ld.so.cache \) -prune -o -printf '%y %p\n' 2>/dev/null | LC_ALL=C sort >"$1"
    fi
    # Sanity check, so a broken snapshot can never make "nothing changed" pass.
    grep -qx 'd /usr/bin' "$1" || echo "SNAPSHOT-BROKEN" >"$1"
}

# snapdiff A B: print paths added (+) or removed (-) between two snapshots.
# Uses comm (coreutils): some images have no diff. Fails loudly if it cannot run.
snapdiff() {
    [[ -s "$1" && -s "$2" ]] || { echo "SNAPSHOT-MISSING"; return 1; }
    grep -q SNAPSHOT-BROKEN "$1" "$2" && { echo "SNAPSHOT-BROKEN"; return 1; }
    command -v comm >/dev/null 2>&1 || { echo "COMM-MISSING"; return 1; }
    LC_ALL=C comm -13 "$1" "$2" | sed 's/^/+ /'
    LC_ALL=C comm -23 "$1" "$2" | sed 's/^/- /'
}

# same_content A B: identical files (some images have no cmp).
same_content() { [[ -f "$1" && -f "$2" && "$(<"$1")" == "$(<"$2")" ]]; }

# same_files A B: the two lists (one path per line) are identical as sets.
same_files() { [[ "$(LC_ALL=C sort -u "$1")" == "$(LC_ALL=C sort -u "$2")" ]]; }

# Relocatable Python 3.12 from Ubuntu (tests/packaging/make-bundle.sh), for
# images whose own python3 cannot be installed because their mirrors are blocked.
use_bundle_python() {
    local dir="${1:-/usr/local/bin}"
    [[ -x /opt/rhtest/python3 ]] || { note "no Python bundle mounted"; return 1; }
    mkdir -p "$dir"
    ln -sf /opt/rhtest/python3 "$dir/python3"
}
