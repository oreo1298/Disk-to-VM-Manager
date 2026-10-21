"""`rehome quick-create` and `rehome download`: the Quick Create catalog."""

from __future__ import annotations

import getpass
import sys
import time
from pathlib import Path

from rehome.cli.output import bold, c, dim, print_json, table
from rehome.errors import RehomeError
from rehome.util.units import human, mib, parse_size


def register(sub) -> None:
    p = sub.add_parser(
        "quick-create", help="Download a free operating system and create a VM for it",
        description="Download an operating system from its official source (verified against "
                    "the published checksum) and create a VM for it, like Hyper-V's Quick "
                    "Create. Windows entries only show Microsoft's download page.")
    p.add_argument("id", nargs="?", help="catalog entry, see --list")
    p.add_argument("--list", action="store_true", help="list the available systems")
    p.add_argument("--name", help="VM name (default: the OS name and version)")
    p.add_argument("--start", action="store_true", help="start the VM after creating it")
    p.add_argument("--dry-run", action="store_true",
                   help="only look up the current release (URL and checksum)")
    p.add_argument("--memory", help="e.g. 4G or 4096 (MiB)")
    p.add_argument("--cpus", type=int)
    p.add_argument("--disk", help="disk size, e.g. 64G")
    add_unattended_args(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_quick_create)

    p = sub.add_parser("download", help="Download and verify a Quick Create image (no VM)")
    p.add_argument("id", help="catalog entry, see `rehome quick-create --list`")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("unattended-cleanup",
                       help="Remove the answer disc and files of a finished unattended install",
                       description="The generated answer files contain the account password. "
                                   "Once the installation has finished, this removes them and "
                                   "the extra CD drive from the VM.")
    p.add_argument("vm")
    p.set_defaults(func=cmd_unattended_cleanup)


# --- shared with `rehome create` -----------------------------------------------------------

def add_unattended_args(p) -> None:
    g = p.add_argument_group("unattended installation")
    g.add_argument("--unattended", action="store_true",
                   help="install automatically (Windows, Ubuntu, Fedora Server, Debian, "
                        "openSUSE and other systems libosinfo has scripts for)")
    g.add_argument("--user", default="", help="user name (default: your login name)")
    g.add_argument("--password", default="",
                   help="password (other users can see command lines; prefer --password-file "
                        "or the prompt)")
    g.add_argument("--password-file", help="read the password from this file ('-' = stdin)")
    g.add_argument("--full-name", default="")
    g.add_argument("--product-key", default="",
                   help="Windows product key (not needed for evaluation media)")
    g.add_argument("--edition", default="",
                   help="Windows image to install: name (\"Windows 11 Pro\") or index (6)")
    g.add_argument("--locale", default="", help="e.g. en_US (default: this computer's)")
    g.add_argument("--keyboard", default="", help="e.g. us, de (default: this computer's)")
    g.add_argument("--timezone", default="", help="e.g. Europe/Berlin (default: this computer's)")
    g.add_argument("--hostname", default="", help="computer name (default: from the VM name)")


def _read_password(args) -> str:
    if args.password_file:
        if args.password_file == "-":
            text = sys.stdin.readline()
        else:
            try:
                text = Path(args.password_file).read_text()
            except OSError as e:
                raise RehomeError(f"The password file {args.password_file} could not be read.",
                                  details=str(e)) from e
        return text.rstrip("\r\n")
    if args.password:
        return args.password
    if sys.stdin.isatty():
        first = getpass.getpass("Password for the new account: ")
        if first != getpass.getpass("Repeat the password: "):
            raise RehomeError("The passwords do not match.")
        return first
    return ""


def answers_from_args(args):
    if not getattr(args, "unattended", False):
        return None
    from rehome.media import unattended
    return unattended.Answers(
        user=args.user, password=_read_password(args), full_name=args.full_name,
        product_key=args.product_key, edition=args.edition, locale=args.locale,
        keyboard=args.keyboard, timezone=args.timezone, hostname=args.hostname)


class ProgressPrinter:
    """Download progress on stderr: a live line on terminals, every 10 % otherwise."""

    def __init__(self, stream=None):
        self.stream = stream = stream or sys.stderr
        self.tty = stream.isatty()
        self.last_pct = -10
        self.label = ""
        self.active = False

    def status(self, text: str) -> None:
        self.finish()
        self.label = text
        print(dim(text + " …"), file=self.stream, flush=True)

    def __call__(self, done: int, total: int, speed: float, eta) -> None:
        pct = int(done * 100 / total) if total else 0
        parts = [f"{human(done)}" + (f" of {human(total)}" if total else "")]
        if speed:
            parts.append(f"{human(int(speed))}/s")
        if eta is not None and done < total:
            parts.append(_eta(eta) + " left")
        line = (f"{pct:3d}%  " if total else "") + ", ".join(parts)
        if self.tty:
            self.stream.write("\r\x1b[K  " + line)
            self.stream.flush()
            self.active = True
        elif pct >= self.last_pct + 10 or (total and done >= total and self.last_pct < 100):
            self.last_pct = pct - pct % 10
            print("  " + line, file=self.stream, flush=True)

    def finish(self) -> None:
        if self.active:
            self.stream.write("\n")
            self.stream.flush()
            self.active = False


