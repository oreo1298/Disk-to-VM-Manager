"""Ubuntu downloads for the appliance: the base root filesystem, indexes and packages.

Everything is downloaded on the host (over HTTPS, through the user's proxy
settings) and cached under ``~/.cache/rehome/appliance``; apt then runs
*offline* inside the image being built, against a local copy of the archive
(see ``base.py``). This avoids depending on networking inside libguestfs's
helper VM, which varies between distributions.

Trust: the base tarball's SHA256 is pinned in ``data/appliance/base.toml``.
apt inside the image checks the signature of each ``InRelease`` with the
Ubuntu keyring from that base. The host checks every index against the
hashes listed in ``InRelease``, and every package against the hashes listed
in the indexes; apt checks the packages again when it installs them.
"""

from __future__ import annotations

import concurrent.futures
import lzma
import os
import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rehome import paths
from rehome.errors import RehomeError
from rehome.media import download
from rehome.util import toml

MIRROR_IN_IMAGE = "/var/tmp/rehome-mirror"


@dataclass
class Release:
    name: str
    codename: str
    url: str
    sha256: str
    archive: str
    suites: list[str]
    components: list[str]
    arch: str
    packages: dict[str, list[str]] = field(default_factory=dict)
    root_gib: int = 6
    esp_mib: int = 256

    @property
    def package_list(self) -> list[str]:
        return sorted({p for group in self.packages.values() for p in group})


def load_release(path: Path | None = None) -> Release:
    data = toml.load(path or paths.data_files_dir() / "appliance" / "base.toml")
    r, a = data["release"], data["archive"]
    return Release(name=r["name"], codename=r["codename"], url=r["url"], sha256=r["sha256"],
                   archive=a["url"].rstrip("/"), suites=list(a["suites"]),
                   components=list(a["components"]), arch=a["arch"],
                   packages={k: list(v) for k, v in data.get("packages", {}).items()},
                   root_gib=int(data.get("size", {}).get("root_gib", 6)),
                   esp_mib=int(data.get("size", {}).get("esp_mib", 256)))


def cache_dir() -> Path:
    """Downloads and the base system (``REHOME_APPLIANCE_CACHE`` overrides; tests share one)."""
    env = os.environ.get("REHOME_APPLIANCE_CACHE")
    return paths.ensure(Path(env) if env else paths.cache_dir() / "appliance")


def base_tarball(release: Release, progress=None, cancel=None) -> Path:
    return download.download(release.url, dest_dir=paths.ensure(cache_dir() / "base"),
                             checksum=download.Checksum("sha256", release.sha256,
                                                        "data/appliance/base.toml"),
                             progress=progress, cancel=cancel)


# --- indexes ---------------------------------------------------------------------------

def _release_hashes(text: str) -> dict[str, tuple[str, int]]:
    """``{path: (sha256, size)}`` from the SHA256 section of an (In)Release file."""
    out, in_sha = {}, False
    for line in text.splitlines():
        if not line.startswith(" "):
            in_sha = line.startswith("SHA256:")
            continue
        if in_sha:
            parts = line.split()
            if len(parts) == 3:
                out[parts[2]] = (parts[0].lower(), int(parts[1]))
    return out


@dataclass
class Mirror:
    """A local, partial copy of the archive (``dists/`` indexes plus ``pool/`` packages)."""
    root: Path
    files: list[str] = field(default_factory=list)      # paths relative to root

    def local(self, rel: str) -> Path:
        return self.root / rel


def fetch_indexes(release: Release, progress=None, cancel=None) -> Mirror:
    """Download the current ``InRelease`` and package indexes of every suite."""
    root = paths.ensure(cache_dir() / "mirror")
    mirror = Mirror(root)
    wanted = []
    for suite in release.suites:
        url = f"{release.archive}/dists/{suite}/InRelease"
        text = download.fetch_text(url)
        if "-----BEGIN PGP SIGNED MESSAGE-----" not in text:
            raise RehomeError(f"The Ubuntu archive sent an unsigned index for {suite}.",
                              details=url, code="network")
        rel = f"dists/{suite}/InRelease"
        paths.ensure(root / "dists" / suite)
        (root / rel).write_text(text)
        mirror.files.append(rel)
        hashes = _release_hashes(text)
        for comp in release.components:
            name = f"{comp}/binary-{release.arch}/Packages.xz"
            if name not in hashes:
                raise RehomeError(f"The Ubuntu archive has no {name} for {suite}.", code="network")
            wanted.append((suite, name, hashes[name]))
    total = sum(size for _s, _n, (_h, size) in wanted) or 1
    done = 0
    for suite, name, (sha, size) in wanted:
        rel = f"dists/{suite}/{name}"
        dest = paths.ensure((root / rel).parent)

        def prog(d, _t, _s, _e, base=done):
            if progress:
                progress((base + d) / total)
        download.download(f"{release.archive}/{rel}", filename="Packages.xz", dest_dir=dest,
                          checksum=download.Checksum("sha256", sha, f"{suite} InRelease"),
                          expected_size=size, progress=prog, cancel=cancel)
        mirror.files.append(rel)
        done += size
    return mirror


