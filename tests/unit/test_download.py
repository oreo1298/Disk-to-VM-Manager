"""rehome.media.download against a local HTTP server (no internet needed).

The server supports Range/If-Range like real mirrors (Python's http.server
does not), and can misbehave on request: drop the connection, ignore ranges,
send HTML, redirect to plain HTTP.
"""

import hashlib
import http.server
import json
import os
import threading
import time
from pathlib import Path

import pytest

from rehome.errors import Cancelled, RehomeError
from rehome.media import download

DATA = os.urandom(3 * 2**20 + 12345)          # a little over 3 chunks
SHA = hashlib.sha256(DATA).hexdigest()
ETAG = '"v1-abc"'


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TestMirror/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append({"path": self.path, "range": self.headers.get("Range"),
                             "if-range": self.headers.get("If-Range"),
                             "ua": self.headers.get("User-Agent")})
        path = self.path.split("?")[0]
        if path == "/redirect-insecure":
            self.send_response(302)
            self.send_header("Location", "http://example.com/file.iso")
            self.end_headers()
            return
        if path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", "/file.iso")
            self.end_headers()
            return
        if path == "/page.iso":
            body = b"<html>Please log in</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/SHA256SUMS":
            body = f"{SHA}  file.iso\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in ("/file.iso", "/drop.iso", "/norange.iso"):
            self.send_error(404)
            return
        data = srv.data
        start = 0
        rng = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        honour = rng and path != "/norange.iso" and (if_range in (None, srv.etag))
        if honour:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data) - start))
        self.send_header("ETag", srv.etag)
        self.send_header("Last-Modified", "Mon, 05 Oct 2026 10:00:00 GMT")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        body = data[start:]
        if path == "/drop.iso" and srv.drops > 0:
            srv.drops -= 1
            self.wfile.write(body[: len(body) // 3])
            self.wfile.flush()
            self.connection.shutdown(2)       # simulate a dropped connection
            return
        step = 256 * 1024
        for i in range(0, len(body), step):
            try:
                self.wfile.write(body[i:i + step])
            except (BrokenPipeError, ConnectionResetError):
                return
            if srv.delay:
                time.sleep(srv.delay)


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.data = DATA
    srv.etag = ETAG
    srv.requests = []
    srv.drops = 0
    srv.delay = 0.0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "dl"
    d.mkdir()
    return d


def sha(algo=SHA):
    return download.Checksum("sha256", algo, "test")


def test_downloads_verifies_and_reuses(server, dest):
    calls = []
    p = download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha(),
                          progress=lambda *a: calls.append(a))
    assert p == dest / "file.iso" and p.read_bytes() == DATA
    assert not (dest / "file.iso.part").exists()
    marker = json.loads((dest / "file.iso.verified.json").read_text())
    assert marker["sha256"] == SHA and marker["size"] == len(DATA)
    # progress: (bytes_done, bytes_total, speed, eta); ends at the full size
    assert calls[-1][0] == calls[-1][1] == len(DATA)
    assert all(len(c) == 4 for c in calls)
    assert server.requests[0]["ua"].startswith("Rehome/")
    # Second call: verified file is re-used without touching the network.
    n = len(server.requests)
    assert download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha()) == p
    assert len(server.requests) == n
    assert download.verified_path("file.iso", sha(), dest) == p


def test_default_destination_is_downloads_dir(server):
    from rehome import paths
    p = download.download(server.url + "/file.iso", checksum=sha())
    assert p.parent == paths.downloads_dir()


def test_resume_uses_range(server, dest):
    part = dest / "file.iso.part"
    part.write_bytes(DATA[:1_000_000])
    (dest / "file.iso.part.json").write_text(json.dumps(
        {"url": server.url + "/file.iso", "etag": ETAG, "last_modified": "", "total": len(DATA)}))
    p = download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha())
    assert p.read_bytes() == DATA
    req = server.requests[-1]
    assert req["range"] == "bytes=1000000-" and req["if-range"] == ETAG


def test_resume_restarts_when_the_file_changed(server, dest):
    part = dest / "file.iso.part"
    part.write_bytes(b"x" * 500_000)               # bytes of an older release
    (dest / "file.iso.part.json").write_text(json.dumps(
        {"url": server.url + "/file.iso", "etag": '"old"', "total": len(DATA)}))
    p = download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha())
    assert p.read_bytes() == DATA                  # server ignored the range -> fresh 200


def test_server_without_range_support(server, dest):
    (dest / "norange.iso.part").write_bytes(DATA[:700_000])
    (dest / "norange.iso.part.json").write_text(json.dumps(
        {"url": server.url + "/norange.iso", "etag": ETAG, "total": len(DATA)}))
    p = download.download(server.url + "/norange.iso", dest_dir=dest, checksum=sha())
    assert p.read_bytes() == DATA


def test_checksum_mismatch_deletes_everything(server, dest):
    bad = "0" * 64
    with pytest.raises(RehomeError) as ei:
        download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha(bad))
    e = ei.value
    assert e.code == "checksum-mismatch"
    assert "deleted" in e.message and e.fix
    assert bad in e.details and SHA in e.details
    assert sorted(os.listdir(dest)) == []            # no final file, no .part, no marker


def test_existing_file_with_old_checksum_is_replaced(server, dest):
    (dest / "file.iso").write_bytes(b"older release")
    p = download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha())
    assert p.read_bytes() == DATA


