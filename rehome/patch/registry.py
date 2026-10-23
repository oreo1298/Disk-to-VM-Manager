"""Registry of patches that profiles reference by ID."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Callable

DISK, CONFIG, EXTERNAL = "disk", "config", "external"


@dataclass
class PatchResult:
    id: str
    title: str
    status: str = "applied"        # applied / not-needed / skipped / failed / info
    message: str = ""
    changes: list[str] = field(default_factory=list)
    details: str = ""              # technical details of a failure (logged)


@dataclass
class PatchSpec:
    id: str
    title: str
    kind: str                      # disk (inside the appliance) / config / external
    fn: Callable
    families: tuple[str, ...] = ()


_PATCHES: dict[str, PatchSpec] = {}
_LOADED = False
MODULES = ("rehome.patch.common", "rehome.patch.windows", "rehome.patch.linux",
           "rehome.patch.legacy", "rehome.patch.v2v")


def patch(id: str, title: str, kind: str = DISK, families: tuple[str, ...] = ()):
    def deco(fn):
        _PATCHES[id] = PatchSpec(id=id, title=title, kind=kind, fn=fn, families=families)
        return fn
    return deco


def _load() -> None:
    global _LOADED
    if not _LOADED:
        for m in MODULES:
            importlib.import_module(m)
        _LOADED = True


def get(pid: str) -> PatchSpec:
    _load()
    return _PATCHES[pid]


def all_ids() -> set[str]:
    _load()
    return set(_PATCHES)
