"""Explain packages apt keeps after removing what an install added.

    apt_kept.py BEFORE_LIST PACKAGE...

apt's autoremove keeps an automatically installed package when an installed
package Recommends or Suggests it, names it as one alternative of an
"a | b" dependency, or depends on a virtual package it provides. After an
install is undone, a few of the dependencies it pulled in can therefore stay
because packages that were *already installed* (BEFORE_LIST, one name per line)
hold them, for example apt Recommends ca-certificates and base-files
Pre-Depends on the virtual "awk" that gawk provides.

Prints every PACKAGE that is NOT reachable that way from the packages in
BEFORE_LIST (those would be real leftovers) and exits 1 if there are any.
"""

import re
import subprocess
import sys

FIELDS = ("Depends", "Pre-Depends", "Recommends", "Suggests")


def installed() -> dict:
    fmt = "${db:Status-Status}\t${Package}\t${Provides}\t" + "\t".join("${%s}" % f for f in FIELDS) + "\n"
    out = subprocess.run(["dpkg-query", "-W", "-f", fmt], capture_output=True, text=True, check=True).stdout
    pkgs = {}
    for line in out.splitlines():
        status, name, provides, *rels = line.split("\t")
        if status == "installed":
            pkgs[name] = {"provides": _names(provides), "rels": _names(",".join(rels))}
    return pkgs


def _names(field: str) -> set:
    """Package names in a relationship field, all alternatives, no versions/arch."""
    names = set()
    for part in re.split(r"[,|]", field):
        part = part.strip()
        if part:
            names.add(re.split(r"[\s(:\[]", part, maxsplit=1)[0])
    return names


def main() -> int:
    before = {ln.strip() for ln in open(sys.argv[1], encoding="utf-8") if ln.strip()}
    pkgs = installed()
    providers = {}
    for name, info in pkgs.items():
        for v in info["provides"] | {name}:
            providers.setdefault(v, set()).add(name)
    held, todo = set(), [p for p in before if p in pkgs]
    while todo:
        p = todo.pop()
        if p in held:
            continue
        held.add(p)
        for dep in pkgs[p]["rels"]:
            todo.extend(providers.get(dep, ()))
    unexplained = [p for p in sys.argv[2:] if p not in held]
    for p in unexplained:
        print(p)
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
