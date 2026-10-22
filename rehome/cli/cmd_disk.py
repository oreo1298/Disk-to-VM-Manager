"""`rehome disk ...`: a VM's disks, and where new disk images are stored."""

from __future__ import annotations

from rehome.cli.output import dim, print_json, table
from rehome.util.units import human, parse_size

BUSES = ("virtio", "scsi", "sata", "ide", "nvme", "usb")
FOLDER_HELP = "folder for the new disk image (default: the folder set with `rehome disk folder`, " \
              "else the VM's folder; '' = the VM's folder)"


def register(sub) -> None:
    p = sub.add_parser("disk", help="Add, replace or remove a VM's disks; choose where disks are stored")
    s = p.add_subparsers(dest="disk_cmd", metavar="ACTION", required=True)

    q = s.add_parser("list", aliases=["ls"], help="Show a VM's disks and where their files are")
    q.add_argument("vm")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_list)

    q = s.add_parser("add", help="Add a new empty disk or an existing image file (VM off)")
    q.add_argument("vm")
    what = q.add_mutually_exclusive_group(required=True)
    what.add_argument("--size", help="new empty disk of this size, e.g. 64G")
    what.add_argument("--image", help="existing disk image file, used where it is")
    q.add_argument("--bus", choices=BUSES)
    q.add_argument("--folder", help=FOLDER_HELP)
    q.set_defaults(func=cmd_add)

    q = s.add_parser("replace", aliases=["swap"],
                     help="Swap a disk for another image or a new empty disk (VM off, no checkpoints)")
    q.add_argument("vm")
    q.add_argument("disk", help="disk ID, e.g. disk0")
    what = q.add_mutually_exclusive_group(required=True)
    what.add_argument("--size", help="new empty disk of this size, e.g. 64G")
    what.add_argument("--image", help="existing disk image file, used where it is")
    q.add_argument("--folder", help=FOLDER_HELP)
    q.add_argument("--delete-old", action="store_true",
                   help="delete the old disk file if Rehome created it (attached images are never deleted)")
    q.set_defaults(func=cmd_replace)

    q = s.add_parser("remove", aliases=["rm"], help="Remove a disk from a VM (VM off)")
    q.add_argument("vm")
    q.add_argument("disk")
    q.add_argument("--delete-file", action="store_true",
                   help="also delete the disk file if Rehome created it")
    q.set_defaults(func=cmd_remove)

    q = s.add_parser("standalone", help="Copy a linked import's disks into standalone images "
                                        "(the original disk is then no longer needed)")
    q.add_argument("vm")
    q.add_argument("--folder", help=FOLDER_HELP)
    q.set_defaults(func=cmd_standalone)

    q = s.add_parser("folder", help="Show or set the default folder for new disk images")
    q.add_argument("path", nargs="?", help="an existing folder, e.g. on a second drive")
    q.add_argument("--reset", action="store_true", help="store new disks in each VM's folder again")
    q.set_defaults(func=cmd_folder)


def _vm(ref):
    from rehome.vm import library
    return library.find(ref)


def _rows(cfg) -> list[dict]:
    from rehome.vm import disks, storage
    out = []
    for d in cfg.disks:
        path = cfg.resolve(d.file)
        try:
            size = d.source.size if d.source and d.source.mode == "linked" else \
                int(disks.info(path, d.format).get("virtual-size", 0))
        except Exception:
            size = 0
        if d.source and d.source.mode == "linked":
            kind = "linked to " + (d.source.label or d.source.path)
        elif storage.created_by_rehome(cfg, d):
            kind = "created by Rehome"
        else:
            kind = "attached image"
        out.append({"id": d.id, "file": str(path), "format": d.format, "bus": d.bus,
                    "size": size, "boot": d.boot_index, "kind": kind,
                    "exists": path.exists()})
    return out


def cmd_list(args) -> int:
    cfg = _vm(args.vm)
    rows = _rows(cfg)
    if args.json:
        print_json(rows)
        return 0
    if not rows:
        print("This VM has no disks.")
        return 0
    print(table([[r["id"], r["bus"], human(r["size"]) if r["size"] else "?", r["kind"],
                  r["file"] + ("" if r["exists"] else "  (missing)")] for r in rows],
                ["DISK", "BUS", "SIZE", "TYPE", "FILE"]))
    return 0


def cmd_add(args) -> int:
    from rehome.vm import storage
    cfg = _vm(args.vm)
    d = storage.add_disk(cfg, size=parse_size(args.size) if args.size else None, image=args.image,
                         folder=args.folder, bus=args.bus)
    print(f"Added {d.id} ({d.bus}): {cfg.resolve(d.file)}")
    return 0


def cmd_replace(args) -> int:
    from rehome.vm import storage
    cfg = _vm(args.vm)
    d, note = storage.replace_disk(cfg, args.disk, size=parse_size(args.size) if args.size else None,
                                   image=args.image, folder=args.folder, delete_old=args.delete_old)
    print(f"{d.id} is now {cfg.resolve(d.file)}")
    if note:
        print(dim(note))
    return 0


def cmd_remove(args) -> int:
    from rehome.vm import storage
    cfg = _vm(args.vm)
    note = storage.remove_disk(cfg, args.disk, delete_file=args.delete_file)
    print(f"Removed {args.disk} from {cfg.name}.")
    if note:
        print(dim(note))
    return 0


def cmd_standalone(args) -> int:
    from rehome.cli.cmd_vm import _progress_printer
    from rehome.vm import lifecycle
    cfg = _vm(args.vm)
    if not any(d.source and d.source.mode == "linked" for d in cfg.disks):
        print(f"{cfg.name} has no linked disks.")
        return 0
    lifecycle.make_standalone(cfg, progress=_progress_printer("Copying"), folder=args.folder)
    print(f"{cfg.name} no longer needs its original disk.")
    for r in _rows(cfg):
        print(f"  {r['id']}: {r['file']}")
    return 0


def cmd_folder(args) -> int:
    from rehome.vm import storage
    if args.reset:
        storage.set_default_disk_dir(None)
    elif args.path:
        storage.set_default_disk_dir(args.path)
    folder = storage.default_disk_dir()
    if folder:
        print(f"New disk images are stored in {folder} ({human(storage.free_space(folder))} free).")
    else:
        print("New disk images are stored in each VM's folder.")
    return 0
