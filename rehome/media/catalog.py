"""Quick Create: a catalog of free operating systems, downloaded and verified.

The catalog lives in ``data/catalog.toml``. Each entry names a *resolver*
type plus parameters; a resolver discovers the current release from the
project's machine-readable index (a directory listing, a checksum list,
``releases.json`` or SourceForge's file list) and returns the download URL
together with the official checksum. Adding an OS is a data change, not a
code change, as long as it publishes its images in one of these ways.

Typical use (GUI and CLI)::

    entries = catalog.load()
    result = catalog.quick_create("ubuntu-server-lts", progress=cb, cancel=is_cancelled,
                                  answers=unattended.Answers(user="me", password="..."),
                                  start=True)
    if result.external_url:      # Windows: show/open Microsoft's page
        ...
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rehome import paths
from rehome.errors import Cancelled, RehomeError
from rehome.media import download
from rehome.util import toml

log = logging.getLogger(__name__)

SCHEMA = 1
KINDS = ("direct", "external")
RESOLVERS: dict[str, Callable] = {}

FetchFn = Callable[[str], str]
StatusFn = Callable[[str], None]


@dataclass
class CatalogEntry:
    id: str
    name: str
    description: str = ""
    osinfo: list[str] = field(default_factory=list)
    kind: str = "direct"
    resolver: dict = field(default_factory=dict)
    url: str = ""                  # external entries: the page to open
    homepage: str = ""
    license: str = ""
    unattended: bool = False
    unattended_note: str = ""
    size: str = ""
    verification: str = ""         # overrides the generated verification text
    notes: list[str] = field(default_factory=list)

    @property
    def external(self) -> bool:
        return self.kind == "external"


@dataclass
class Resolved:
    """The concrete image a catalog entry currently points to."""
    entry: CatalogEntry
    version: str
    url: str
    filename: str
    checksum: download.Checksum | None
    checksum_url: str = ""
    series: str = ""
    archive_member: str = ""       # regex of the ISO inside a downloaded archive
    size: int = 0

    @property
    def label(self) -> str:
        return f"{self.entry.name} {self.version}".strip()

    @property
    def verification(self) -> str:
        if self.entry.verification:
            return self.entry.verification
        if self.checksum:
            return f"{self.checksum.algo.upper()} from {self.checksum_url or self.checksum.source}"
        return "Only the HTTPS connection and the file size are checked (no published checksum)."

    def vars(self) -> dict[str, str]:
        return version_vars(self.version, self.series)

    def osinfo_id(self) -> str:
        return pick_os_id(self.entry, self.vars())

    def to_dict(self) -> dict:
        return {"id": self.entry.id, "name": self.entry.name, "version": self.version,
                "label": self.label, "url": self.url, "filename": self.filename,
                "checksum": ({"algo": self.checksum.algo, "value": self.checksum.value}
                             if self.checksum else None),
                "checksum_url": self.checksum_url, "verification": self.verification,
                "archive_member": self.archive_member or None}


@dataclass
class QuickCreateResult:
    entry: CatalogEntry
    resolved: Resolved | None = None
    iso: Path | None = None
    config: object | None = None          # VMConfig
    external_url: str = ""                # set for "external" entries: open this page
    started: bool = False
    notes: list[str] = field(default_factory=list)


# --- loading -------------------------------------------------------------------------

def catalog_path() -> Path:
    return paths.data_files_dir() / "catalog.toml"


def load(path: str | os.PathLike | None = None) -> list[CatalogEntry]:
    p = Path(path) if path else catalog_path()
    try:
        data = toml.load(p)
    except FileNotFoundError as e:
        raise RehomeError("The Quick Create catalog is missing.",
                          fix="Reinstall Rehome.", details=str(p)) from e
    except ValueError as e:
        raise RehomeError("The Quick Create catalog could not be read.",
                          details=f"{p}: {e}") from e
    return parse(data, str(p))


def parse(data: dict, origin: str = "catalog") -> list[CatalogEntry]:
    if data.get("schema", SCHEMA) > SCHEMA:
        raise RehomeError("The Quick Create catalog is newer than this Rehome.",
                          fix="Update Rehome.", details=origin)
    out: list[CatalogEntry] = []
    seen: set[str] = set()
    names = {f for f in CatalogEntry.__dataclass_fields__}
    for raw in data.get("os", []):
        unknown = set(raw) - names
        if unknown:
            raise RehomeError(f"Unknown field(s) {sorted(unknown)} in catalog entry "
                              f"{raw.get('id', '?')}.", details=origin)
        e = CatalogEntry(**raw)
        problems = []
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", e.id or ""):
            problems.append("invalid id")
        if e.id in seen:
            problems.append("duplicate id")
        if e.kind not in KINDS:
            problems.append(f"kind must be one of {KINDS}")
        if e.kind == "external" and not e.url.startswith("https://"):
            problems.append("external entries need an https url")
        if e.kind == "direct" and e.resolver.get("type") not in RESOLVERS:
            problems.append(f"unknown resolver type {e.resolver.get('type')!r}")
        if not e.osinfo:
            problems.append("osinfo list is empty")
        if problems:
            raise RehomeError(f"Catalog entry \"{e.id}\" is invalid: {', '.join(problems)}.",
                              details=origin)
        seen.add(e.id)
        out.append(e)
    return out


def get(entry_id: str, entries: list[CatalogEntry] | None = None) -> CatalogEntry:
    entries = entries if entries is not None else load()
    for e in entries:
        if e.id == entry_id:
            return e
    raise RehomeError(f"There is no Quick Create entry called \"{entry_id}\".",
                      fix="Run `rehome quick-create --list` to see the available systems.",
                      code="not-found")


# --- helpers ----------------------------------------------------------------------------

def vkey(v: str) -> tuple:
    """Sort key for versions: '24.04.5.1' > '24.04.5' > '24.04'."""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def version_vars(version: str, series: str = "") -> dict[str, str]:
    nums = re.findall(r"\d+", version or "")
    return {"version": version or "", "series": series or version or "",
            "major": nums[0] if nums else "",
            "major_minor": ".".join(nums[:2]) if len(nums) >= 2 else (nums[0] if nums else "")}


def fill(template: str, values: dict[str, str], regex: bool = False) -> str:
    """Replace ``{name}`` for known names only (regex quantifiers like {4} survive)."""
    def sub(m):
        key = m.group(1)
        if key not in values:
            return m.group(0)
        return re.escape(values[key]) if regex else values[key]
    return re.sub(r"\{([a-z_]+)\}", sub, template)


def _regexes(spec, values: dict[str, str]) -> list[re.Pattern]:
    specs = spec if isinstance(spec, list) else [spec]
    return [re.compile(fill(s, values, regex=True)) for s in specs]


def pick_file(names, spec, values: dict[str, str]) -> tuple[str, str] | None:
    """Pick the newest file name matching ``spec``: returns (name, version)."""
    for rx in _regexes(spec, values):
        hits = []
        for n in names:
            m = rx.fullmatch(n)
            if m:
                ver = m.group(1) if m.groups() and m.group(1) else ""
                hits.append((vkey(ver), n, ver))
        if hits:
            hits.sort(reverse=True)
            return hits[0][1], hits[0][2]
    return None


def hrefs(html: str) -> list[str]:
    """Link targets of a directory listing (file or directory names, decoded)."""
    out = []
    for h in re.findall(r'href\s*=\s*["\']([^"\'?#]+)["\']', html, re.I):
        h = urllib.parse.unquote(h).rstrip("/").rsplit("/", 1)[-1]
        if h and h not in (".", ".."):
            out.append(h)
    return out


def digest_for(text: str, name: str) -> str | None:
    """Find ``name``'s digest in a checksum file (strict formats first, then lenient)."""
    d = download.parse_checksums(text).get(name)
    if d:
        return d
    for line in text.splitlines():
        if name in line:
            m = re.search(r"\b([0-9a-fA-F]{64}|[0-9a-fA-F]{128})\b", line)
            if m:
                return m.group(1).lower()
    return None


