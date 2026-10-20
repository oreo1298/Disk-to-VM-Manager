"""`rehome inspect` — identify the installed system without importing."""

from __future__ import annotations

import sys

from rehome.cli.output import bold, c, dim, print_json
from rehome.util.units import human


def register(sub) -> None:
    p = sub.add_parser("inspect", help="Identify what is installed on disks or images (read-only)")
    p.add_argument("sources", nargs="+", metavar="SOURCE", help="/dev/sdX or an image file")
    p.add_argument("--key", action="append", default=[],
                   help="BitLocker recovery key or LUKS passphrase (repeatable)")
    p.add_argument("--no-smart", action="store_true", help="skip the disk health check")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def progress_printer():
    from rehome import jobs
    tty = sys.stderr.isatty()

    def on_change(p):
        if not tty:
            return
        line = f"{p.stage_name}: {p.detail}" if p.detail else p.stage_name
        sys.stderr.write("\r\x1b[K" + line[:110])
        sys.stderr.flush()
    return jobs.Progress(on_change=on_change)


def print_analysis(a, show_preview: bool = True) -> None:
    for s in a.sources:
        print(bold(s.display) + dim(f"  ({s.stable_path or s.path})"))
        h = a.health.get(s.path)
        if h:
            col = {"healthy": "32", "warning": "33", "failing": "31"}.get(h.verdict, "2")
            print("  Health: " + c(h.verdict, col) + " — " + h.summary)
            for d in h.details:
                print(dim("    " + d))
    for d in a.report.disks:
        vols = ", ".join(f"{v.fstype or '?'} {human(v.size)}" + (f" [{v.encryption}]" if v.encryption else "")
                         for v in d.volumes)
        print(dim(f"  Disk {d.index + 1}: {d.scheme} — {vols}"))
    if not a.report.oses:
        print(c("  No operating system found.", "33"))
    for i, o in enumerate(a.report.oses):
        mark = "▶" if i == a.os_index else " "
        print()
        print(f"{mark} {bold(o.label)}")
        rows = [("Family", f"{o.family} ({o.distro})" if o.distro else o.family),
                ("Version", " ".join(x for x in (o.version, f"build {o.build}" if o.build else "") if x)),
                ("Edition", o.edition or o.details.get("edition_id", "")),
                ("Architecture", o.arch), ("Boot", f"{o.boot_mode.upper()} — {o.bootloader}".strip(" —")),
                ("Original disk controller", o.original_bus or "unknown"),
                ("Clock", o.rtc), ("Kernel", o.kernel), ("Initramfs", o.initramfs_tool),
                ("Virtio drivers", {True: "present", False: "missing (will be added)", None: ""}[o.virtio_ready]),
                ("HAL", o.hal), ("Hostname", o.hostname if o.hostname != "unknown" else ""),
                ("Hibernated", "yes" if o.hibernated else ""),
                ("Fast Startup", "on" if o.fast_startup else ""),
                ("Other hypervisor tools", ", ".join(o.hypervisor_tools)),
                ("Encryption", ", ".join(o.encryption)),
                ("Proprietary GPU driver", ", ".join(o.gpu_drivers))]
        for k, v in rows:
            if v:
                print(f"    {k + ':':<26}{v}")
        for w in o.warnings:
            print(c("    ⚠ " + w, "33"))
    for n in a.notes:
        if not any(n == w for o in a.report.oses for w in o.warnings):
            print(dim("  • " + n))
    for p in a.report.problems:
        print(c("  ✖ " + p, "31"))
    if a.report.encrypted_locked:
        print(c(f"  Locked encrypted volumes: {', '.join(a.report.encrypted_locked)} — pass --key.", "33"))
    if show_preview and a.preview:
        m = a.preview.machine
        print()
        print(bold("Virtual machine") + dim(f"  (profile: {a.profile.name if a.profile else '-'})"))
        print(f"    {m.type}, {m.firmware}, TPM {m.tpm}, {m.cpus} CPU, {human(m.memory_mib * 2**20)} RAM, "
              f"clock {m.rtc}, accel {m.accel}")
        buses = ", ".join(f"{d.id}:{d.bus}" for d in a.preview.disks)
        nics = ", ".join(n.model for n in a.preview.nics)
        print(f"    disks {buses}; network {nics}; display {a.preview.display.device}; "
              f"sound {a.preview.sound.device}")
        if a.profile:
            print(dim("    fixes: " + ", ".join(a.profile.patches or ["none"])))
    for b in a.blockers:
        print(c("✖ " + b, "31"))


def run(args) -> int:
    from rehome import importer
    prog = progress_printer()
    a = importer.analyze(args.sources, keys=args.key, smart_check=not args.no_smart, progress=prog)
    if sys.stderr.isatty():
        sys.stderr.write("\r\x1b[K")
    if args.json:
        print_json(a.to_dict())
        return 0
    print_analysis(a)
    return 0
