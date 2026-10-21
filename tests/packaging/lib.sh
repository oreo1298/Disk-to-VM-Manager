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

# apt (Debian/Ubuntu): sorted names of installed packages / manually installed ones.
apt_installed() { dpkg-query -W -f='${db:Status-Status} ${Package}\n' | awk '$1 == "installed" {print $2}' | LC_ALL=C sort; }
apt_manual() { apt-mark showmanual | LC_ALL=C sort; }

# check_apt_restored PKGS_BEFORE MANUAL_BEFORE: after an install was undone,
# nothing that was installed before is gone, no manual-install mark changed,
# and any package still installed that was not before is one apt's autoremove
# keeps on purpose because a package that was installed before Recommends or
# Suggests it, lists it as an alternative ("a | b") or needs a virtual package
# it provides (e.g. apt Recommends ca-certificates; base-files needs "awk",
# which gawk provides). Those are apt policy, not something Rehome left behind.
check_apt_restored() {
    local before="$1" manual_before="$2" left
    apt_installed >/work/pkgs-after
    apt_manual >/work/manual-after
    check "no package that was installed before was removed" test -z "$(LC_ALL=C comm -23 "$before" /work/pkgs-after)"
    check "manual-install marks unchanged" same_content "$manual_before" /work/manual-after
    left="$(LC_ALL=C comm -13 "$before" /work/pkgs-after | tr '\n' ' ')"
    if [[ -z "${left// /}" ]]; then
        pass "installed package set is exactly as before"
    else
        note "apt's autoremove kept these automatically installed packages: $left"
        # shellcheck disable=SC2086  # one argument per package name
        check "each is held by a package that was installed before (Recommends/Suggests, alternative or virtual provider)" \
            /opt/rhtest/python3 -I /src/tests/packaging/apt_kept.py "$before" $left
    fi
}

# check_no_rehome_leftovers DIFF [ALLOWED_REMOVALS_REGEX]: judge a snapdiff taken
# around a full install + uninstall with real packages. Distribution packages'
# maintainer scripts legitimately leave things behind (system users' state,
# enabled units, empty directories such as /usr/local/share/fonts from
# fontconfig-config or /usr/local/share/ca-certificates from ca-certificates),
# so the check is: nothing added mentions Rehome, no regular file was added
# under /usr/local (Rehome installs files there; the packages only create
# directories), and nothing was removed except ALLOWED_REMOVALS_REGEX. A path
# that was removed and added again changed type (e.g. the locales package turns
# /etc/default/locale into a symlink to /etc/locale.conf); it is not a removal.
check_no_rehome_leftovers() {
    local d="$1" allowed="${2:-^$}" added removed
    added="$(grep '^+ ' <<<"$d")"
    removed="$(awk '$1 == "+" {a[$3] = 1} $1 == "-" {r[$3] = $0} END {for (p in r) if (!(p in a)) print r[p]}' <<<"$d" \
        | grep -Ev -- "$allowed")"
    note "$(grep -c '^+ ' <<<"$d") paths added and $(grep -c '^- ' <<<"$d") removed by the distribution packages' maintainer scripts or on purpose; added under /usr/local: $(grep ' /usr/local' <<<"$added" | awk '{print $3}' | tr '\n' ' ')"
    check "nothing added mentions Rehome" test -z "$(grep -iE 'rehome|oreo1298' <<<"$added")"
    check "no file added under /usr/local (only package-created empty directories)" test -z "$(grep -E '^\+ [^d] /usr/local' <<<"$added")"
    check "nothing removed except what was meant to be${removed:+ (unexpected: $(awk '{print $3}' <<<"$removed" | head -n 5 | tr '\n' ' '))}" test -z "$removed"
}

# Relocatable Python 3.12 from Ubuntu (tests/packaging/make-bundle.sh), for
# images whose own python3 cannot be installed because their mirrors are blocked.
use_bundle_python() {
    local dir="${1:-/usr/local/bin}"
    [[ -x /opt/rhtest/python3 ]] || { note "no Python bundle mounted"; return 1; }
    mkdir -p "$dir"
    ln -sf /opt/rhtest/python3 "$dir/python3"
}