def pick_os_id(entry: CatalogEntry, values: dict[str, str]) -> str:
    """First candidate known to libosinfo (or a Rehome profile ID)."""
    from rehome.media import osinfo
    from rehome.patch import profiles
    have_osinfo = osinfo.available()
    candidates = [fill(c, values) for c in entry.osinfo]
    for c in candidates:
        if "{" in c or c.endswith(("-", ".")) or not c:
            continue
        if have_osinfo and (osinfo.by_short_id(c) or c in osinfo.KNOWN_EXTRA):
            return c
        try:
            profiles.get(c)
            return c
        except KeyError:
            continue
    return candidates[-1]


# --- resolvers -------------------------------------------------------------------------

def resolver(name: str):
    def deco(fn):
        RESOLVERS[name] = fn
        return fn
    return deco


def _no_release(entry: CatalogEntry, details: str) -> RehomeError:
    return RehomeError(
        f"Rehome could not find the current {entry.name} release.",
        fix="The project's download site may be changing or unreachable right now. Try again "
            "later; if it keeps failing, update Rehome (the site layout may have changed).",
        details=details, code="no-release")


@resolver("checksum-list")
def _resolve_checksum_list(entry: CatalogEntry, r: dict, fetch: FetchFn) -> Resolved:
    index = r["index"].rstrip("/") + "/"
    versions: list[str | None] = [None]
    if r.get("version_dir"):
        html = fetch(index)
        found = sorted(set(re.findall(r["version_dir"], html)), key=vkey, reverse=True)
        if not found:
            raise _no_release(entry, f"no version directories matching {r['version_dir']!r} "
                                     f"in {index}")
        versions = found[: int(r.get("max_tries", 3))]
    tried = []
    for v in versions:
        base = index + (f"{v}/" if v else "")
        for sub in r.get("subdirs", [""]):
            d = base + sub
            values = version_vars(v or "", v or "")
            names: list[str] = []
            text = ""
            checksum_url = ""
            optional = bool(r.get("checksum_optional"))
            if r.get("checksum_file"):
                checksum_url = d + fill(r["checksum_file"], values)
                try:
                    text = fetch(checksum_url)
                except RehomeError as e:
                    # A missing file means "try the next version/directory";
                    # anything else (offline, proxy refusal) is reported as is.
                    if e.code != "http-404":
                        raise
                    tried.append(f"{checksum_url}: {e.message}")
                    text, checksum_url = "", ""
                    if not optional:
                        continue
                if text:
                    names = list(download.parse_checksums(text)) or re.findall(r"[\w.+-]+", text)
            if not names and (optional or not r.get("checksum_file")):
                try:
                    names = hrefs(fetch(d))
                except RehomeError as e:
                    if e.code != "http-404":
                        raise
                    tried.append(f"{d}: {e.message}")
                    continue
            hit = pick_file(names, r["file"], values)
            if not hit:
                tried.append(f"{checksum_url or d}: no file matching {r['file']!r}")
                continue
            name, file_version = hit
            checksum = None
            if text:
                digest = digest_for(text, name)
                if not digest:
                    raise _no_release(entry, f"{name} has no checksum in {checksum_url}")
                checksum = download.Checksum(download.algo_for(digest), digest, checksum_url)
            elif not r.get("checksum_optional"):
                raise _no_release(entry, f"no checksum for {name}")
            version = file_version or v or ""
            return Resolved(entry=entry, version=version, series=v or version,
                            url=d + urllib.parse.quote(name), filename=name, checksum=checksum,
                            checksum_url=checksum_url,
                            archive_member=r.get("archive_member", ""))
    raise _no_release(entry, "\n".join(tried) or index)


