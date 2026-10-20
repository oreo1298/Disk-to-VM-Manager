"""Per-OS profiles loaded from ``data/profiles/*.toml``.

A profile matches detected OS facts and supplies the virtual hardware for
the first boot of an imported system (``[hardware]``), the paravirtual
hardware to switch to after guest tools are installed (``[paravirt]``),
overrides for fresh installs (``[create]``), and the ordered list of patches
(``[patches]``). Adding support for an OS means adding a data file, not
another if-chain.

Matching keys in ``[match]``:

* ``family`` / ``distro`` / ``arch`` — string or list of strings
* ``version_min`` / ``version_max`` — dotted versions, inclusive
* ``build_min`` / ``build_max`` — integer build numbers, inclusive
* ``name_regex`` — regular expression on the product name
* ``edition`` — "client" / "server" (Windows)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from rehome import paths
from rehome.util import toml


@dataclass
class Profile:
    id: str
    name: str
    priority: int
    match: dict
    hardware: dict
    paravirt: dict = field(default_factory=dict)
    create: dict = field(default_factory=dict)
    patches: list[str] = field(default_factory=list)
    patch_params: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    supported: bool = True
    path: str = ""

    def hw(self, key: str, default=None, *, fresh: bool = False, paravirt: bool = False):
        if paravirt and key in self.paravirt:
            return self.paravirt[key]
        if fresh and key in self.create:
            return self.create[key]
        return self.hardware.get(key, default)


def _version_tuple(v) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(v))
    return tuple(int(n) for n in nums) if nums else (0,)


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def matches(match: dict, facts: dict) -> int:
    """Return a specificity score (>0) when ``facts`` satisfy ``match``, else 0."""
    score = 1
    for key, want in match.items():
        if key in ("family", "distro", "arch", "edition", "boot"):
            have = str(facts.get(key, "")).lower()
            if have not in [str(w).lower() for w in _as_list(want)]:
                return 0
            score += 2
        elif key.endswith(("_min", "_max")):
            # version_min, build_max, kernel_max, ... compare the fact numerically.
            have = facts.get(key[:-4])
            if have in (None, "", 0, "0"):
                return 0
            hv, wv = _version_tuple(have), _version_tuple(want)
            if key.endswith("_min") and hv < wv:
                return 0
            if key.endswith("_max") and hv > wv:
                return 0
            score += 1
        elif key == "name_regex":
            if not re.search(want, str(facts.get("name", "")), re.I):
                return 0
            score += 2
        else:
            have = facts.get(key)
            if have is None or str(have).lower() not in [str(w).lower() for w in _as_list(want)]:
                return 0
            score += 1
    return score


@lru_cache(maxsize=1)
def load_all() -> tuple[Profile, ...]:
    out = []
    for f in sorted((paths.data_files_dir() / "profiles").glob("*.toml")):
        data = toml.load(f)
        patches = data.get("patches", {})
        out.append(Profile(
            id=data["id"], name=data.get("name", data["id"]),
            priority=int(data.get("priority", 0)), match=data.get("match", {}),
            hardware=data.get("hardware", {}), paravirt=data.get("paravirt", {}),
            create=data.get("create", {}), patches=list(patches.get("list", [])),
            patch_params={k: v for k, v in patches.items() if k != "list"},
            notes=list(data.get("notes", {}).get("list", [])),
            supported=bool(data.get("supported", True)), path=str(f)))
    return tuple(out)


def get(profile_id: str) -> Profile:
    for p in load_all():
        if p.id == profile_id:
            return p
    raise KeyError(profile_id)


def select(facts: dict) -> Profile:
    """Best matching profile: highest priority, then most specific match."""
    best = None
    best_key = None
    for p in load_all():
        score = matches(p.match, facts)
        if not score:
            continue
        key = (p.priority, score)
        if best_key is None or key > best_key:
            best, best_key = p, key
    if best is None:
        return get("generic")
    return best