def _eta(sec: float) -> str:
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    return f"{sec // 3600} h {sec % 3600 // 60} min"


# --- commands ------------------------------------------------------------------------------

def cmd_quick_create(args) -> int:
    from rehome.media import catalog
    if args.list or not args.id:
        return _list(args)
    entry = catalog.get(args.id)
    if args.dry_run:
        if entry.external:
            out = {"id": entry.id, "name": entry.name, "external_url": entry.url}
            print_json(out) if args.json else print(f"{entry.name}: download it from {entry.url}")
            return 0
        res = catalog.resolve(entry)
        d = res.to_dict()
        d["osinfo"] = res.osinfo_id()
        if args.json:
            print_json(d)
        else:
            print(bold(res.label))
            print(f"  URL:          {res.url}")
            print(f"  Checksum:     {res.checksum.algo}:{res.checksum.value}" if res.checksum
                  else "  Checksum:     (none published)")
            print(f"  Verification: {res.verification}")
            print(f"  VM defaults:  {d['osinfo']}")
        return 0
    answers = answers_from_args(args)
    overrides = {}
    if args.memory:
        overrides["memory_mib"] = mib(args.memory)
    if args.cpus:
        overrides["cpus"] = args.cpus
    if args.disk:
        overrides["disk_size"] = parse_size(args.disk)
    prog = ProgressPrinter()
    try:
        result = catalog.quick_create(args.id, name=args.name, start=args.start, answers=answers,
                                      progress=None if args.json else prog,
                                      status=None if args.json else prog.status,
                                      spec_overrides=overrides)
    finally:
        prog.finish()
    if result.external_url:
        if args.json:
            print_json({"id": entry.id, "external_url": result.external_url,
                        "notes": result.notes})
        else:
            print(f"{bold(entry.name)} is not downloaded by Rehome. Get it from Microsoft:")
            print(f"  {result.external_url}")
            print("Then create the VM from the ISO, for example:")
            print("  rehome create --iso ~/Downloads/<file>.iso"
                  + (" --unattended --user NAME --start" if entry.unattended else ""))
        return 0
    cfg = result.config
    if args.json:
        print_json({"id": cfg.id, "name": cfg.name, "dir": str(cfg.dir), "iso": str(result.iso),
                    "version": result.resolved.version, "started": result.started,
                    "verification": result.resolved.verification, "notes": result.notes})
        return 0
    print(f"Verified {result.iso.name} ({result.resolved.verification}).")
    print(f"Created {bold(cfg.name)} ({cfg.short_id}) in {cfg.dir}")
    for n in result.notes:
        print("  " + n)
    if result.started:
        print("Started. Open the console with: " + bold(f"rehome console {cfg.short_id}"))
    else:
        print("Start it with: " + bold(f"rehome start {cfg.short_id}"))
    return 0


def _list(args) -> int:
    from rehome.media import catalog
    entries = catalog.load()
    if args.json:
        print_json([{"id": e.id, "name": e.name, "description": e.description, "kind": e.kind,
                     "url": e.url or None, "homepage": e.homepage, "license": e.license,
                     "unattended": e.unattended, "unattended_note": e.unattended_note or None,
                     "size": e.size, "verification": e.verification or None}
                    for e in entries])
        return 0
    rows = []
    for e in entries:
        source = "Microsoft (manual)" if e.external else "download"
        rows.append([e.id, e.name, source, "yes" if e.unattended else "-", e.size])
    print(table(rows, ["ID", "Name", "Source", "Unattended", "Size"]))
    print(dim("\nrehome quick-create ID [--unattended --user NAME] [--start]"))
    return 0


def cmd_unattended_cleanup(args) -> int:
    from rehome.media import unattended
    from rehome.vm import library
    cfg = library.find(args.vm)
    if not unattended.pending(cfg):
        print(f"{cfg.name} has no unattended-installation files.")
        return 0
    unattended.cleanup(cfg)
    print(f"Removed the answer disc and files from {bold(cfg.name)}.")
    return 0


def cmd_download(args) -> int:
    from rehome.media import catalog
    entry = catalog.get(args.id)
    if entry.external:
        raise RehomeError(f"Rehome does not download {entry.name}.",
                          fix=f"Download it from Microsoft: {entry.url}")
    prog = ProgressPrinter()
    t0 = time.monotonic()
    try:
        prog.status(f"Looking up the current {entry.name} release")
        res = catalog.resolve(entry)
        path = catalog.download_image(res, progress=None if args.json else prog,
                                      status=None if args.json else prog.status)
    finally:
        prog.finish()
    if args.json:
        print_json({"path": str(path), **res.to_dict()})
    else:
        print(f"{bold(res.label)}: {path}")
        print(c(f"Verified: {res.verification}", "32") if res.checksum else
              c(f"Note: {res.verification}", "33"))
        print(dim(f"({int(time.monotonic() - t0)} s)"))
    return 0
