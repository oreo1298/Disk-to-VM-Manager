"""`rehome doctor` — host check."""

from __future__ import annotations

import textwrap

from rehome.cli.output import bold, c, dim, print_json, status


def register(sub) -> None:
    p = sub.add_parser("doctor", help="Check this computer and explain how to fix problems",
                       description="Check CPU virtualization, KVM, QEMU, firmware and the "
                                   "tools Rehome uses, and explain how to fix any problem.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--quick", action="store_true", help="skip slow probes (OpenGL test)")
    p.add_argument("--refresh", action="store_true", help="ignore cached QEMU capabilities")
    p.set_defaults(func=run)


def run(args) -> int:
    from rehome.host import doctor
    if args.refresh:
        from rehome import paths
        for f in paths.cache_dir().glob("qemu-caps-*.json"):
            f.unlink(missing_ok=True)
    report = doctor.run(deep=not args.quick, refresh=args.refresh)
    if args.json:
        print_json(report.to_dict())
        return 0 if report.worst != "error" else 1
    print(bold("Rehome host check"))
    section = None
    order: list[str] = []
    for chk in report.checks:
        if chk.section not in order:
            order.append(chk.section)
    for chk in sorted(report.checks, key=lambda k: order.index(k.section)):
        if chk.section != section:
            section = chk.section
            print()
            print(bold(section))
        print(f"  {status(chk.status)} {chk.title}: {chk.detail}")
    problems = report.problems
    print()
    if not problems:
        print(c("Everything Rehome needs is in place.", "32;1"))
        return 0
    print(bold(f"{len(problems)} thing(s) to look at:"))
    for i, chk in enumerate(problems, 1):
        print()
        head = f"{i}. {chk.title}"
        print(f"{status(chk.status)} {bold(head)}")
        for line in textwrap.wrap(chk.detail, 88):
            print(f"   {line}")
        if chk.fix:
            for j, line in enumerate(textwrap.wrap(chk.fix, 84)):
                print(("   Fix: " if j == 0 else "        ") + line)
        for cmd in chk.commands:
            print("   " + dim("$ ") + c(cmd, "1"))
    return 1 if report.worst == "error" else 0
