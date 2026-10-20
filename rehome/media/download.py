"""Verified HTTPS downloads (installer images and their checksum files).

* Streaming with :mod:`urllib`, so ``HTTPS_PROXY``/``NO_PROXY`` work as usual.
* Data goes to ``NAME.part`` first; the final name only ever holds a complete,
  verified file. An interrupted download resumes with an HTTP ``Range``
  request (guarded by ``If-Range`` so a changed file is never spliced).
* SHA-256 (and the algorithm of the expected checksum, if different) is
  computed while the data streams in, so verification needs no second pass.
* Progress: ``progress(bytes_done, bytes_total, speed, eta)`` with speed in
  bytes/s and eta in seconds (``bytes_total``/``eta`` are 0/None when unknown).
* Cancellation: ``cancel()`` returning True raises :class:`Cancelled`; the
  ``.part`` file is kept so the next attempt resumes.
* Only HTTPS is accepted (plain HTTP only for loopback test servers), and a
  redirect may never downgrade to HTTP.
"""

from __future__ import annotations

import email.utils
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rehome import __version__, paths
from rehome.errors import Cancelled, RehomeError

log = logging.getLogger(__name__)

CHUNK = 1 << 20
USER_AGENT = f"Rehome/{__version__} (+https://github.com/oreo1298/rehome) Python-urllib"
MAX_TEXT = 16 * 2**20            # largest index/checksum file we fetch into memory
SPACE_MARGIN = 256 * 2**20       # keep this much free on the downloads disk
RETRIES = 3                      # automatic resumes after a dropped connection

ProgressFn = Callable[[int, int, float, "float | None"], None]
CancelFn = Callable[[], bool]

HASH_ALGOS = {"sha256": 64, "sha512": 128, "sha1": 40, "md5": 32}


@dataclass
class Checksum:
    """An expected digest and where it came from (shown in error messages)."""
    algo: str
    value: str
    source: str = ""

    def __post_init__(self) -> None:
        self.algo = self.algo.lower()
        self.value = self.value.strip().lower()
        if self.algo not in HASH_ALGOS:
            raise ValueError(f"unsupported checksum algorithm {self.algo}")
        if not re.fullmatch(rf"[0-9a-f]{{{HASH_ALGOS[self.algo]}}}", self.value):
            raise ValueError(f"malformed {self.algo} checksum {self.value!r}")


# --- URL policy ----------------------------------------------------------------

def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> None:
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" or (u.scheme == "http" and _is_loopback(u.hostname)):
        return
    raise RehomeError(f"Refusing to download over an insecure connection: {url}",
                      fix="Rehome only downloads over HTTPS.", code="insecure-url")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        check_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            # Keep Range/If-Range across mirror redirects (urllib drops them).
            for h in ("Range", "If-Range"):
                if req.has_header(h):
                    new.add_unredirected_header(h, req.get_header(h))
        return new


def _opener() -> urllib.request.OpenerDirector:
    # build_opener() adds a ProxyHandler that honours https_proxy/no_proxy.
    return urllib.request.build_opener(_SafeRedirect())


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or url


def _network_error(url: str, e: BaseException) -> RehomeError:
    host = _host(url)
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    via = f" (through the proxy {proxy})" if proxy else ""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return RehomeError(
                f"The file is not available on {host} (HTTP 404).",
                fix="The project may have just published a new version and removed the old one. "
                    "Try again in a few minutes; if it keeps failing, update Rehome.",
                details=f"GET {url}\nHTTP {e.code} {e.reason}", code="http-404")
        if e.code in (401, 403, 407):
            return RehomeError(
                f"{host} refused the download (HTTP {e.code}){via}.",
                fix="If you use a proxy or firewall, make sure it allows connections to "
                    f"{host}. Otherwise try again later.",
                details=f"GET {url}\nHTTP {e.code} {e.reason}", code=f"http-{e.code}")
        return RehomeError(
            f"{host} answered with an error (HTTP {e.code} {e.reason}).",
            fix="This is usually temporary. Try again later.",
            details=f"GET {url}\nHTTP {e.code} {e.reason}", code=f"http-{e.code}")
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(reason, ssl.SSLError):
        return RehomeError(
            f"The secure connection to {host} could not be verified.",
            fix="Check that this computer's date and time are correct. If your network "
                "inspects HTTPS traffic, point SSL_CERT_FILE at its certificate bundle.",
            details=f"GET {url}\n{reason}", code="tls")
    if isinstance(reason, socket.gaierror):
        return RehomeError(
            f"Could not find the server {host}.",
            fix="Check that this computer is connected to the internet (and the proxy "
                "settings in HTTPS_PROXY, if you use one).",
            details=f"GET {url}\n{reason}", code="dns")
    m = re.search(r"Tunnel connection failed: (\d+)", str(reason))
    if m:
        return RehomeError(
            f"The proxy {proxy or ''} refused the connection to {host} "
            f"(HTTP {m.group(1)}).".replace("  ", " "),
            fix=f"Ask your network administrator to allow {host}, or use another network.",
            details=f"GET {url}\n{reason}", code="proxy-refused")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return RehomeError(
            f"The connection to {host} timed out{via}.",
            fix="Check your internet connection and try again; the download resumes where "
                "it stopped.", details=f"GET {url}\n{reason}", code="timeout")
    return RehomeError(
        f"Could not connect to {host}{via}.",
        fix="Check that this computer is connected to the internet and try again (an "
            "interrupted download resumes where it stopped).",
        details=f"GET {url}\n{type(reason).__name__}: {reason}", code="network")


