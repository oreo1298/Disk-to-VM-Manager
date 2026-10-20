"""List every file and directory on the root filesystem as "TYPE PATH" lines.

Used by the container tests to prove that uninstall leaves nothing behind
(some images have no find(1)). Volatile state and test mounts are skipped.
"""

import os
import stat
import sys

SKIP = {
    "/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/var/cache", "/var/log", "/src",
    "/pkgs", "/opt/rhtest", "/results", "/work", "/var/lib/apt/lists", "/var/lib/dpkg",
    "/var/lib/pacman", "/var/lib/rpm", "/usr/lib/sysimage", "/var/lib/dnf", "/var/lib/zypp",
    "/etc/ld.so.cache",
}


def kind(mode: int) -> str:
    for test, ch in ((stat.S_ISDIR, "d"), (stat.S_ISLNK, "l"), (stat.S_ISREG, "f"),
                     (stat.S_ISFIFO, "p"), (stat.S_ISSOCK, "s"), (stat.S_ISCHR, "c"),
                     (stat.S_ISBLK, "b")):
        if test(mode):
            return ch
    return "?"


def main(out: str) -> None:
    root_dev = os.lstat("/").st_dev
    lines = []
    stack = ["/"]
    while stack:
        d = stack.pop()
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            p = os.path.join(d, name)
            if p in SKIP:
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue
            lines.append(f"{kind(st.st_mode)} {p}")
            if stat.S_ISDIR(st.st_mode) and st.st_dev == root_dev:
                stack.append(p)
    lines.sort()
    with open(out, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
