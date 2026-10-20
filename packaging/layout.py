#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rehome's installation layout: the single source of truth.

Used by ``make install``, ``install.sh`` and the .deb/.rpm/Arch packages, so
every way of installing Rehome puts the same files in the same places.

    layout.py install   [--prefix P] [--destdir D] [--libexecdir L] [--python PY]
    layout.py uninstall [--prefix P] [--destdir D] [--libexecdir L] | --manifest FILE
    layout.py list      [--prefix P] [--libexecdir L]      # installed paths
    layout.py version                                       # Rehome version
    layout.py deps --family debian [--required|--optional] [--format ...]

Layout (PREFIX defaults to /usr/local):

    PREFIX/lib/rehome/rehome/...           Python package
    PREFIX/bin/rehome                      generated launcher
    PREFIX/share/rehome/...                data (profiles, packages.toml, ...)
    LIBEXECDIR/rehome-helper               polkit helper, ``#!PYTHON -I``
                                           (LIBEXECDIR defaults to PREFIX/libexec/rehome)
    /usr/share/polkit-1/actions/APP.policy polkit only reads this directory
    PREFIX/share/applications/APP.desktop
    PREFIX/share/metainfo/APP.metainfo.xml
    PREFIX/share/icons/hicolor/{scalable,symbolic}/apps/APP[-symbolic].svg

The script only uses the standard library and never imports Rehome itself, so
an installed copy (install.sh keeps one in PREFIX/share/rehome/installer/) can
uninstall Rehome without the source tree.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

APP_ID = "io.github.oreo1298.Rehome"
POLKIT_DIR = "/usr/share/polkit-1/actions"
MANIFEST_NAME = "install-manifest.json"
MANIFEST_SCHEMA = 1
FAMILIES = ("arch", "debian", "fedora", "suse")
SOURCE_ROOT = Path(__file__).resolve().parent.parent

# Files in the Python package that are never installed.
_SKIP_SUFFIXES = (".pyc", ".pyo", "~", ".swp", ".orig", ".rej")
# Files in data/ installed elsewhere (templates and desktop integration).
_DATA_SKIP_SUFFIXES = (".in", ".desktop", ".metainfo.xml")


class LayoutError(Exception):
    pass


# --- layout -----------------------------------------------------------------------

@dataclass
class Entry:
    """One installed file: where it goes and how to produce its content."""
    dest: str                  # absolute path on the target system (without DESTDIR)
    mode: int
    src: Path | None = None    # copy (or template) source
    subst: dict[str, str] | None = None   # @KEY@ -> value substitutions for templates
    content: str | None = None            # generated content
    shebang: str | None = None            # replace the first line with this shebang