def test_cancel_keeps_partial_download_for_resume(server, dest):
    server.delay = 0.01
    checks = []

    def cancel():
        # 1st check: before connecting; 2nd: before the first chunk; then cancel.
        checks.append(1)
        return len(checks) > 2

    with pytest.raises(Cancelled):
        download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha(),
                          cancel=cancel)
    assert not (dest / "file.iso").exists()
    part = dest / "file.iso.part"
    assert part.exists() and 0 < part.stat().st_size < len(DATA)
    have = part.stat().st_size
    server.delay = 0
    p = download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha())
    assert p.read_bytes() == DATA
    assert server.requests[-1]["range"] == f"bytes={have}-"


def test_cancel_before_start(server, dest):
    with pytest.raises(Cancelled):
        download.download(server.url + "/file.iso", dest_dir=dest, cancel=lambda: True)
    assert not (dest / "file.iso").exists()


def test_dropped_connection_resumes_automatically(server, dest, monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    server.drops = 1
    p = download.download(server.url + "/drop.iso", dest_dir=dest, checksum=sha())
    assert p.read_bytes() == DATA
    ranges = [r["range"] for r in server.requests if r["path"] == "/drop.iso"]
    assert ranges[0] is None and ranges[1] and ranges[1].startswith("bytes=")


def test_html_instead_of_file(server, dest):
    with pytest.raises(RehomeError) as ei:
        download.download(server.url + "/page.iso", dest_dir=dest)
    assert ei.value.code == "html-instead-of-file"


def test_http_404(server, dest):
    with pytest.raises(RehomeError) as ei:
        download.download(server.url + "/gone.iso", dest_dir=dest)
    assert ei.value.code == "http-404" and ei.value.fix


def test_only_https(dest):
    with pytest.raises(RehomeError) as ei:
        download.download("http://example.com/x.iso", dest_dir=dest)
    assert ei.value.code == "insecure-url"
    with pytest.raises(RehomeError):
        download.fetch_text("ftp://example.com/SHA256SUMS")


def test_redirect_may_not_downgrade(server, dest):
    with pytest.raises(RehomeError) as ei:
        download.download(server.url + "/redirect-insecure", filename="x.iso", dest_dir=dest)
    assert ei.value.code == "insecure-url"
    p = download.download(server.url + "/redirect-ok", filename="file.iso", dest_dir=dest,
                          checksum=sha())
    assert p.read_bytes() == DATA


def test_unreachable_host_explains_how_to_fix(dest):
    with pytest.raises(RehomeError) as ei:
        download.fetch_text("http://127.0.0.1:9/nothing", timeout=2)
    assert ei.value.code in ("network", "timeout") and "internet" in ei.value.fix


def test_not_enough_space(server, dest, monkeypatch):
    class Usage:
        free = 1000
    monkeypatch.setattr(download.shutil, "disk_usage", lambda p: Usage)
    with pytest.raises(RehomeError) as ei:
        download.download(server.url + "/file.iso", dest_dir=dest, checksum=sha())
    assert ei.value.code == "no-space"


def test_expected_size_mismatch(server, dest):
    with pytest.raises(RehomeError) as ei:
        download.download(server.url + "/file.iso", dest_dir=dest, expected_size=10)
    assert ei.value.code == "size-mismatch"


def test_fetch_text(server):
    assert download.fetch_text(server.url + "/SHA256SUMS") == f"{SHA}  file.iso\n"


def test_parse_checksums_formats():
    a, b = "a" * 64, "b" * 64
    text = f"""-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA256

# Fedora-Server-dvd-x86_64-43-1.1.iso: 2612854784 bytes
SHA256 (Fedora-Server-dvd-x86_64-43-1.1.iso) = {a}
{b} *ubuntu-26.04.1-desktop-amd64.iso
{"C" * 64}  ./sub/debian-13.1.0-amd64-netinst.iso
{"d" * 128}  big.iso
-----BEGIN PGP SIGNATURE-----
iQIzBAEBCAAd
-----END PGP SIGNATURE-----
"""
    sums = download.parse_checksums(text)
    assert sums == {"Fedora-Server-dvd-x86_64-43-1.1.iso": a,
                    "ubuntu-26.04.1-desktop-amd64.iso": b,
                    "debian-13.1.0-amd64-netinst.iso": "c" * 64,
                    "big.iso": "d" * 128}
    assert download.algo_for("d" * 128) == "sha512"


def test_checksum_validation():
    with pytest.raises(ValueError):
        download.Checksum("sha256", "xyz")
    with pytest.raises(ValueError):
        download.Checksum("crc32", "0" * 8)
    assert download.Checksum("SHA256", "A" * 64).value == "a" * 64


def test_filename_from_url():
    assert download.filename_from_url(
        "https://sourceforge.net/projects/reactos/files/ReactOS/0.4.16/ReactOS-0.4.16-i386.zip/download"
    ) == "ReactOS-0.4.16-i386.zip"
    assert download.filename_from_url("https://x/a%20b.iso") == "a b.iso"
    assert download.filename_from_url("https://x/../..") not in ("..", ".")


def test_file_hash_and_remove(dest):
    f = dest / "f.iso"
    f.write_bytes(DATA)
    assert download.file_hash(f) == SHA
    (dest / "f.iso.verified.json").write_text("{}")
    download.remove(f)
    assert list(dest.iterdir()) == []
