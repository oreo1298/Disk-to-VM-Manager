"""`rehome sources` — list disks that can be imported."""

from __future__ import annotations

from rehome.cli.output import bold, c, dim, print_json, status, table
from rehome.util.units import human


def register(sub) -> None:
    p = sub.add_parser("sources", help="List physical disks that could be imported")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="also show loop and nbd devices")
    p.set_defaults(func=run)


def run(args) -> int:
    import os
    if args.all:
        os.environ["REHOME_SHOW_LOOP"] = "1"
    from rehome.disk import sources
    disks = sources.list_disks()
    if args.json:
        print_json([d.to_dict() for d in disks])
        return 0
    if not disks:
        print("No disks found.")
        return 0
    rows = []
    for d in disks:
        st = "ok" if d.safe else "error"
        rows.append([f"{status(st)} {'ready' if d.safe else 'refused'}", d.path, d.label or "-",
                     human(d.size), d.transport.upper() or "-", d.partition_table or "-"])
    print(table(rows, ["", "Device", "Model", "Size", "Bus", "Table"]))
    for d in disks:
        for p in d.problems:
            print(dim(f"  {d.path}: ") + (c(p, "33") if not d.safe else p))
    print()
    print("Import a disk with: " + bold("rehome import /dev/sdX") + "  (disk images work too: "
          "rehome import disk.vmdk)")
    return 0