@dataclass
class Layout:
    prefix: str = "/usr/local"
    libexecdir: str | None = None
    python: str = "/usr/bin/python3"
    destdir: str = ""
    source: Path = field(default=SOURCE_ROOT)

    def __post_init__(self) -> None:
        self.prefix = _norm_abs(self.prefix, "--prefix")
        self.libexecdir = _norm_abs(self.libexecdir or f"{self.prefix}/libexec/rehome",
                                    "--libexecdir")
        if not os.path.isabs(self.python):
            raise LayoutError(f"--python must be an absolute path, not {self.python!r}")
        self.source = Path(self.source).resolve()

    # Directories --------------------------------------------------------------------
    @property
    def bindir(self) -> str:
        return f"{self.prefix}/bin"

    @property
    def libdir(self) -> str:
        return f"{self.prefix}/lib/rehome"

    @property
    def datadir(self) -> str:
        return f"{self.prefix}/share/rehome"

    @property
    def appsdir(self) -> str:
        return f"{self.prefix}/share/applications"

    @property
    def metadir(self) -> str:
        return f"{self.prefix}/share/metainfo"

    @property
    def icondir(self) -> str:
        return f"{self.prefix}/share/icons/hicolor"

    @property
    def helper(self) -> str:
        return f"{self.libexecdir}/rehome-helper"

    @property
    def launcher(self) -> str:
        return f"{self.bindir}/rehome"

    @property
    def manifest(self) -> str:
        return f"{self.datadir}/{MANIFEST_NAME}"

    @property
    def installer_dir(self) -> str:
        return f"{self.datadir}/installer"

    def single_files(self) -> list[str]:
        """Files outside Rehome's own directories."""
        return [
            self.launcher,
            f"{POLKIT_DIR}/{APP_ID}.policy",
            f"{self.appsdir}/{APP_ID}.desktop",
            f"{self.metadir}/{APP_ID}.metainfo.xml",
            f"{self.icondir}/scalable/apps/{APP_ID}.svg",
            f"{self.icondir}/symbolic/apps/{APP_ID}-symbolic.svg",
        ]

    def owned_dirs(self) -> list[str]:
        """Directories that belong to Rehome alone (removed when empty)."""
        dirs = [f"{self.libdir}/rehome", self.libdir, self.datadir]
        if os.path.basename(self.libexecdir) == "rehome":
            dirs.append(self.libexecdir)
        return dirs

    def owns(self, path: str) -> bool:
        """True if *path* is a file location Rehome may install (and remove)."""
        path = os.path.normpath(path)
        if path in self.single_files() or path == self.helper:
            return True
        return any(path.startswith(d + "/") for d in self.owned_dirs())

    # Entries ------------------------------------------------------------------------
    def entries(self, installer_copy: bool = False) -> list[Entry]:
        src = self.source
        if not (src / "rehome" / "__init__.py").is_file():
            raise LayoutError(f"{src} is not a Rehome source tree (rehome/__init__.py is missing).")
        out: list[Entry] = []
        helper_src = src / "rehome" / "helper" / "rehome-helper"
        for f in _walk(src / "rehome"):
            if f == helper_src or f.name.endswith(_SKIP_SUFFIXES):
                continue
            out.append(Entry(f"{self.libdir}/{f.relative_to(src).as_posix()}", 0o644, src=f))
        data = src / "data"
        for f in _walk(data):
            rel = f.relative_to(data).as_posix()
            if rel.startswith("icons/") or f.name.endswith(_DATA_SKIP_SUFFIXES):
                continue
            out.append(Entry(f"{self.datadir}/{rel}", 0o644, src=f))
        out.append(Entry(self.helper, 0o755, src=helper_src, shebang=f"#!{self.python} -I"))
        out.append(Entry(f"{POLKIT_DIR}/{APP_ID}.policy", 0o644,
                         src=data / f"{APP_ID}.policy.in", subst={"HELPER": self.helper}))
        out.append(Entry(self.launcher, 0o755, content=self.launcher_text()))
        out.append(Entry(f"{self.appsdir}/{APP_ID}.desktop", 0o644,
                         src=data / f"{APP_ID}.desktop", subst={"BINDIR": self.bindir}))
        out.append(Entry(f"{self.metadir}/{APP_ID}.metainfo.xml", 0o644,
                         src=data / f"{APP_ID}.metainfo.xml"))
        out.append(Entry(f"{self.icondir}/scalable/apps/{APP_ID}.svg", 0o644,
                         src=data / "icons" / f"{APP_ID}.svg"))
        out.append(Entry(f"{self.icondir}/symbolic/apps/{APP_ID}-symbolic.svg", 0o644,
                         src=data / "icons" / f"{APP_ID}-symbolic.svg"))
        if installer_copy:
            # install.sh keeps itself and this script so --update/--uninstall work offline.
            out.append(Entry(f"{self.installer_dir}/install.sh", 0o755, src=src / "install.sh"))
            out.append(Entry(f"{self.installer_dir}/layout.py", 0o644,
                             src=src / "packaging" / "layout.py"))
        for e in out:
            if e.src is not None and not e.src.is_file():
                raise LayoutError(f"Missing source file: {e.src}")
        return out

    def files(self, installer_copy: bool = False) -> list[str]:
        return [e.dest for e in self.entries(installer_copy)]

    def launcher_text(self) -> str:
        return "\n".join([
            f"#!{self.python}",
            '"""Rehome launcher (generated by packaging/layout.py; do not edit)."""',
            "import os",
            "import sys",
            "",
            "# Keep compiled bytecode in the user's cache, never next to the installed",
            "# sources, so the package manager or uninstaller removes every file.",
            "if sys.pycache_prefix is None:",
            '    _cache = os.environ.get("XDG_CACHE_HOME", "")',
            "    if not os.path.isabs(_cache):",
            '        _cache = os.path.join(os.path.expanduser("~"), ".cache")',
            '    sys.pycache_prefix = os.path.join(_cache, "rehome", "pycache")',
            f"sys.path.insert(0, {self.libdir!r})",
            f'os.environ.setdefault("REHOME_DATA_DIR", {self.datadir!r})',
            f'os.environ.setdefault("REHOME_HELPER_PATH", {self.helper!r})',
            "from rehome.cli.main import main  # noqa: E402",
            "",
            "sys.exit(main())",
            "",
        ])

    def target(self, path: str) -> Path:
        """Where *path* lives on disk, taking DESTDIR into account."""
        return Path(self.destdir + path) if self.destdir else Path(path)


