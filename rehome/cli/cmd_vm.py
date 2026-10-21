"""VM lifecycle commands: list, info, start, shutdown, ..."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from rehome.cli.output import bold, c, dim, print_json, status, table
from rehome.errors import RehomeError
from rehome.util.units import human


def register(sub) -> None:
    p = sub.add_parser("list", aliases=["ls"], help="List VMs and their state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="Show a VM's configuration and state")
    p.add_argument("vm")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("start", help="Start a VM")
    p.add_argument("vm")
    p.add_argument("--headless", action="store_true", help="no display (QMP screenshots still work)")
    p.add_argument("--paused", action="store_true", help="start with the CPU paused")
    p.add_argument("--no-restore", action="store_true", help="ignore a saved state (it is kept)")
    p.add_argument("--accel", choices=["kvm", "tcg"], help="override the accelerator")
    p.add_argument("--allow-overcommit", action="store_true",
                   help="start even if memory would be over-allocated")
    p.add_argument("--console", action="store_true", help="open the console window after starting")
    p.set_defaults(func=cmd_start)

    for name, helptext in (("shutdown", "Ask the guest to shut down (ACPI power button)"),
                           ("stop", "Same as shutdown")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("vm")
        p.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                       help="wait for the VM to stop")
        p.add_argument("--force-after", type=float, default=0, metavar="SECONDS",
                       help="turn the VM off if it has not stopped after SECONDS")
        p.set_defaults(func=cmd_shutdown)

    for name, fn, helptext in (
            ("force-off", "force_off", "Turn the VM off immediately (like pulling the plug)"),
            ("reset", "reset", "Reset the VM (like pressing the reset button)"),
            ("pause", "pause", "Pause the VM"),
            ("resume", "resume", "Resume a paused VM")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("vm")
        p.set_defaults(func=cmd_simple, op=fn)

    p = sub.add_parser("save", help="Save the VM's memory to disk and stop it")
    p.add_argument("vm")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("discard-state", help="Delete a VM's saved state")
    p.add_argument("vm")
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("status", help="Print a VM's state")
    p.add_argument("vm")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wait", help="Wait until a VM has stopped")
    p.add_argument("vm")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("screenshot", help="Save a screenshot of the VM's display (PNG)")
    p.add_argument("vm")
    p.add_argument("file")
    p.set_defaults(func=cmd_screenshot)

    p = sub.add_parser("screen-text", help="Print the text on a text-mode screen (DOS, BIOS, consoles)")
    p.add_argument("vm")
    p.set_defaults(func=cmd_screen_text)

    p = sub.add_parser("send-keys", help="Send a key combination, e.g. ctrl-alt-delete")
    p.add_argument("vm")
    p.add_argument("keys", help="qcodes joined with '-', e.g. ctrl-alt-delete")
    p.set_defaults(func=cmd_send_keys)

    p = sub.add_parser("type", help="Type text into the VM (US keyboard layout)")
    p.add_argument("vm")
    p.add_argument("text")
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("cd", help="Insert or eject an ISO in a VM's CD drive")
    p.add_argument("vm")
    p.add_argument("iso", nargs="?", help="ISO to insert (omit to eject)")
    p.add_argument("--drive", default="cd0")
    p.set_defaults(func=cmd_cd)

    p = sub.add_parser("set", help="Change VM settings (VM must be off for hardware changes)")
    p.add_argument("vm")
    p.add_argument("--name")
    p.add_argument("--memory", help="e.g. 4G or 4096 (MiB)")
    p.add_argument("--cpus", type=int)
    p.add_argument("--cpu-model", help="host, max, qemu64, pentium3, ...")
    p.add_argument("--firmware", choices=["bios", "uefi", "uefi-secure"])
    p.add_argument("--tpm", choices=["none", "2.0", "1.2"])
    p.add_argument("--machine", choices=["q35", "pc"])
    p.add_argument("--accel", choices=["auto", "kvm", "tcg"])
    p.add_argument("--disk-bus", metavar="DISK=BUS", action="append", default=[],
                   help="e.g. disk0=virtio")
    p.add_argument("--nic-model", metavar="NIC=MODEL", action="append", default=[])
    p.add_argument("--net", metavar="NIC=MODE[:BRIDGE]", action="append", default=[],
                   help="nat, none or bridge:br0")
    p.add_argument("--forward", metavar="NIC=tcp:HOST:GUEST", action="append", default=[])
    p.add_argument("--display", help="virtio-vga, VGA, qxl-vga, cirrus-vga, ...")
    p.add_argument("--gl", choices=["on", "off"], help="3D acceleration (virgl)")
    p.add_argument("--console", choices=["dbus", "spice", "vnc"],
                   help="how the screen is shown: Rehome's console (dbus, default) or an external "
                        "SPICE/VNC viewer")
    p.add_argument("--sound", help="none, ich9-intel-hda, AC97, sb16, ...")
    p.add_argument("--usb", help="USB controller: none, qemu-xhci, piix3-usb-uhci, ...")
    p.add_argument("--pointer", choices=["usb-tablet", "virtio-tablet", "ps2", "usb-mouse"])
    p.add_argument("--rtc", choices=["utc", "localtime"])
    p.add_argument("--add-disk", metavar="SIZE[:BUS]", action="append", default=[])
    p.add_argument("--attach-disk", metavar="IMAGE[:BUS]", action="append", default=[],
                   help="attach an existing image file")
    p.add_argument("--remove-disk", metavar="DISK", action="append", default=[])
    p.add_argument("--resize-disk", metavar="DISK=SIZE", action="append", default=[])
    p.add_argument("--add-cd", metavar="ISO", action="append", default=[], help="'' for empty drive")
    p.add_argument("--remove-cd", metavar="CD", action="append", default=[])
    p.add_argument("--add-share", metavar="PATH[:TAG]", action="append", default=[])
    p.add_argument("--remove-share", metavar="TAG", action="append", default=[])
    p.add_argument("--boot", metavar="DEVICE", help="device to boot first, e.g. cd0 or disk0")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("delete", aliases=["rm"], help="Delete a VM")
    p.add_argument("vm")
    p.add_argument("--keep-disks", action="store_true", help="move the disk files aside instead")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("clone", help="Copy a VM")
    p.add_argument("vm")
    p.add_argument("name")
    p.set_defaults(func=cmd_clone)

    p = sub.add_parser("export", help="Export a VM to a portable .rehome file")
    p.add_argument("vm")
    p.add_argument("dest", help="file or directory")
    p.add_argument("--compress", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import-vm", help="Import a VM from a .rehome file")
    p.add_argument("file")
    p.add_argument("--name")
    p.set_defaults(func=cmd_import_vm)

    p = sub.add_parser("logs", help="Show a VM's logs")
    p.add_argument("vm")
    p.add_argument("--which", default="qemu",
                   help="qemu, serial, import, patch, swtpm (default qemu)")
    p.add_argument("-n", "--lines", type=int, default=60)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("diagnostics", help="Print diagnostics for bug reports (\"Copy diagnostics\")")
    p.add_argument("vm")
    p.set_defaults(func=cmd_diagnostics)

    p = sub.add_parser("console", help="Open the VM's console window")
    p.add_argument("vm")
    p.add_argument("--external", action="store_true",
                   help="open a VNC/SPICE console in remote-viewer (when QEMU has no D-Bus display)")
    p.set_defaults(func=cmd_console)


def _vm(ref):
    from rehome.vm import library
    return library.find(ref)


def _progress_printer(label: str):
    start = time.monotonic()
    tty = sys.stderr.isatty()

    def cb(frac: float):
        if not tty:
            return
        frac = max(0.0, min(1.0, frac))
        elapsed = time.monotonic() - start
        eta = (elapsed / frac - elapsed) if frac > 0.01 else 0
        from rehome.util.units import human_duration
        bar = "#" * int(frac * 30)
        sys.stderr.write(f"\r{label} [{bar:<30}] {frac * 100:5.1f}%  ETA {human_duration(eta):>10}")
        sys.stderr.flush()
        if frac >= 1.0:
            sys.stderr.write("\n")
    return cb


def cmd_list(args) -> int:
    from rehome.vm import library, runner
    vms = library.list_vms()
    rows, out = [], []
    for v in vms:
        st = runner.state(v, timeout=1.0)
        out.append({"id": v.id, "name": v.name, "state": st.status, "os": v.os.name,
                    "memory_mib": v.machine.memory_mib, "cpus": v.machine.cpus,
                    "pid": st.pid or None, "dir": str(v.dir)})
        rows.append([f"{status(st.status)} {st.status}", v.name, v.os.name or v.os.family,
                     f"{v.machine.cpus} CPU, {human(v.machine.memory_mib * 2**20)}", v.short_id])
    if args.json:
        print_json(out)
        return 0
    if not vms:
        print("No VMs yet. Create one with `rehome create --iso FILE` or import a disk with "
              "`rehome import SOURCE`.")
        return 0
    print(table(rows, ["State", "Name", "OS", "Resources", "ID"]))
    return 0


def describe(cfg) -> dict:
    from rehome.vm import runner
    st = runner.state(cfg)
    d = cfg.to_dict()
    d["state"] = st.status
    d["pid"] = st.pid or None
    d["dir"] = str(cfg.dir)
    return d


def cmd_info(args) -> int:
    from rehome.vm import library, runner, snapshots
    cfg = _vm(args.vm)
    if args.json:
        print_json(describe(cfg))
        return 0
    st = runner.state(cfg)
    m = cfg.machine
    print(bold(cfg.name) + dim(f"  ({cfg.id})"))
    print(f"  State:     {status(st.status)} {st.status}" + (f" (pid {st.pid})" if st.pid else ""))
    print(f"  OS:        {cfg.os.name or cfg.os.family}  [profile: {cfg.profile or '-'}]")
    print(f"  Machine:   {m.type}, {m.arch}, accel {m.accel}, CPU {m.cpu_model} × {m.cpus}, "
          f"{human(m.memory_mib * 2**20)} RAM")
    print(f"  Firmware:  {m.firmware}, TPM {m.tpm}, RTC {m.rtc}")
    for d in cfg.disks:
        src = ""
        if d.source:
            src = f"  ← {d.source.mode} {d.source.kind} {d.source.path}"
        print(f"  Disk:      {d.id} [{d.bus}] {d.file}{src}")
    for cd in cfg.cdroms:
        print(f"  CD/DVD:    {cd.id} [{cd.bus}] {cd.file or '(empty)'}")
    for n in cfg.nics:
        extra = f" via {n.bridge}" if n.mode == "bridge" else ""
        fw = f" forwards {', '.join(n.port_forwards)}" if n.port_forwards else ""
        print(f"  Network:   {n.id} {n.model} {n.mode}{extra} {n.mac}{fw}")
    print(f"  Display:   {cfg.display.device}{' + 3D' if cfg.display.gl else ''}; "
          f"sound {cfg.sound.device}; USB {cfg.usb.controller}; pointer {cfg.input.pointer}")
    for s in cfg.shares:
        print(f"  Share:     {s.tag} → {s.path}")
    if cfg.snapshots:
        print("  Checkpoints:")
        for line in snapshots.render_tree(cfg):
            print("    " + line)
    print(f"  Disk use:  {human(library.disk_usage(cfg))} in {cfg.dir}")
    return 0


def cmd_start(args) -> int:
    from rehome.vm import runner
    cfg = _vm(args.vm)
    st = runner.start(cfg, headless=args.headless, paused=args.paused,
                      restore=not args.no_restore, accel=args.accel,
                      allow_overcommit=args.allow_overcommit)
    try:
        meta = json.loads((runner.runtime(cfg).run / "state.json").read_text())
    except (OSError, ValueError):
        meta = {}
    accel = meta.get("accel", "?")
    print(f"Started {cfg.name} (pid {st.pid}, {accel}).")
    if accel == "tcg" and cfg.machine.accel_reason:
        print(dim(cfg.machine.accel_reason))
    elif accel == "tcg":
        print(c("Note: running without KVM (software emulation, much slower). See `rehome doctor`.", "33"))
    for w in meta.get("warnings", []):
        print(c("Note: ", "33") + w)
    from rehome.media import unattended
    for note in unattended.after_start(cfg, status=lambda s: print(dim(s + " …"))):
        print(note)
    if args.console:
        return cmd_console(args)
    return 0


def cmd_shutdown(args) -> int:
    from rehome.vm import runner
    cfg = _vm(args.vm)
    runner.shutdown(cfg)
    if args.force_after:
        if not runner.wait_stopped(cfg, args.force_after):
            print(f"{cfg.name} did not shut down in time; turning it off.")
            runner.force_off(cfg)
        else:
            runner.after_stop(cfg)
    elif args.wait:
        if not runner.wait_stopped(cfg, args.wait):
            raise RehomeError(f"{cfg.name} did not shut down within {int(args.wait)} s.",
                              fix="Use `rehome force-off` if the guest is stuck.")
        runner.after_stop(cfg)
    print(f"Asked {cfg.name} to shut down." if not (args.wait or args.force_after)
          else f"{cfg.name} is off.")
    return 0


def cmd_simple(args) -> int:
    from rehome.vm import runner
    cfg = _vm(args.vm)
    getattr(runner, args.op)(cfg)
    print({"force_off": "Turned off", "reset": "Reset", "pause": "Paused",
           "resume": "Resumed"}[args.op] + f" {cfg.name}.")
    return 0


def cmd_save(args) -> int:
    from rehome.vm import runner
    cfg = _vm(args.vm)
    runner.save(cfg)
    print(f"Saved {cfg.name}; it will resume where it left off at the next start.")
    return 0


def cmd_discard(args) -> int:
    from rehome.vm import runner
    cfg = _vm(args.vm)
    runner.discard_saved_state(cfg)
    print(f"Discarded the saved state of {cfg.name}.")
    return 0


def cmd_status(args) -> int:
    from rehome.vm import runner
    print(runner.state(_vm(args.vm)).status)
    return 0


def cmd_wait(args) -> int:
    from rehome.vm import runner
    cfg = _vm(args.vm)
    if not runner.wait_stopped(cfg, args.timeout):
        print(f"{cfg.name} is still running.", file=sys.stderr)
        return 1
    runner.after_stop(cfg)
    return 0


def cmd_screenshot(args) -> int:
    from rehome.vm import runner
    cfg = _vm(args.vm)
    runner.screenshot(cfg, os.path.abspath(args.file))
    print(args.file)
    return 0


def cmd_screen_text(args) -> int:
    from rehome.vm import runner
    print(runner.screen_text(_vm(args.vm)))
    return 0


def cmd_send_keys(args) -> int:
    from rehome.vm import runner
    runner.send_keys(_vm(args.vm), args.keys.split("-"))
    return 0


def cmd_type(args) -> int:
    from rehome.vm import runner
    runner.type_text(_vm(args.vm), args.text.encode().decode("unicode_escape"))
    return 0


def cmd_cd(args) -> int:
    from rehome.vm import library, runner
    cfg = _vm(args.vm)
    iso = os.path.abspath(args.iso) if args.iso else None
    if runner.state(cfg).active:
        runner.change_cd(cfg, args.drive, iso)
    with library.locked(cfg):
        cfg = library.reload(cfg)
        for cd in cfg.cdroms:
            if cd.id == args.drive:
                cd.file = iso or ""
                break
        else:
            raise RehomeError(f"{cfg.name} has no CD drive called {args.drive}.")
        library.save(cfg)
    print(f"{'Inserted ' + args.iso if iso else 'Ejected'} ({args.drive}).")
    return 0


def cmd_set(args) -> int:
    from rehome.vm import edit
    cfg = _vm(args.vm)
    changes = edit.apply_cli_changes(cfg, args)
    if not changes:
        print("Nothing to change.")
    for ch in changes:
        print("• " + ch)
    return 0


def cmd_delete(args) -> int:
    from rehome.vm import library
    cfg = _vm(args.vm)
    if not args.yes:
        if not sys.stdin.isatty():
            raise RehomeError("Refusing to delete without confirmation.", fix="Pass --yes.")
        what = "the VM's configuration (its disks will be kept)" if args.keep_disks else \
            "the VM and ALL of its disks and checkpoints"
        reply = input(f"Delete \"{cfg.name}\"? This removes {what}. Type the VM name to confirm: ")
        if reply.strip() != cfg.name:
            print("Not deleted.")
            return 1
    kept = library.delete(cfg, keep_disks=args.keep_disks)
    print(f"Deleted {cfg.name}." + (f" Disks kept in {kept}" if kept else ""))
    return 0


def cmd_clone(args) -> int:
    from rehome.vm import lifecycle
    new = lifecycle.clone(_vm(args.vm), args.name, progress=_progress_printer("Cloning"))
    print(f"Created {new.name} ({new.short_id}).")
    return 0


def cmd_export(args) -> int:
    from rehome.vm import lifecycle
    out = lifecycle.export(_vm(args.vm), args.dest, compress=args.compress,
                           progress=_progress_printer("Exporting"))
    print(out)
    return 0


def cmd_import_vm(args) -> int:
    from rehome.vm import lifecycle
    cfg = lifecycle.import_bundle(args.file, args.name)
    print(f"Imported {cfg.name} ({cfg.short_id}).")
    return 0


def cmd_logs(args) -> int:
    cfg = _vm(args.vm)
    names = {"qemu": "qemu.log", "serial": "serial.log", "import": "import.log",
             "patch": "patch.log", "swtpm": "swtpm.log"}
    f = cfg.dir / "logs" / names.get(args.which, args.which)
    if not f.exists():
        print(f"No {args.which} log yet ({f}).")
        return 0
    lines = f.read_text(errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))
    return 0


def cmd_diagnostics(args) -> int:
    from rehome.vm import diagnostics
    print(diagnostics.collect(_vm(args.vm)))
    return 0


def cmd_console(args) -> int:
    cfg = _vm(args.vm)
    if os.geteuid() == 0:
        raise RehomeError("The console must not run as root.")
    if args.external:
        from rehome.vm import runner
        argv = runner.external_viewer(cfg)
        subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"Opened {os.path.basename(argv[0])}.")
        return 0
    try:
        from rehome.ui.app import run as run_gui
    except (ImportError, ValueError) as e:
        raise RehomeError("The graphical console is unavailable.", details=str(e),
                          fix="Install GTK 4 and libadwaita (see `rehome doctor`).") from e
    return run_gui([sys.argv[0], "--console", cfg.id])
