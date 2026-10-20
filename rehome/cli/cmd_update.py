"""`rehome update-check` — opt-in check for a newer release."""

from __future__ import annotations

from rehome.cli.output import bold, c, dim, print_json


def register(sub) -> None:
    p = sub.add_parser(
        "update-check", help="Check for a newer Rehome release (opt-in)",
        description="Check GitHub for a newer Rehome release and explain how to update. "
                    "Running this command makes one anonymous request (no identifiers, only "
                    "a 'rehome/<version>' User-Agent). Automatic checks in the app are off "
                    "unless you turn them on with --enable.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true",
                   help="let Rehome check automatically (at most once a day)")
    g.add_argument("--disable", action="store_true", help="turn automatic checks off")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=run)


def run(args) -> int:
    from rehome import update
    if args.enable or args.disable:
        update.set_enabled(bool(args.enable))
        state = "on" if args.enable else "off"
        print(f"Automatic update checks are {bold(state)} ({update.settings_path()}).")
        if args.enable:
            print(dim("Rehome will ask GitHub for the latest release at most once a day. "
                      "No identifiers are sent."))
        return 0
    result = update.check(force=True)
    method = update.install_method()
    if args.json:
        print_json({**result.to_dict(), "automatic": update.is_enabled(),
                    "install_method": method.kind, "update_commands": list(method.update_commands)})
        return 0
    if result.error:
        print(f"Could not check for updates: {result.error}")
    elif result.available:
        print(c(f"Rehome {result.latest} is available", "32;1") + f" (you have {result.current}).")
        if result.url:
            print(f"Release notes: {result.url}")
    else:
        print(f"Rehome {result.current} is up to date (latest release: {result.latest}).")
    if result.available or result.error:
        print()
        print(bold("How to update") + dim(f" (installed via {method.kind}):"))
        for cmd in method.update_commands:
            print(f"  {cmd}")
    if not update.is_enabled():
        print()
        print(dim("Automatic checks are off. Turn them on with `rehome update-check --enable`."))
    return 0
