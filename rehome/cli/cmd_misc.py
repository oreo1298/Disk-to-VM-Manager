"""`rehome tools ...`: after the first boot of an imported (or new) VM."""

from __future__ import annotations

from rehome.cli.output import bold, dim


def register(sub) -> None:
    p = sub.add_parser("tools", aliases=["guest-tools"],
                       help="Guest tools and faster virtual devices after the first boot")
    s = p.add_subparsers(dest="tools_cmd", metavar="ACTION", required=True)

    q = s.add_parser("status", help="What to install in the VM and what can be switched")
    q.add_argument("vm")
    q.set_defaults(func=cmd_status)

    q = s.add_parser("boot-ok", help="Confirm that the imported system starts correctly")
    q.add_argument("vm")
    q.set_defaults(func=cmd_boot_ok)

    q = s.add_parser("insert", help="Insert the guest tools disc (Windows; downloads virtio-win "
                                    "from the Fedora project if needed)")
    q.add_argument("vm")
    q.add_argument("--iso", help="use this virtio-win ISO instead of looking for or downloading one")
    q.set_defaults(func=cmd_insert)

    q = s.add_parser("paravirt", help="Switch disks, network and display to virtio "
                                      "(after the drivers are installed; takes a checkpoint first)")
    q.add_argument("vm")
    q.set_defaults(func=cmd_paravirt)


def _vm(ref):
    from rehome.vm import library
    return library.find(ref)


def cmd_status(args) -> int:
    from rehome.vm import guest_tools
    cfg = _vm(args.vm)
    ii = cfg.import_info
    if ii and not ii.first_boot_ok:
        print(bold("First boot: ") + "not confirmed yet. Start the VM, check that it works, then run "
              f"`rehome tools boot-ok \"{cfg.name}\"`.")
    plan = guest_tools.plan(cfg)
    print(bold("Guest tools: ") + plan.message)
    if plan.command:
        print("  " + plan.command)
    if plan.iso:
        print(dim(f"  Disc: {plan.iso}"))
    changes = guest_tools.paravirt_changes(cfg)
    if cfg.guest.paravirt:
        print(bold("Virtual devices: ") + "already switched to paravirtual (virtio) devices.")
    elif changes:
        print(bold("Virtual devices: ") + "once the drivers are installed, run "
              f"`rehome tools paravirt \"{cfg.name}\"` to switch to: "
              + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in changes.items()) + ".")
    return 0


def cmd_boot_ok(args) -> int:
    from rehome.vm import guest_tools
    cfg = _vm(args.vm)
    guest_tools.mark_first_boot_ok(cfg)
    print("Noted. " + guest_tools.plan(cfg).message)
    return 0


def cmd_insert(args) -> int:
    from rehome.cli.cmd_media import ProgressPrinter
    from rehome.vm import guest_tools
    cfg = _vm(args.vm)
    plan = guest_tools.plan(cfg)
    if plan.kind != "windows-iso":
        print(plan.message + (f"\n  {plan.command}" if plan.command else ""))
        return 0
    iso = args.iso or plan.iso
    if not iso:
        printer = ProgressPrinter()
        printer.status("Downloading virtio-win.iso from fedorapeople.org")
        iso = guest_tools.download_virtio_win(progress=printer)
        printer.finish()
    guest_tools.attach_tools(cfg, iso)
    print(f"Inserted {iso}. In the VM, open the CD drive and run virtio-win-guest-tools.exe.")
    return 0


def cmd_paravirt(args) -> int:
    from rehome.vm import guest_tools
    cfg = _vm(args.vm)
    log = guest_tools.switch_to_paravirt(cfg)
    if log:
        print("Switched (a checkpoint \"Before switching to virtio\" was taken first):")
        for line in log:
            print("  " + line)
    else:
        print("Nothing to change.")
    return 0