def _request(url: str, headers: dict | None = None, timeout: float = 30):
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        return _opener().open(req, timeout=timeout)
    except RehomeError:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return e          # handled by the caller (resume past the end)
        raise _network_error(url, e) from e
    except (urllib.error.URLError, OSError) as e:
        raise _network_error(url, e) from e


# --- small files -----------------------------------------------------------------

def fetch(url: str, *, timeout: float = 30, max_bytes: int = MAX_TEXT) -> bytes:
    """Fetch a small file (an index page, a checksum list) into memory."""
    resp = _request(url, timeout=timeout)
    with resp:
        if getattr(resp, "status", 200) >= 400:
            raise _network_error(url, urllib.error.HTTPError(url, resp.status, "error",
                                                             resp.headers, None))
        try:
            data = resp.read(max_bytes + 1)
        except (OSError, urllib.error.URLError) as e:
            raise _network_error(url, e) from e
    if len(data) > max_bytes:
        raise RehomeError(f"{url} is unexpectedly large.", code="too-large")
    return data


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).decode("utf-8", errors="replace")


# --- checksum files -----------------------------------------------------------------

_GNU_LINE = re.compile(r"^\\?([0-9a-fA-F]{32,128})\s+[ *]?(.+?)\s*$")
_BSD_LINE = re.compile(r"^(SHA256|SHA512|SHA1|MD5)\s*\((.+)\)\s*=\s*([0-9a-fA-F]{32,128})\s*$",
                       re.I)


