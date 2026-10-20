"""`rehome checkpoint ...` (snapshots)."""

from __future__ import annotations

from rehome.cli.output import print_json


def register(sub) -> None:
    p = sub.add_parser("checkpoint", aliases=["snapshot", "snap"], help="Manage checkpoints")
    s = p.add_subparsers(dest="snap_cmd", metavar="ACTION", required=True)

    q = s.add_parser("list", help="Show the checkpoint tree")
    q.add_argument("vm")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_list)

    q = s.add_parser("create", help="Take a checkpoint (includes memory if the VM runs)")
    q.add_argument("vm")
    q.add_argument("name", nargs="?", default="")
    q.add_argument("-d", "--description", default="")
    q.set_defaults(func=cmd_create)

    q = s.add_parser("revert", aliases=["apply"], help="Revert the VM to a checkpoint")
    q.add_argument("vm")
    q.add_argument("checkpoint", help="name or ID")
    q.add_argument("--start", action="store_true",
                   help="for a checkpoint with memory: start the VM from it")
    q.set_defaults(func=cmd_revert)

    q = s.add_parser("delete", help="Delete a checkpoint (its changes are merged, not lost)")
    q.add_argument("vm")
    q.add_argument("checkpoint")
    q.add_argument("--subtree", action="store_true", help="also delete its children")
    q.set_defaults(func=cmd_delete)


def _vm(ref):
    from rehome.vm import library
    return library.find(ref)


def cmd_list(args) -> int:
    from rehome.vm import snapshots
    cfg = _vm(args.vm)
    if args.json:
        print_json({"current": cfg.current_snapshot,
                    "checkpoints": [s.__dict__ for s in cfg.snapshots]})
        return 0
    if not cfg.snapshots:
        print("No checkpoints.")
        return 0
    for line in snapshots.render_tree(cfg):
        print(line)
    return 0


def cmd_create(args) -> int:
    from rehome.vm import snapshots
    cfg = _vm(args.vm)
    snap = snapshots.create(cfg, args.name, args.description)
    print(f"Created checkpoint \"{snap.name}\" ({snap.id})"
          + (" including memory." if snap.has_state else "."))
    return 0


def cmd_revert(args) -> int:
    from rehome.vm import snapshots
    cfg = _vm(args.vm)
    if args.start:
        snapshots.start_from(cfg, args.checkpoint)
        print("Started from the checkpoint.")
    else:
        snap = snapshots.revert(cfg, args.checkpoint)
        print(f"Reverted to \"{snap.name}\".")
    return 0


def cmd_delete(args) -> int:
    from rehome.vm import snapshots
    cfg = _vm(args.vm)
    gone = snapshots.delete(cfg, args.checkpoint, recursive=args.subtree)
    print("Deleted " + ", ".join(f"\"{s.name}\"" for s in gone) + ".")
    return 0
