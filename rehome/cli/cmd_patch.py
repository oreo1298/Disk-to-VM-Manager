"""`rehome patch` — show, re-run or undo the automatic fixes of an imported VM."""

from __future__ import annotations

import json

from rehome.cli.output import bold, c, dim
from rehome.errors import RehomeError


def register(sub) -> None:
    p = sub.add_parser("patch", help="Show, re-run or undo the automatic fixes of an imported VM")
    s = p.add_subparsers(dest="patch_cmd", metavar="ACTION", required=True)
    q = s.add_parser("log", help="Show what was changed")
    q.add_argument("vm")
    q.set_defaults(func=cmd_log)
    q = s.add_parser("rollback", help="Undo every automatic fix (reverts to the pre-patch checkpoint)")
    q.add_argument("vm")
    q.set_defaults(func=cmd_rollback)
    q = s.add_parser("run", help="Run fixes again (all of the profile's, or --only ID)")
    q.add_argument("vm")
    q.add_argument("--only", action="append", default=[])
    q.add_argument("--discard-hibernation", action="store_true")
    q.add_argument("--key", action="append", default=[])
    q.set_defaults(func=cmd_run)


def cmd_log(args) -> int:
    from rehome.vm import library
    cfg = library.find(args.vm)
    f = cfg.dir / "logs" / "patch.log"
    if not f.exists():
        print("No fixes were applied to this VM.")
        return 0
    for line in f.read_text().splitlines():
        e = json.loads(line)
        if e.get("event") == "rollback":
            print(c(f"{e['time']}  rolled back to the pre-patch checkpoint", "33"))
            continue
        col = {"applied": "32", "failed": "31", "skipped": "33"}.get(e.get("status"), "2")
        print(f"{dim(e['time'])}  {c(e.get('status', ''), col):<9} {bold(e.get('title', ''))}: "
              f"{e.get('message', '')}")
        for ch in e.get("changes", []):
            print(dim("      - " + ch))
    return 0


def cmd_rollback(args) -> int:
    from rehome.patch import engine
    from rehome.vm import library
    cfg = library.find(args.vm)
    engine.rollback(cfg)
    print(f"Undid all automatic fixes of {cfg.name}. Re-apply them with `rehome patch run {cfg.short_id}`.")
    return 0


def cmd_run(args) -> int:
    from rehome.importer import ImportOptions
    from rehome.inspect.model import InspectionReport
    from rehome.patch import engine
    from rehome.vm import library
    cfg = library.find(args.vm)
    insp = cfg.dir / "inspection.json"
    if not insp.exists():
        raise RehomeError("This VM was not imported, so there is nothing to patch.")
    report = InspectionReport.from_dict(json.loads(insp.read_text()))
    if not report.oses:
        raise RehomeError("No operating system was identified when this VM was imported.")
    o = report.oses[0]
    opts = ImportOptions(keys=args.key, discard_hibernation=args.discard_hibernation or None)
    results = engine.run(cfg, o, opts, only=args.only or None)
    for r in results:
        print(f"[{r.status}] {r.title}: {r.message}")
    return 0