def parse_checksums(text: str) -> dict[str, str]:
    """Parse ``sha256sum`` (GNU) and ``SHA256 (file) = …`` (BSD) style lists.

    Returns ``{file name: lower-case hex digest}``; PGP armour and comments are
    ignored, as are paths before the file name (``./x.iso`` → ``x.iso``).
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-----", "Hash:", "Version:", "Comment:")):
            continue
        m = _BSD_LINE.match(line)
        if m:
            name, digest = m.group(2), m.group(3)
        else:
            m = _GNU_LINE.match(line)
            if not m:
                continue
            digest, name = m.group(1), m.group(2)
        name = name.strip().lstrip("*")
        out[name.rsplit("/", 1)[-1]] = digest.lower()
    return out


def algo_for(digest: str) -> str:
    for algo, n in HASH_ALGOS.items():
        if len(digest) == n:
            return algo
    raise ValueError(f"unknown digest length {len(digest)}")


def file_hash(path: str | os.PathLike, algo: str = "sha256", progress: ProgressFn | None = None,
              cancel: CancelFn | None = None) -> str:
    h = hashlib.new(algo)
    total = os.path.getsize(path)
    done = 0
    rate = _Rate(total, progress)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            if cancel and cancel():
                raise Cancelled()
            h.update(chunk)
            done += len(chunk)
            rate.update(done)
    rate.update(done, final=True)
    return h.hexdigest()


# --- progress ----------------------------------------------------------------------------

class _Rate:
    """Throttled progress reporting with a smoothed speed estimate."""

    def __init__(self, total: int, cb: ProgressFn | None, start_at: int = 0):
        self.total = total
        self.cb = cb
        self.t0 = time.monotonic()
        self.last_t = 0.0
        self.last_bytes = start_at
        self.last_mark = (self.t0, start_at)
        self.speed = 0.0

    def update(self, done: int, final: bool = False) -> None:
        if not self.cb:
            return
        now = time.monotonic()
        if not final and now - self.last_t < 0.25:
            return
        t_prev, b_prev = self.last_mark
        if now - t_prev >= 0.5:
            inst = (done - b_prev) / (now - t_prev)
            self.speed = inst if self.speed == 0 else 0.7 * self.speed + 0.3 * inst
            self.last_mark = (now, done)
        eta = None
        if self.total and self.speed > 0:
            eta = max(0.0, (self.total - done) / self.speed)
        self.last_t = now
        self.cb(done, self.total, self.speed, eta)


# --- downloads ----------------------------------------------------------------------------

def filename_from_url(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1] == "download" and len(parts) > 1:   # SourceForge style
        parts.pop()
    name = parts[-1] if parts else "download"
    return _safe_name(name)


def _safe_name(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").strip()
    if name in ("", ".", "..") or name.startswith("."):
        name = "download" + name
    return name


def _sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _marker_ok(final: Path, checksum: Checksum | None) -> bool:
    m = _read_json(_sidecar(final, ".verified.json"))
    try:
        st = final.stat()
    except OSError:
        return False
    if m.get("size") != st.st_size or m.get("mtime_ns") != st.st_mtime_ns:
        return False
    if checksum is None:
        return True
    return m.get(checksum.algo) == checksum.value


def _write_marker(final: Path, digests: dict[str, str], url: str) -> None:
    st = final.stat()
    _write_json(_sidecar(final, ".verified.json"),
                {"url": url, "size": st.st_size, "mtime_ns": st.st_mtime_ns, **digests})


def verified_path(filename: str, checksum: Checksum | None,
                  dest_dir: str | os.PathLike | None = None) -> Path | None:
    """Return the downloaded file if it exists and was verified against ``checksum``."""
    final = Path(dest_dir or paths.downloads_dir()) / _safe_name(filename)
    return final if final.exists() and _marker_ok(final, checksum) else None


def _check_space(dest: Path, need: int) -> None:
    if need <= 0:
        return
    free = shutil.disk_usage(dest).free
    if free < need + SPACE_MARGIN:
        from rehome.util.units import human
        raise RehomeError(
            f"There is not enough free space for the download: {human(need)} needed, "
            f"{human(free)} free in {dest}.",
            fix="Free up some space (for example delete old downloads or VMs) and try again.",
            code="no-space")


def _content_range_start(value: str | None) -> int | None:
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value or "")
    return int(m.group(1)) if m else None


def _total_from(resp, offset: int) -> int:
    cr = resp.headers.get("Content-Range")
    if cr:
        m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
        if m:
            return int(m.group(1))
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        return int(cl) + offset
    return 0


def download(url: str, *, filename: str | None = None,
             dest_dir: str | os.PathLike | None = None,
             checksum: Checksum | None = None, expected_size: int | None = None,
             progress: ProgressFn | None = None, cancel: CancelFn | None = None,
             timeout: float = 30) -> Path:
    """Download ``url`` into the downloads directory and verify it.

    Returns the path of the complete, verified file. An existing verified file
    is reused without network access.
    """
    check_url(url)
    dest = paths.ensure(Path(dest_dir) if dest_dir else paths.downloads_dir())
    final = dest / _safe_name(filename or filename_from_url(url))
    part = _sidecar(final, ".part")
    meta_path = _sidecar(final, ".part.json")

    if final.exists():
        if _marker_ok(final, checksum):
            log.debug("re-using verified download %s", final)
            return final
        if checksum is not None:
            digest = file_hash(final, checksum.algo, progress, cancel)
            if digest == checksum.value:
                _write_marker(final, {checksum.algo: digest}, url)
                return final
            # An older release under the same name (or a damaged copy): make room
            # for the new one rather than keep a file that fails verification.
            log.info("%s does not match the current checksum; downloading again", final)
            final.unlink(missing_ok=True)
            _sidecar(final, ".verified.json").unlink(missing_ok=True)
        elif expected_size is None or final.stat().st_size == expected_size:
            # Completed downloads are only ever renamed into place, so a file
            # under the final name is complete.
            _write_marker(final, {}, url)
            return final

    attempt = 0
    while True:
        try:
            digests = _transfer(url, part, meta_path, checksum, expected_size, progress,
                                cancel, timeout, dest)
            break
        except RehomeError as e:
            retriable = e.code in ("network", "timeout", "short-read")
            attempt += 1
            if not retriable or attempt > RETRIES:
                raise
            log.info("download interrupted (%s); resuming (attempt %d)", e.code, attempt)
            time.sleep(min(2 ** attempt, 10))

    if checksum is not None and digests.get(checksum.algo) != checksum.value:
        got = digests.get(checksum.algo, "?")
        part.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise RehomeError(
            f"The downloaded file {final.name} is damaged: its {checksum.algo.upper()} "
            "checksum does not match the official one. Rehome deleted it.",
            fix="Try again. If it fails again, the download mirror may be broken or something "
                "on your network is modifying downloads; wait a while or use another network.",
            details=f"URL: {url}\nexpected {checksum.algo}: {checksum.value}"
                    f"{' (from ' + checksum.source + ')' if checksum.source else ''}\n"
                    f"actual   {checksum.algo}: {got}",
            code="checksum-mismatch")
    with open(part, "rb+") as f:
        os.fsync(f.fileno())
    os.replace(part, final)
    meta_path.unlink(missing_ok=True)
    _write_marker(final, digests, url)
    return final


def _transfer(url: str, part: Path, meta_path: Path, checksum: Checksum | None,
              expected_size: int | None, progress: ProgressFn | None, cancel: CancelFn | None,
              timeout: float, dest: Path) -> dict[str, str]:
    algos = {"sha256"} | ({checksum.algo} if checksum else set())
    meta = _read_json(meta_path)
    offset = part.stat().st_size if part.exists() else 0
    if offset and meta.get("url") != url:
        offset = 0                    # a different file was being downloaded
    headers = {}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        validator = meta.get("etag") if meta.get("etag", "").startswith('"') else \
            meta.get("last_modified")
        if validator:
            headers["If-Range"] = validator
        else:
            headers.pop("Range")      # cannot prove the file is unchanged: start over
            offset = 0
    if cancel and cancel():
        raise Cancelled()
    resp = _request(url, headers, timeout)
    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if status == 416:
            total = meta.get("total", 0)
            if offset and total and offset == total:
                # Already complete (we stopped before renaming).
                return _hash_existing(part, algos, progress, cancel)
            part.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise RehomeError("The server rejected the resume request.", code="network")
        if status == 206 and _content_range_start(resp.headers.get("Content-Range")) == offset:
            mode = "ab"
        else:
            offset, mode = 0, "wb"
        if status >= 400:
            raise _network_error(url, urllib.error.HTTPError(url, status, "error", resp.headers,
                                                             None))
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/html") and not url.endswith((".html", "/")):
            raise RehomeError(
                f"{_host(resp.geturl())} sent a web page instead of the file.",
                fix="Your network may require signing in (a captive portal), or the download "
                    "page changed. Open the URL in a browser to check.",
                details=f"GET {url}\n-> {resp.geturl()}\nContent-Type: {ctype}",
                code="html-instead-of-file")
        total = _total_from(resp, offset)
        if expected_size and total and total != expected_size:
            raise RehomeError(
                f"The server reports a different size for {part.name[:-5]} than expected.",
                fix="The project may have replaced the file. Try again later.",
                details=f"expected {expected_size} bytes, server says {total}",
                code="size-mismatch")
        total = total or (expected_size or 0)
        _check_space(dest, total - offset if total else 0)
        _write_json(meta_path, {"url": url, "etag": resp.headers.get("ETag", ""),
                                "last_modified": resp.headers.get("Last-Modified", ""),
                                "total": total, "started": email.utils.formatdate()})
        hashers = {a: hashlib.new(a) for a in algos}
        if mode == "ab":
            with open(part, "rb") as f:
                while chunk := f.read(CHUNK):
                    for h in hashers.values():
                        h.update(chunk)
        done = offset
        rate = _Rate(total, progress, start_at=offset)
        rate.update(done, final=True)
        with open(part, mode) as out:
            while True:
                if cancel and cancel():
                    raise Cancelled("The download was cancelled. It will resume where it "
                                    "stopped next time.")
                try:
                    chunk = resp.read(CHUNK)
                except (OSError, urllib.error.URLError) as e:
                    out.flush()
                    raise _network_error(url, e) from e
                if not chunk:
                    break
                out.write(chunk)
                for h in hashers.values():
                    h.update(chunk)
                done += len(chunk)
                rate.update(done)
        rate.update(done, final=True)
    if total and done < total:
        raise RehomeError(f"The connection closed before the download finished "
                          f"({done} of {total} bytes).", code="short-read")
    if total and done > total:
        part.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise RehomeError("The server sent more data than announced.", code="size-mismatch")
    return {a: h.hexdigest() for a, h in hashers.items()}


def _hash_existing(part: Path, algos: set[str], progress, cancel) -> dict[str, str]:
    hashers = {a: hashlib.new(a) for a in algos}
    total = part.stat().st_size
    rate = _Rate(total, progress)
    done = 0
    with open(part, "rb") as f:
        while chunk := f.read(CHUNK):
            if cancel and cancel():
                raise Cancelled()
            for h in hashers.values():
                h.update(chunk)
            done += len(chunk)
            rate.update(done)
    rate.update(done, final=True)
    return {a: h.hexdigest() for a, h in hashers.items()}


def remove(path: str | os.PathLike) -> None:
    """Delete a download and its bookkeeping files."""
    p = Path(path)
    for f in (p, _sidecar(p, ".part"), _sidecar(p, ".part.json"), _sidecar(p, ".verified.json")):
        f.unlink(missing_ok=True)