@resolver("fedora-releases")
def _resolve_fedora(entry: CatalogEntry, r: dict, fetch: FetchFn) -> Resolved:
    try:
        releases = json.loads(fetch(r["index"]))
    except ValueError as e:
        raise _no_release(entry, f"{r['index']}: {e}") from e
    arch = r.get("arch", "x86_64")
    rx = re.compile(r["link"])
    cands = []
    for rel in releases if isinstance(releases, list) else []:
        ver = str(rel.get("version", ""))
        link = rel.get("link", "")
        if not ver.isdigit() or rel.get("arch") != arch or rel.get("variant") != r["variant"]:
            continue
        if r.get("subvariant") and rel.get("subvariant") != r["subvariant"]:
            continue
        if not link.startswith("https://") or not rx.fullmatch(link.rsplit("/", 1)[-1]):
            continue
        cands.append((int(ver), rel))
    if not cands:
        raise _no_release(entry, f"no {r['variant']} image for {arch} in {r['index']}")
    cands.sort(key=lambda c: c[0], reverse=True)
    ver, rel = cands[0]
    link = rel["link"]
    name = link.rsplit("/", 1)[-1]
    dir_url = link.rsplit("/", 1)[0] + "/"
    if r.get("checksum_host"):
        u = urllib.parse.urlsplit(dir_url)
        dir_url = r["checksum_host"].rstrip("/") + u.path
    listing = hrefs(fetch(dir_url))
    crx = re.compile(r.get("checksum", r".*-CHECKSUM"))
    digest = None
    checksum_url = ""
    for cand in sorted(n for n in set(listing) if crx.fullmatch(n)):
        checksum_url = dir_url + cand
        digest = digest_for(fetch(checksum_url), name)
        if digest:
            break
    if not digest:
        raise _no_release(entry, f"no CHECKSUM file listing {name} in {dir_url}")
    published = (rel.get("sha256") or "").lower()
    if published and published != digest:
        raise RehomeError(
            f"The checksums Fedora publishes for {name} disagree.",
            fix="This can happen briefly while a release is being updated. Try again later.",
            details=f"releases.json: {published}\n{checksum_url}: {digest}",
            code="checksum-conflict")
    size = int(rel["size"]) if str(rel.get("size", "")).isdigit() else 0
    return Resolved(entry=entry, version=str(ver), series=str(ver), url=link, filename=name,
                    checksum=download.Checksum(download.algo_for(digest), digest, checksum_url),
                    checksum_url=checksum_url, size=size)


