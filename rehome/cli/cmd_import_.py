"""`rehome import` — turn a disk or image into a VM."""

from __future__ import annotations

import sys

from rehome.cli.output import bold, c, dim, print_json
from rehome.errors import RehomeError


def register(sub) -> None:
    p = sub.add_parser("import", help="Turn an installed system (disk or image) into a VM",
                       description="Identify the system on the source, show what was found, then "
                                   "create a VM. The source is never written to.")
    p.add_argument("sources", nargs="+", metavar="SOURCE",
                   help="/dev/sdX or image files (raw, E01, VHD/VHDX, VMDK, VDI, qcow2); several "
                        "sources for installs that span disks")
    p.add_argument("--name")
    p.add_argument("--mode", choices=["linked", "copy"], default="linked",
                   help="linked: fast, the source must stay attached (default). copy: standalone "
                        "image, the source can be removed afterwards")
    p.add_argument("--verify", action="store_true",
                   help="SHA-256 the source before and after (slow; logged in logs/import.log)")
    p.add_argument("--key", action="append", default=[],
                   help="BitLocker recovery key or LUKS passphrase (repeatable)")
    p.add_argument("--os-index", type=int, default=0, help="which detected system to boot (multi-boot)")
    p.add_argument("--discard-hibernation", action="store_true",
                   help="if Windows is hibernated, discard that state in the VM (not on the source)")
    p.add_argument("--keep-hibernation", action="store_true")
    p.add_argument("--no-patch", action="store_true", help="do not change anything inside the system")
    p.add_argument("--allow-failing-disk", action="store_true",
                   help="import even though the disk reports failing health (not recommended)")
    p.add_argument("--no-smart", action="store_true")
    # Overrides
    p.add_argument("--profile", help="force an OS profile (see data/profiles)")
    p.add_argument("--family", choices=["windows", "win9x", "dos", "linux", "bsd", "other"])
    p.add_argument("--version", dest="os_version")
    p.add_argument("--firmware", choices=["bios", "uefi", "uefi-secure"])
    p.add_argument("--disk-bus", choices=["virtio", "sata", "ide", "scsi", "nvme"])
    p.add_argument("--machine", choices=["q35", "pc"])
    p.add_argument("--memory")
    p.add_argument("--cpus", type=int)
    p.add_argument("--accel", choices=["auto", "kvm", "tcg"])
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--start", action="store_true", help="start the VM afterwards")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)

    p = sub.add_parser("verify-source", help="Re-hash an import's source and compare with the stored hash")
    p.add_argument("vm")
    p.set_defaults(func=run_verify)


def _overrides(args) -> dict:
    from rehome.util.units import mib
    ov = {}
    for k in ("profile", "family", "firmware", "disk_bus", "machine", "cpus", "accel"):
        v = getattr(args, k)
        if v:
            ov[k] = v
    if args.os_version:
        ov["version"] = args.os_version
    if args.memory:
        ov["memory_mib"] = mib(args.memory)
    return ov


def _ask(prompt: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    ans = input(prompt + (" [Y/n] " if default else " [y/N] ")).strip().lower()
    return default if not ans else ans in ("y", "yes")


def run(args) -> int:
    from rehome import importer
    from rehome.cli.cmd_inspect import print_analysis, progress_printer
    prog = progress_printer()
    a = importer.analyze(args.sources, keys=args.key, smart_check=not args.no_smart,
                         allow_failing_disk=args.allow_failing_disk, progress=prog)
    ov = _overrides(args)
    a.os_index = args.os_index
    importer.refresh(a, ov)
    if sys.stderr.isatty():
        sys.stderr.write("\r\x1b[K")
    if not args.json:
        print_analysis(a)
        print()
    opts = importer.ImportOptions(name=args.name or "", mode=args.mode, verify_hash=args.verify,
                                  keys=args.key, os_index=args.os_index, overrides=ov,
                                  patch=not args.no_patch, allow_failing_disk=args.allow_failing_disk)
    o = a.os
    if o and o.hibernated and not args.no_patch:
        if args.discard_hibernation:
            opts.discard_hibernation = True
        elif args.keep_hibernation:
            opts.discard_hibernation = False
        elif sys.stdin.isatty() and not args.yes:
            print(c("Windows is hibernated (or shut down with Fast Startup).", "33"))
            print("Discarding the hibernation state in the VM loses the unsaved session but lets it "
                  "boot normally. The source disk itself is not changed.")
            opts.discard_hibernation = _ask("Discard the hibernation state in the VM?", True)
        else:
            raise RehomeError("Windows is hibernated.",
                              fix="Pass --discard-hibernation (the unsaved session is lost in the VM; "
                                  "the source is unchanged) or --keep-hibernation.")
    if a.blockers and not (ov.get("family") or ov.get("profile")) and \
            not (args.allow_failing_disk and all(c == "failing-disk" for c in a.blocker_codes)):
        return 1
    if not args.yes and sys.stdin.isatty():
        mode = "linked (the source must stay connected)" if args.mode == "linked" else \
            "copy (a standalone image; needs disk space)"
        if not _ask(f"Create the VM now? Mode: {mode}.", True):
            print("Nothing was created.")
            return 1
    from rehome.cli.cmd_vm import _progress_printer
    bar = _progress_printer("Importing")

    def on_change(p):
        bar(p.fraction)
    prog.on_change = on_change
    cfg = importer.run(a, opts, progress=prog)
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    if args.json:
        print_json({"id": cfg.id, "name": cfg.name, "dir": str(cfg.dir)})
    else:
        print(bold(f"Created {cfg.name}") + dim(f" ({cfg.short_id}) in {cfg.dir}"))
        summary = cfg.dir / "logs" / "patch-summary.txt"
        if summary.exists():
            print(bold("Fixes applied:"))
            for line in summary.read_text().splitlines():
                print("  " + line)
        for n in (cfg.import_info.notes if cfg.import_info else []):
            print(c("  ⚠ " + n, "33"))
        print(dim("Undo all fixes with: rehome patch rollback " + cfg.short_id))
    if args.start:
        from rehome.vm import runner
        st = runner.start(cfg)
        print(f"Started (pid {st.pid}). Open the console with: rehome console {cfg.short_id}")
    return 0


def run_verify(args) -> int:
    from rehome import importer
    from rehome.vm import library
    cfg = library.find(args.vm)
    for r in importer.verify(cfg):
        print(f"{r['disk']}: {r['source']}\n  before {r['before']}\n  after  {r['after']}  "
              + c("unchanged", "32"))
    return 0
