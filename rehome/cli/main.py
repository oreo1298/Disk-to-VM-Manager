"""Command-line entry point.

``rehome`` with no arguments starts the GUI; every GUI capability is also
available as a sub-command for scripting.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys

from rehome import __version__
from rehome.errors import RehomeError

COMMAND_MODULES = [
    "doctor", "vm", "create", "snapshot", "sources", "inspect", "import_",
    "patch", "usb", "media", "misc",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rehome",
        description="Rehome turns installed operating systems (physical disks or disk images) "
                    "into QEMU/KVM virtual machines and manages VMs.")
    p.add_argument("--version", action="version", version=f"rehome {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="show technical details")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    for name in COMMAND_MODULES:
        try:
            mod = importlib.import_module(f"rehome.cli.cmd_{name}")
        except ModuleNotFoundError as e:
            if e.name and e.name.startswith("rehome.cli.cmd_"):
                continue
            raise
        mod.register(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        os.environ["REHOME_VERBOSE"] = "1"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if not getattr(args, "func", None):
        if not argv:
            return launch_gui()
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except RehomeError as e:
        from rehome.cli.output import error
        error(e)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def launch_gui() -> int:
    if os.geteuid() == 0:
        print("Rehome's graphical interface must not run as root. Run it as your normal user; "
              "it asks for permission when it needs it.", file=sys.stderr)
        return 1
    try:
        from rehome.ui.app import run as run_gui
    except (ImportError, ValueError) as e:
        print(f"The graphical interface is unavailable: {e}\n"
              "Run `rehome doctor` to see which packages are missing, or use `rehome --help` "
              "for the command line.", file=sys.stderr)
        return 1
    return run_gui(sys.argv[:1])
