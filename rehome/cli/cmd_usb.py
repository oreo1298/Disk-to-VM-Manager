"""`rehome usb ...`: pass host USB devices through to a running VM."""

from __future__ import annotations

from rehome.cli.output import print_json, table
from rehome.errors import NotFound


def register(sub) -> None:
    p = sub.add_parser("usb", help="Connect USB devices of this computer to a running VM")
    s = p.add_subparsers(dest="usb_cmd", metavar="ACTION", required=True)

    q = s.add_parser("list", help="List USB devices (and which are connected to the VM)")
    q.add_argument("vm", nargs="?", help="also show what is connected to this VM")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_list)

    for name, fn, text in (("attach", cmd_attach, "Connect a device to the VM (asks for permission)"),
                           ("detach", cmd_detach, "Give a device back to this computer")):
        q = s.add_parser(name, help=text)
        q.add_argument("vm")
        q.add_argument("device", help="BUS-DEVICE as shown by `rehome usb list` (e.g. 001-004) "
                                      "or VENDOR:PRODUCT (e.g. 0781:5581)")
        q.set_defaults(func=fn)


def _find_device(ref: str):
    from rehome.vm import usb
    devs = usb.list_host_devices()
    ref = ref.lower()
    hits = [d for d in devs if d.key == ref or f"{d.vendor_id}:{d.product_id}".lower() == ref
            or f"{d.bus}-{d.devnum}" == ref]
    if not hits:
        raise NotFound(f"There is no USB device \"{ref}\".", fix="See `rehome usb list`.")
    if len(hits) > 1:
        raise NotFound(f"Several USB devices match \"{ref}\".",
                       fix="Use the BUS-DEVICE form instead: "
                           + ", ".join(d.key for d in hits) + ".")
    return hits[0]


def cmd_list(args) -> int:
    from rehome.vm import library, usb
    devs = usb.list_host_devices()
    connected = usb.attached(library.find(args.vm)) if args.vm else set()
    if args.json:
        print_json([dict(d.__dict__, key=d.key, connected=d.qom_id in connected) for d in devs])
        return 0
    if not devs:
        print("No USB devices found (hubs are not listed).")
        return 0
    rows = [[d.key, f"{d.vendor_id}:{d.product_id}", d.label,
             "connected to the VM" if d.qom_id in connected else ""] for d in devs]
    print(table(rows, ["DEVICE", "ID", "NAME", ""]))
    return 0


def cmd_attach(args) -> int:
    from rehome.vm import library, runner, usb
    cfg = library.find(args.vm)
    if not runner.is_running(cfg):
        print(f"{cfg.name} is not running.")
        return 1
    dev = _find_device(args.device)
    usb.attach(cfg, dev)
    print(f"Connected {dev.label} to {cfg.name}. It is not available on this computer until you "
          f"detach it (rehome usb detach \"{cfg.name}\" {dev.key}) or the VM stops.")
    return 0


def cmd_detach(args) -> int:
    from rehome.vm import library, runner, usb
    cfg = library.find(args.vm)
    if not runner.is_running(cfg):
        print(f"{cfg.name} is not running.")
        return 1
    dev = _find_device(args.device)
    if dev.qom_id not in usb.attached(cfg):
        print(f"{dev.label} is not connected to {cfg.name}.")
        return 1
    usb.detach(cfg, dev)
    print(f"Gave {dev.label} back to this computer.")
    return 0
