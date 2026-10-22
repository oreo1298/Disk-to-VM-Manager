"""`rehome create` and OS listing."""

from __future__ import annotations

import os

from rehome.cli.output import bold, dim, print_json, table
from rehome.errors import RehomeError
from rehome.util.units import human, mib, parse_size


def register(sub) -> None:
    p = sub.add_parser("create", aliases=["new"], help="Create a new VM",
                       description="Create a VM, optionally from an installer ISO. The OS is "
                                   "detected from the ISO and sensible hardware is chosen.")
    p.add_argument("--name", help="VM name (default: the detected OS name)")
    p.add_argument("--iso", help="installer ISO")
    p.add_argument("--os", help="libosinfo short ID (e.g. win11, ubuntu24.04) or profile ID")
    p.add_argument("--memory", help="e.g. 4G or 4096 (MiB)")
    p.add_argument("--cpus", type=int)
    p.add_argument("--disk", help="new disk size, e.g. 64G (0 for no disk)")
    p.add_argument("--disk-bus", choices=["virtio", "sata", "ide", "scsi", "nvme"])
    p.add_argument("--use-disk", help="use an existing disk image instead of creating one")
    p.add_argument("--firmware", choices=["bios", "uefi", "uefi-secure"])
    p.add_argument("--tpm", choices=["none", "2.0", "1.2"])
    p.add_argument("--network", default="nat", help="nat, none or bridge:BRIDGE")
    p.add_argument("--nic-model")
    p.add_argument("--share", action="append", default=[], help="host folder to share (virtiofs)")
    p.add_argument("--extra-iso", action="append", default=[], help="additional CD (e.g. drivers)")
    p.add_argument("--floppy", help="floppy image (PC machine only)")
    from rehome.cli.cmd_media import add_unattended_args
    add_unattended_args(p)
    p.add_argument("--dry-run", action="store_true", help="show what would be created")
    p.add_argument("--start", action="store_true", help="start the VM after creating it")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("os-list", help="Search operating systems known to libosinfo")
    p.add_argument("query", nargs="?", default="")
    p.set_defaults(func=cmd_os_list)

    p = sub.add_parser("os-detect", help="Identify an installer ISO")
    p.add_argument("iso")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_os_detect)


def _spec(args):
    from rehome.vm.create import NewVMSpec
    network, bridge = args.network, ""
    if network.startswith("bridge"):
        network, _, bridge = network.partition(":")
    return NewVMSpec(
        name=args.name or "", iso=os.path.abspath(args.iso) if args.iso else "",
        os=args.os or "", memory_mib=mib(args.memory) if args.memory else None,
        cpus=args.cpus, disk_size=parse_size(args.disk) if args.disk is not None else None,
        disk_bus=args.disk_bus, firmware=args.firmware, tpm=args.tpm, network=network,
        bridge=bridge, nic_model=args.nic_model, shares=args.share,
        extra_isos=[os.path.abspath(i) for i in args.extra_iso],
        floppy=os.path.abspath(args.floppy) if args.floppy else "",
        existing_disk=os.path.abspath(args.use_disk) if args.use_disk else "")


def cmd_create(args) -> int:
    from rehome.vm import create as create_mod
    spec = _spec(args)
    if args.dry_run:
        p = create_mod.plan(spec)
        if args.json:
            print_json({"config": p.config.to_dict(), "disk_size": p.disk_size,
                        "notes": p.notes, "facts": p.facts})
            return 0
        _print_plan(p)
        return 0
    from rehome.cli.cmd_media import answers_from_args
    answers = answers_from_args(args)
    if answers is not None:
        from rehome.media import unattended
        unattended.validate(answers)          # before creating anything
    cfg = create_mod.create(spec)
    notes = []
    if answers is not None:
        from rehome.vm import library
        try:
            notes += unattended.prepare(cfg, answers)
        except BaseException:
            library.delete(cfg)               # don't leave a half-prepared VM behind
            raise
    if args.json and not args.start:
        print_json({"id": cfg.id, "name": cfg.name, "dir": str(cfg.dir), "notes": notes})
    elif not args.json:
        print(f"Created {bold(cfg.name)} ({cfg.short_id}) in {cfg.dir}")
        for n in notes:
            print("  " + n)
    if args.start:
        from rehome.vm import runner
        st = runner.start(cfg)
        if not args.json:
            print(f"Started (pid {st.pid}).")
        extra = []
        if answers is not None:
            # Windows DVDs wait for a key press before starting Setup.
            extra = unattended.after_start(
                cfg, status=None if args.json else (lambda s: print(dim(s + " …"))))
        notes += extra
        if args.json:
            print_json({"id": cfg.id, "name": cfg.name, "dir": str(cfg.dir), "notes": notes,
                        "pid": st.pid})
        else:
            for n in extra:
                print("  " + n)
    return 0


def _print_plan(p) -> None:
    c = p.config
    m = c.machine
    print(bold(f"{c.name}") + dim(f"  — detected: {p.os_label}, profile {c.profile}"))
    print(f"  {m.type}, {m.firmware}, TPM {m.tpm}, {m.cpus} CPU, {human(m.memory_mib * 2**20)}")
    for d in c.disks:
        print(f"  disk {d.id}: {human(p.disk_size)} on {d.bus}")
    for cd in c.cdroms:
        print(f"  CD {cd.id} [{cd.bus}]: {cd.file}")
    for n in c.nics:
        print(f"  network {n.id}: {n.model} ({n.mode})")
    print(f"  display {c.display.device}, sound {c.sound.device}, USB {c.usb.controller}")
    for n in p.notes:
        print(dim("  note: " + n))


def cmd_os_list(args) -> int:
    from rehome.media import osinfo
    if not osinfo.available():
        raise RehomeError("libosinfo is not installed.", fix="Run `rehome doctor`.")
    rows = [[e.short_id, e.name, "EOL" if e.eol else ""] for e in osinfo.search(args.query, limit=60)]
    print(table(rows, ["ID", "Name", ""]))
    return 0


def cmd_os_detect(args) -> int:
    from rehome.media import osinfo
    mi = osinfo.identify_media(os.path.abspath(args.iso))
    if args.json:
        print_json({"label": mi.label, "volume_id": mi.volume_id, "evidence": mi.evidence,
                    "os": mi.os.short_id if mi.os else None,
                    "facts": mi.os.facts() if mi.os else None})
        return 0
    how = f", recognised by {mi.evidence}" if mi.evidence else ""
    print(f"{mi.label}  (volume label {mi.volume_id!r}, os id "
          f"{mi.os.short_id if mi.os and mi.os.short_id else '-'}{how})")
    return 0