def _norm_abs(p: str, what: str) -> str:
    if not p or not os.path.isabs(p):
        raise LayoutError(f"{what} must be an absolute path, not {p!r}")
    p = os.path.normpath(p)
    return "/" if p == "//" else p


def _walk(root: Path) -> list[Path]:
    """Regular files below *root*, sorted, skipping caches and hidden files."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath, name)
            if not name.startswith(".") and p.is_file() and not p.is_symlink():
                out.append(p)
    return out


def read_version(source: Path = SOURCE_ROOT) -> str:
    text = (Path(source) / "rehome" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise LayoutError("Could not find __version__ in rehome/__init__.py")
    return m.group(1)


# --- install ----------------------------------------------------------------------

def _render(e: Entry) -> bytes:
    if e.content is not None:
        return e.content.encode()
    assert e.src is not None
    data = e.src.read_bytes()
    if e.subst:
        text = data.decode("utf-8")
        for k, v in e.subst.items():
            text = text.replace(f"@{k}@", v)
        data = text.encode("utf-8")
    if e.shebang:
        rest = data.split(b"\n", 1)[1] if data.startswith(b"#!") else data
        data = e.shebang.encode() + b"\n" + rest
    return data


def _makedirs(path: Path, created: list[Path]) -> None:
    """mkdir -p that records each directory it creates (mode 0755 regardless of umask)."""
    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir()
        os.chmod(d, 0o755)
        created.append(d)


def _prune_empty_parents(layout: Layout, path: str) -> None:
    """Remove now-empty parent directories of *path* inside Rehome's own directories."""
    roots = layout.owned_dirs()
    d = os.path.dirname(path)
    while any(d.startswith(r + "/") for r in roots):
        try:
            layout.target(d).rmdir()
        except OSError:
            return
        d = os.path.dirname(d)


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def install(layout: Layout, *, installer_copy: bool = False, manifest: bool = False,
            manifest_data: dict | None = None) -> dict:
    """Install every file; return {"files": [...], "directories": [...]}.

    With *manifest*, also write PREFIX/share/rehome/install-manifest.json from
    *manifest_data* plus the file list, and remove files an earlier install
    listed that this version no longer ships.
    """
    entries = layout.entries(installer_copy)
    old = read_manifest(layout.target(layout.manifest)) if manifest else None
    created: list[Path] = []
    if layout.destdir:
        os.makedirs(layout.destdir, exist_ok=True)
    for e in entries:
        target = layout.target(e.dest)
        _makedirs(target.parent, created)
        if target.is_dir() and not target.is_symlink():
            raise LayoutError(f"{target} is a directory; refusing to replace it with a file.")
        _write_atomic(target, _render(e), e.mode)
    files = [e.dest for e in entries]
    strip = len(layout.destdir)
    owned = layout.owned_dirs()
    # Directories inside Rehome's own ones go with them; record only the others
    # (e.g. PREFIX/share/metainfo on a fresh /usr/local) so uninstall can remove them.
    dirs = [str(d)[strip:] for d in created]
    dirs = [d for d in dirs if not any(d == r or d.startswith(r + "/") for r in owned)]
    result = {"files": files, "directories": dirs}
    if manifest:
        files = files + [layout.manifest]
        stale: list[str] = []
        if old:
            keep = set(files)
            stale = [f for f in old.get("files", []) if f not in keep]
            for f in _remove_files(layout, stale):
                _prune_empty_parents(layout, f)
            dirs = _unique(old.get("directories", []) + dirs)
        now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        data = dict(manifest_data or {})
        data.update({
            "schema": MANIFEST_SCHEMA,
            "app": "rehome",
            "version": read_version(layout.source),
            "prefix": layout.prefix,
            "libexecdir": layout.libexecdir,
            "python": layout.python,
            "installed_at": (old or {}).get("installed_at", now),
            "updated_at": now,
            "files": files,
            "directories": dirs,
        })
        target = layout.target(layout.manifest)
        _write_atomic(target, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), 0o644)
        result = {"files": files, "directories": dirs, "removed_stale": stale}
    return result


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in items if not (x in seen or seen.add(x))]


# --- uninstall --------------------------------------------------------------------

