"""Terminal output helpers (colours only when writing to a terminal)."""

from __future__ import annotations

import json
import os
import sys

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"

STATUS_STYLE = {
    "ok": ("✔", "32"), "info": ("ℹ", "36"), "warning": ("⚠", "33"), "error": ("✖", "31"),
    "running": ("●", "32"), "paused": ("‖", "33"), "stopped": ("○", "2"),
    "saved": ("◐", "36"), "crashed": ("✖", "31"),
}


def c(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


def bold(text: str) -> str:
    return c(text, "1")


def dim(text: str) -> str:
    return c(text, "2")


def status(s: str) -> str:
    sym, code = STATUS_STYLE.get(s, ("•", "0"))
    return c(sym, code)


def print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def table(rows: list[list[str]], headers: list[str]) -> str:
    def plain(s):
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", s)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(plain(cell)))
    out = ["  ".join(bold(h.ljust(widths[i])) for i, h in enumerate(headers))]
    for r in rows:
        out.append("  ".join(cell + " " * (widths[i] - len(plain(cell))) for i, cell in enumerate(r)))
    return "\n".join(out)


def error(err) -> None:
    from rehome.errors import RehomeError
    print(c("Error: ", "31;1") + str(err), file=sys.stderr)
    if isinstance(err, RehomeError):
        if err.fix:
            print(c("How to fix: ", "33;1") + err.fix, file=sys.stderr)
        if err.details and os.environ.get("REHOME_VERBOSE"):
            print(dim("--- technical details ---"), file=sys.stderr)
            print(err.details, file=sys.stderr)
        elif err.details:
            print(dim("(run with --verbose for technical details)"), file=sys.stderr)