def _sf_files(project: str, path: str, fetch: FetchFn) -> tuple[dict, str]:
    url = f"https://sourceforge.net/projects/{project}/files/{urllib.parse.quote(path)}/"
    html = fetch(url)
    m = re.search(r"net\.sf\.files\s*=\s*", html)
    if not m:
        raise RehomeError("SourceForge's file list could not be read.", details=url,
                          code="no-release")
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError as e:
        raise RehomeError("SourceForge's file list could not be read.",
                          details=f"{url}: {e}", code="no-release") from e
    return data, url


@resolver("sourceforge")
def _resolve_sourceforge(entry: CatalogEntry, r: dict, fetch: FetchFn) -> Resolved:
    project, path = r["project"], r["path"].strip("/")
    try:
        top, top_url = _sf_files(project, path, fetch)
    except RehomeError as e:
        raise _no_release(entry, e.details or e.message) from e
    vrx = re.compile(r["version_dir"])
    versions = sorted((k for k, v in top.items() if v.get("type") == "d" and vrx.fullmatch(k)),
                      key=vkey, reverse=True)
    tried = []
    for v in versions[: int(r.get("max_tries", 3))]:
        files, url = _sf_files(project, f"{path}/{v}", fetch)
        names = [k for k, info in files.items() if info.get("type") == "f"]
        hit = pick_file(names, r["file"], version_vars(v, v))
        if not hit:
            tried.append(f"{url}: no file matching {r['file']!r}")
            continue
        name = hit[0]
        info = files[name]
        checksum = None
        for algo in ("sha256", "sha1", "md5"):
            if info.get(algo):
                checksum = download.Checksum(algo, info[algo], f"SourceForge ({url})")
                break
        if checksum is None:
            raise _no_release(entry, f"SourceForge lists no checksum for {name}")
        dl = info.get("download_url") or f"https://sourceforge.net/projects/{project}/files/" \
                                          f"{path}/{v}/{name}/download"
        return Resolved(entry=entry, version=v, series=v, url=dl, filename=name,
                        checksum=checksum, checksum_url=url,
                        archive_member=r.get("archive_member", ""))
    raise _no_release(entry, "\n".join(tried) or top_url)


def resolve(entry: CatalogEntry | str, fetch: FetchFn | None = None) -> Resolved:
    """Find the current image for ``entry`` (network access unless ``fetch`` is given)."""
    if isinstance(entry, str):
        entry = get(entry)
    if entry.external:
        raise RehomeError(f"{entry.name} is not downloaded by Rehome.",
                          fix=f"Download it from {entry.url}, then create the VM from the ISO.")
    fn = RESOLVERS[entry.resolver["type"]]
    return fn(entry, entry.resolver, fetch or download.fetch_text)


# --- downloading -------------------------------------------------------------------------

def download_image(res: Resolved, progress=None, cancel=None,
                   status: StatusFn | None = None) -> Path:
    """Download (or re-use) the verified image; returns the ISO path."""
    say = status or (lambda s: None)
    if download.verified_path(res.filename, res.checksum):
        say(f"Using the already downloaded and verified {res.filename}")
    else:
        say(f"Downloading {res.filename}")
    path = download.download(res.url, filename=res.filename, checksum=res.checksum,
                             expected_size=res.size or None, progress=progress, cancel=cancel)
    if res.archive_member:
        say(f"Unpacking {res.filename}")
        path = extract_member(path, res.archive_member, cancel=cancel)
    return path