def read_manifest(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise LayoutError(f"Cannot read the install manifest {path}: {e}") from e
    if not isinstance(data, dict) or data.get("app") != "rehome":
        raise LayoutError(f"{path} is not a Rehome install manifest.")
    return data


def _remove_files(layout: Layout, files: list[str]) -> list[str]:
    removed = []
    for f in files:
        if not layout.owns(f):
            print(f"warning: not removing {f}: outside Rehome's install locations", file=sys.stderr)
            continue
        t = layout.target(f)
        if t.is_symlink() or t.is_file():
            t.unlink()
            removed.append(f)
    return removed


def _remove_pycache(layout: Layout) -> None:
    """Bytecode Python may have written next to the sources (e.g. when run as root
    without the launcher). Only __pycache__ dirs holding nothing but .pyc files."""
    pkg = layout.target(f"{layout.libdir}/rehome")
    if not pkg.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(pkg, topdown=False):
        if os.path.basename(dirpath) == "__pycache__" and not dirnames \
                and all(n.endswith((".pyc", ".pyo")) for n in filenames):
            for n in filenames:
                os.unlink(os.path.join(dirpath, n))
            os.rmdir(dirpath)


def _rmdir_empty_tree(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        return
    for dirpath, _dirnames, _files in os.walk(path, topdown=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def _owned_tree_files(layout: Layout) -> list[str]:
    """Every file currently inside Rehome's own directories."""
    out: list[str] = []
    strip = len(layout.destdir)
    for d in layout.owned_dirs():
        root = layout.target(d)
        if root.is_dir() and not root.is_symlink():
            for dirpath, _dirs, names in os.walk(root):
                out.extend(os.path.join(dirpath, n)[strip:] for n in names)
    return out


def uninstall(layout: Layout, manifest: dict | None = None) -> list[str]:
    """Remove Rehome's files and return them.

    With *manifest*, exactly the files it lists (each must be inside Rehome's
    install locations). Without one (``make uninstall``), the files of the
    computed layout plus anything left inside Rehome's own directories.
    """
    if manifest is not None:
        files = list(manifest.get("files", []))
        dirs = list(manifest.get("directories", []))
    else:
        files = _unique(layout.single_files() + [layout.helper] + _owned_tree_files(layout))
        dirs = []
    removed = _remove_files(layout, [f for f in files if f != layout.manifest])
    _remove_pycache(layout)
    if layout.manifest in files and _remove_files(layout, [layout.manifest]):
        removed.append(layout.manifest)
    for d in sorted(layout.owned_dirs(), key=len, reverse=True):
        _rmdir_empty_tree(layout.target(d))
    # Directories the installer created (e.g. share/metainfo on a fresh /usr/local),
    # deepest first, only when empty.
    for d in sorted(dirs, key=lambda p: p.count("/"), reverse=True):
        t = layout.target(d)
        if t.is_dir() and not t.is_symlink():
            try:
                t.rmdir()
            except OSError:
                pass
    return removed


# --- dependencies -------------------------------------------------------------------

def load_components(source: Path = SOURCE_ROOT) -> dict:
    import tomllib
    with open(Path(source) / "data" / "packages.toml", "rb") as f:
        return tomllib.load(f)


def deps(family: str, which: str = "required", source: Path = SOURCE_ROOT) -> list[dict]:
    """Components of packages.toml for *family*: [{key, label, required, packages}]."""
    if family not in FAMILIES:
        raise LayoutError(f"Unknown family {family!r}; expected one of {', '.join(FAMILIES)}")
    out = []
    for key, comp in load_components(source).items():
        required = bool(comp.get("required", False))
        if (which == "required" and not required) or (which == "optional" and required):
            continue
        out.append({"key": key, "label": comp.get("label", key), "required": required,
                    "packages": list(comp.get(family, []))})
    return out


def _flat(components: list[dict]) -> list[str]:
    return _unique([p for c in components for p in c["packages"]])


# --- command line -------------------------------------------------------------------

def _layout_from(args, prefix: str | None = None, libexecdir: str | None = None) -> Layout:
    return Layout(prefix=prefix or args.prefix, libexecdir=libexecdir or args.libexecdir,
                  python=getattr(args, "python", None) or "/usr/bin/python3",
                  destdir=(getattr(args, "destdir", "") or "").rstrip("/"),
                  source=Path(getattr(args, "source", None) or SOURCE_ROOT))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="layout.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def common(q, python=True, destdir=True):
        q.add_argument("--prefix", default="/usr/local", help="installation prefix (default /usr/local)")
        q.add_argument("--libexecdir", default=None,
                       help="directory for rehome-helper (default PREFIX/libexec/rehome)")
        if destdir:
            q.add_argument("--destdir", default=os.environ.get("DESTDIR", ""),
                           help="staging root for packages (default $DESTDIR)")
        if python:
            q.add_argument("--python", default="/usr/bin/python3",
                           help="interpreter for the launcher and helper shebangs")
        q.add_argument("--source", default=None, help=argparse.SUPPRESS)

    q = sub.add_parser("install", help="install Rehome's files and print them")
    common(q)
    q.add_argument("--installer-copy", action="store_true",
                   help="also keep install.sh and layout.py in PREFIX/share/rehome/installer")
    q.add_argument("--manifest", action="store_true",
                   help="write PREFIX/share/rehome/install-manifest.json (install.sh)")
    q.add_argument("--manifest-data", metavar="JSON",
                   help="extra manifest fields (packages, groups, ...) from this JSON file")
    q.add_argument("--quiet", action="store_true", help="do not print the file list")

    q = sub.add_parser("uninstall", help="remove Rehome's files")
    common(q, python=False)
    q.add_argument("--manifest", metavar="FILE",
                   help="remove exactly what this install manifest lists")

    q = sub.add_parser("list", help="print the files 'install' would install")
    common(q, destdir=False)
    q.add_argument("--installer-copy", action="store_true")
    q.add_argument("--json", action="store_true", help="also print directories as JSON")

    q = sub.add_parser("version", help="print Rehome's version")
    q.add_argument("--source", default=None, help=argparse.SUPPRESS)

    q = sub.add_parser("deps", help="print distribution packages from data/packages.toml")
    q.add_argument("--family", required=True, choices=FAMILIES)
    g = q.add_mutually_exclusive_group()
    g.add_argument("--required", dest="which", action="store_const", const="required")
    g.add_argument("--optional", dest="which", action="store_const", const="optional")
    g.add_argument("--all", dest="which", action="store_const", const="all")
    q.set_defaults(which="required")
    q.add_argument("--format", default="lines", choices=("lines", "words", "deb", "tsv", "json"),
                   help="lines: one package per line; words: space separated; deb: comma "
                        "separated; tsv: key, required, label, packages per component")
    q.add_argument("--source", default=None, help=argparse.SUPPRESS)

    args = p.parse_args(argv)
    try:
        return _run(args)
    except LayoutError as e:
        print(f"layout.py: error: {e}", file=sys.stderr)
        return 1
    except PermissionError as e:
        print(f"layout.py: error: {e} (installing to {getattr(args, 'prefix', '?')} "
              "needs root; use sudo, or --destdir to stage)", file=sys.stderr)
        return 1


def _run(args) -> int:
    source = Path(args.source or SOURCE_ROOT)
    if args.cmd == "version":
        print(read_version(source))
        return 0
    if args.cmd == "deps":
        comps = deps(args.family, args.which, source)
        if args.format == "json":
            print(json.dumps(comps, indent=2))
        elif args.format == "tsv":
            for c in comps:
                print("\t".join([c["key"], "1" if c["required"] else "0", c["label"],
                                 " ".join(c["packages"])]))
        else:
            pkgs = _flat(comps)
            sep = {"lines": "\n", "words": " ", "deb": ", "}[args.format]
            if pkgs:
                print(sep.join(pkgs))
        return 0
    if args.cmd == "list":
        layout = _layout_from(args)
        files = layout.files(args.installer_copy)
        if args.json:
            print(json.dumps({"files": files, "owned_dirs": layout.owned_dirs()}, indent=2))
        else:
            print("\n".join(files))
        return 0
    if args.cmd == "install":
        layout = _layout_from(args)
        extra = None
        if args.manifest_data:
            with open(args.manifest_data, encoding="utf-8") as f:
                extra = json.load(f)
        res = install(layout, installer_copy=args.installer_copy, manifest=args.manifest,
                      manifest_data=extra)
        if not args.quiet:
            print("\n".join(res["files"]))
        for f in res.get("removed_stale", []):
            print(f"removed obsolete file {f}", file=sys.stderr)
        return 0
    if args.cmd == "uninstall":
        if args.manifest:
            data = read_manifest(Path(args.manifest))
            if data is None:
                raise LayoutError(f"{args.manifest} does not exist.")
            # Removal is confined to Rehome's locations under the manifest's own
            # prefix (Layout.owns), whatever paths the file lists.
            layout = _layout_from(args, prefix=data.get("prefix"),
                                  libexecdir=data.get("libexecdir"))
            removed = uninstall(layout, data)
        else:
            layout = _layout_from(args)
            removed = uninstall(layout)
        for f in removed:
            print(f)
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