def package_hashes(mirror: Mirror, wanted: set[str]) -> dict[str, tuple[str, int]]:
    """``{Filename: (sha256, size)}`` from the indexes, for the pool paths in ``wanted``."""
    out: dict[str, tuple[str, int]] = {}
    for rel in mirror.files:
        if not rel.endswith("Packages.xz"):
            continue
        with lzma.open(mirror.local(rel), "rt", encoding="utf-8", errors="replace") as f:
            stanza: dict[str, str] = {}
            for line in f:
                if line.strip():
                    if not line.startswith((" ", "\t")) and ":" in line:
                        k, v = line.split(":", 1)
                        if k in ("Filename", "SHA256", "Size"):
                            stanza[k] = v.strip()
                    continue
                fn = stanza.get("Filename")
                if fn in wanted and "SHA256" in stanza:
                    out[fn] = (stanza["SHA256"].lower(), int(stanza.get("Size", "0")))
                stanza = {}
            fn = stanza.get("Filename")
            if fn in wanted and "SHA256" in stanza:
                out[fn] = (stanza["SHA256"].lower(), int(stanza.get("Size", "0")))
    return out


# --- packages --------------------------------------------------------------------------

_URI = re.compile(r"^'(?P<uri>[^']+)' (?P<file>\S+) (?P<size>\d+) ")


def parse_print_uris(text: str) -> list[str]:
    """Pool paths from ``apt-get --print-uris`` run against the local mirror."""
    out = []
    prefix = f"file:{MIRROR_IN_IMAGE}/"
    for line in text.splitlines():
        m = _URI.match(line)
        if not m:
            continue
        uri = m.group("uri")
        if not uri.startswith(prefix):
            raise RehomeError("apt wanted a package from outside the local mirror.", details=line)
        rel = urllib.parse.unquote(uri[len(prefix):])
        if not rel.startswith("pool/") or ".." in rel.split("/"):
            raise RehomeError("apt printed an unexpected package path.", details=line)
        out.append(rel)
    return out


def fetch_packages(release: Release, mirror: Mirror, pool_paths: list[str],
                   progress: Callable[[float], None] | None = None,
                   cancel: Callable[[], bool] | None = None, workers: int = 4) -> list[str]:
    """Download (or reuse) the packages and link them into the mirror's ``pool/``."""
    hashes = package_hashes(mirror, set(pool_paths))
    missing = sorted(set(pool_paths) - set(hashes))
    if missing:
        raise RehomeError("Some packages apt chose are not listed in the downloaded indexes.",
                          details="\n".join(missing[:20]))
    debs = paths.ensure(cache_dir() / "debs")
    total = sum(size for _h, size in hashes.values()) or 1
    lock = threading.Lock()
    done_by: dict[str, int] = {}

    def report():
        if progress:
            with lock:
                frac = sum(done_by.values()) / total
            progress(min(frac, 1.0))

    def one(rel: str) -> str:
        sha, size = hashes[rel]

        def prog(d, _t, _s, _e):
            with lock:
                done_by[rel] = d
            report()
        url = f"{release.archive}/{urllib.parse.quote(rel, safe='/~')}"
        path = download.download(url, filename=rel.rsplit("/", 1)[-1], dest_dir=debs,
                                 checksum=download.Checksum("sha256", sha, "Ubuntu package index"),
                                 expected_size=size, progress=prog, cancel=cancel)
        with lock:
            done_by[rel] = size
        report()
        link = mirror.local(rel)
        paths.ensure(link.parent)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(path)
        return rel

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, rel) for rel in sorted(set(pool_paths))]
        try:
            for f in concurrent.futures.as_completed(futures):
                f.result()
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    for rel in sorted(set(pool_paths)):
        if rel not in mirror.files:
            mirror.files.append(rel)
    return sorted(set(pool_paths))


def prune_debs(keep: set[str]) -> None:
    """Delete cached packages no longer used (``keep``: file names to keep)."""
    debs = cache_dir() / "debs"
    if not debs.is_dir():
        return
    for p in debs.iterdir():
        base = p.name.split(".deb", 1)[0] + ".deb" if ".deb" in p.name else p.name
        if base not in keep:
            p.unlink(missing_ok=True)