def extract_member(archive: Path, member_regex: str, cancel=None) -> Path:
    """Extract the ISO from a downloaded ZIP (CRC-checked by zipfile) next to it."""
    rx = re.compile(member_regex)
    try:
        with zipfile.ZipFile(archive) as z:
            infos = [i for i in z.infolist()
                     if not i.is_dir() and rx.fullmatch(i.filename.rsplit("/", 1)[-1])]
            if not infos:
                raise RehomeError(f"{archive.name} does not contain the expected disk image.",
                                  details=f"members: {z.namelist()}", code="bad-archive")
            info = infos[0]
            out = archive.parent / download._safe_name(info.filename.rsplit("/", 1)[-1])
            marker = out.with_name(out.name + ".extracted.json")
            try:
                m = json.loads(marker.read_text())
                if out.stat().st_size == info.file_size and m.get("crc") == info.CRC and \
                        m.get("archive") == archive.name:
                    return out
            except (OSError, ValueError):
                pass
            part = out.with_name(out.name + ".part")
            try:
                # ZipExtFile checks the member's CRC-32 when it reaches the end.
                with z.open(info) as src, open(part, "wb") as dst:
                    while chunk := src.read(download.CHUNK):
                        if cancel and cancel():
                            raise Cancelled()
                        dst.write(chunk)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            os.replace(part, out)
            marker.write_text(json.dumps({"archive": archive.name, "crc": info.CRC}))
            return out
    except zipfile.BadZipFile as e:
        download.remove(archive)
        raise RehomeError(f"{archive.name} is damaged (the archive check failed). Rehome "
                          "deleted it.", fix="Try again.", details=str(e),
                          code="bad-archive") from e


# --- quick create ---------------------------------------------------------------------------

def quick_create(entry_id: str, name: str | None = None, start: bool = False, answers=None,
                 progress=None, cancel=None, *, status: StatusFn | None = None,
                 fetch: FetchFn | None = None, spec_overrides: dict | None = None,
                 start_options: dict | None = None) -> QuickCreateResult:
    """Download (or re-use) an OS from the catalog and create a VM for it.

    * ``answers`` (:class:`rehome.media.unattended.Answers`): install unattended.
    * ``progress(bytes_done, bytes_total, speed, eta)``, ``cancel() -> bool``.
    * ``status(text)`` receives short phase descriptions for the UI.
    * Blocking; the GUI runs it in a worker thread (the callbacks are called
      from that thread) and cancels through ``cancel``.
    * ``spec_overrides``: NewVMSpec fields such as memory_mib, cpus, disk_size.
    * ``start_options``: keyword arguments for ``runner.start`` (e.g. headless).
    * For "external" entries (Windows) nothing is downloaded: the result's
      ``external_url`` is the official page to open.
    """
    entry = get(entry_id)
    say = status or (lambda s: None)
    if entry.external:
        notes = [f"Rehome does not download {entry.name}. Get the ISO from Microsoft:",
                 entry.url,
                 "Then create the VM from it, for example: rehome create --iso PATH-TO.iso"
                 + (" --unattended --user NAME" if entry.unattended else "")]
        return QuickCreateResult(entry=entry, external_url=entry.url, notes=notes)
    if answers is not None:
        if not entry.unattended:
            raise RehomeError(f"{entry.name} cannot be installed automatically.",
                              fix=entry.unattended_note or
                              "Create the VM without --unattended and install it by hand.",
                              code="unattended-unsupported")
        from rehome.media import unattended
        unattended.validate(answers)          # fail before a long download
    say(f"Looking up the current {entry.name} release")
    res = resolve(entry, fetch)
    iso = download_image(res, progress=progress, cancel=cancel, status=say)
    if cancel and cancel():
        raise Cancelled()
    say("Creating the virtual machine")
    from rehome.vm import create as create_mod
    from rehome.vm import library
    os_id = res.osinfo_id()
    spec = create_mod.NewVMSpec(name=name or res.label, iso=str(iso), os=os_id)
    for k, v in (spec_overrides or {}).items():
        if not hasattr(spec, k):
            raise ValueError(f"unknown VM setting {k}")
        setattr(spec, k, v)
    cfg = create_mod.create(spec)
    notes = list(entry.notes)
    exact = fill(entry.osinfo[0], res.vars())
    if os_id != exact:
        # osinfo-db on this computer does not know this release yet: keep the
        # hardware defaults of the closest known one but show the real name.
        cfg.os.name = res.label
        library.save(cfg)
        notes.append(f"This computer's osinfo-db does not know {res.label} yet; Rehome used "
                     f"the settings for {os_id}.")
    if not res.checksum:
        notes.append(f"Verification: {res.verification}")
    result = QuickCreateResult(entry=entry, resolved=res, iso=iso, config=cfg, notes=notes)
    if answers is not None:
        from rehome.media import unattended
        say("Preparing the automatic installation")
        try:
            notes += unattended.prepare(cfg, answers)
        except BaseException:
            library.delete(cfg)
            raise
    if start:
        from rehome.vm import runner
        say("Starting the virtual machine")
        runner.start(cfg, **(start_options or {}))
        result.started = True
        if answers is not None:
            from rehome.media import unattended
            notes += unattended.after_start(cfg, cancel=cancel, status=say)
    return result
