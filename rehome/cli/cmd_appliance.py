"""`rehome appliance ...`: build a bootable appliance from a VM (docs/APPLIANCE.md).

``build`` and ``base`` run on your computer. The other actions run inside an
appliance (started by its systemd units) and refuse to run anywhere else.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from rehome.errors import RehomeError

INSIDE = ("boot", "prepare", "kiosk", "session", "watch-boot", "after-kiosk", "firstboot",
          "console", "hardware")


def register(sub) -> None:
    p = sub.add_parser("appliance", help="Build a bootable drive image that runs a VM on its own")
    s = p.add_subparsers(dest="appliance_cmd", metavar="ACTION", required=True)

    q = s.add_parser("build", help="Build an appliance image from a VM")
    q.add_argument("vm")
    q.add_argument("output", help="image file to write (e.g. windows-xp.img)")
    q.add_argument("--no-media", action="store_true",
                   help="leave out the VM's install media (ISO and floppy images)")
    q.add_argument("--maintenance-password", action="store_true",
                   help="ask for a password the maintenance console will require")
    q.add_argument("--maintenance-password-file", metavar="FILE",
                   help="read that password from the first line of FILE")
    q.add_argument("--network", choices=("nat", "none"), default="nat",
                   help="the VM's network in the appliance (default: nat)")
    q.add_argument("--no-usb-auto", action="store_true",
                   help="do not give plugged-in USB devices to the VM automatically")
    q.add_argument("--cpu-model", default="",
                   help="a fixed processor model for the VM (e.g. Westmere), so it sees the same "
                        "processor on every computer; default: keep the VM's")
    q.add_argument("--timezone", default="", metavar="ZONE",
                   help="the appliance's time zone, e.g. Europe/Berlin (default: this "
                        "computer's); Windows VMs show local time from it")
    q.add_argument("--kernel-args", default="",
                   help="extra Linux kernel arguments for every boot entry")
    q.add_argument("--format", choices=("raw", "qcow2"), default="raw",
                   help="raw writes directly to a drive (default); qcow2 is for testing in a VM")
    q.add_argument("--refresh", action="store_true",
                   help="rebuild the cached base system with the latest Ubuntu updates")
    q.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    q.set_defaults(func=cmd_build)

    q = s.add_parser("base", help="Prepare (or refresh) the cached base system")
    q.add_argument("--refresh", action="store_true", help="rebuild it with the latest updates")
    q.set_defaults(func=cmd_base)

    # Used by the appliance's own services; not listed in --help.
    for name in INSIDE:
        q = s.add_parser(name)
        q.set_defaults(func=cmd_inside, inside=name)
    s._choices_actions = [a for a in s._choices_actions if a.dest not in INSIDE]


def _progress():
    from rehome.cli.cmd_inspect import progress_printer
    p = progress_printer()
    inner = p.on_change

    def on_change(pr):
        inner(pr)
        if sys.stderr.isatty():
            sys.stderr.write(f"  {pr.stage_fraction * 100:3.0f}%")
            sys.stderr.flush()
    p.on_change = on_change
    return p


def _password(args) -> str:
    if args.maintenance_password_file:
        pw = Path(args.maintenance_password_file).read_text().splitlines()[:1]
        return pw[0] if pw else ""
    if args.maintenance_password:
        a = getpass.getpass("Maintenance console password: ")
        b = getpass.getpass("Again: ")
        if a != b:
            raise RehomeError("The passwords do not match.")
        if not a:
            raise RehomeError("The password is empty.")
        return a
    return ""


def cmd_build(args) -> int:
    from rehome.appliance import base, build
    from rehome.util.units import human
    from rehome.vm import library
    cfg = library.find(args.vm)
    opts = build.Options(output=Path(args.output), include_media=not args.no_media,
                         maintenance_password=_password(args), network=args.network,
                         usb_auto=not args.no_usb_auto, kernel_args=args.kernel_args,
                         refresh_base=args.refresh, fmt=args.format, cpu_model=args.cpu_model,
                         timezone=args.timezone)
    pl = build.plan(cfg, opts)
    print(f"Appliance for \"{cfg.name}\" → {opts.output}")
    for d_id, src, est in pl.disks:
        print(f"  disk {d_id}: {src} (about {human(est)})")
    for d_id, f in pl.media:
        print(f"  media {d_id}: {f.name} ({human(f.stat().st_size)})")
    for w in pl.warnings:
        print(f"  note: {w}")
    cached = base.cached()
    if cached is None or base.is_stale(cached) or args.refresh:
        print("  The base system (Ubuntu with QEMU) is downloaded and prepared first: about "
              "1.2 GB, once.")
    print(f"  Image size: about {human(pl.total_estimate)}")
    if not args.yes and sys.stdin.isatty() and \
            input("Build it? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1
    out = build.build(cfg, opts, progress=_progress())
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    print(f"Built {out} ({human(out.stat().st_size)}).")
    if opts.fmt == "raw":
        print("Write it to a drive with GNOME Disks (\"Restore Disk Image\"), balenaEtcher or "
              "`dd`, then boot the target computer from it. Turn Secure Boot off there.")
    return 0


def cmd_base(args) -> int:
    from rehome.appliance import base
    b = base.ensure(_progress(), refresh=args.refresh)
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    print(f"Base system: {b.path} ({base.describe(b)})")
    return 0


def cmd_inside(args) -> int:
    from rehome.appliance import runtime
    if os.environ.get("REHOME_APPLIANCE") != "1" or not runtime.CONFIG.exists():
        raise RehomeError(f"`rehome appliance {args.inside}` only runs inside a Rehome appliance.")
    if args.inside == "console":
        from rehome.appliance import console
        return console.main()
    if args.inside == "hardware":
        from rehome.appliance import hardware
        print(hardware.gather().to_text())
        return 0
    fn = {"boot": runtime.boot, "prepare": runtime.prepare, "kiosk": runtime.kiosk,
          "session": runtime.session, "watch-boot": runtime.watch_boot,
          "after-kiosk": runtime.after_kiosk, "firstboot": runtime.firstboot}[args.inside]
    return fn()
