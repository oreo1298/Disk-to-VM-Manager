"""Quick Create and `rehome create --unattended` end to end, offline.

A local HTTP server plays the download mirror (serving a stand-in Ubuntu
Server ISO); the real VM creation code (host detection, profiles, qcow2
creation) runs, then the unattended preparation.
"""

import hashlib
import http.server
import json
import threading

import pytest

from rehome.media import catalog, download, unattended
from rehome.vm import library
from tests.fixtures import fake_installers

pytestmark = pytest.mark.skipif(fake_installers.available() is not None,
                                reason=fake_installers.available() or "")


@pytest.fixture
def mirror(tmp_path):
    iso = fake_installers.build("ubuntu24.04-server", tmp_path / "ubuntu-24.04.3-live-server-amd64.iso")
    data = iso.read_bytes()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.server.hits += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/x-iso9660-image")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", '"1"')
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.hits = 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/ubuntu-24.04.3-live-server-amd64.iso"
    srv.sha = hashlib.sha256(data).hexdigest()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_quick_create_downloads_creates_and_prepares(mirror, monkeypatch):
    entry = catalog.get("ubuntu-server-lts")

    def fake_resolve(e, fetch=None):
        assert e.id == entry.id
        return catalog.Resolved(entry=entry, version="24.04.3", series="24.04", url=mirror.url,
                                filename="ubuntu-24.04.3-live-server-amd64.iso",
                                checksum=download.Checksum("sha256", mirror.sha, "test"),
                                checksum_url="https://releases.ubuntu.com/24.04/SHA256SUMS")

    monkeypatch.setattr(catalog, "resolve", fake_resolve)
    progress, status = [], []
    res = catalog.quick_create(
        "ubuntu-server-lts", answers=unattended.Answers(user="alice", password="pw-123",
                                                        locale="en_US", keyboard="us",
                                                        timezone="UTC"),
        progress=lambda *a: progress.append(a), status=status.append)
    cfg = library.reload(res.config)
    assert cfg.name == "Ubuntu Server LTS 24.04.3"
    assert cfg.os.osinfo_id.endswith("/24.04") and cfg.machine.firmware == "uefi"
    assert res.iso.read_bytes()[:0] == b"" and res.iso.name.endswith(".iso")
    assert progress[-1][0] == progress[-1][1]
    assert any("Downloading" in s for s in status) and any("automatic" in s for s in status)
    assert cfg.direct_boot and cfg.direct_boot.append.startswith("autoinstall")
    assert any(cd.file == unattended.ANSWER_ISO for cd in cfg.cdroms)
    assert (cfg.dir / "disks/disk0.qcow2").exists()
    assert any("turns off" in n for n in res.notes)
    # Second quick create re-uses the verified download.
    hits = mirror.hits
    res2 = catalog.quick_create("ubuntu-server-lts")
    assert mirror.hits == hits and res2.iso == res.iso
    assert res2.config.name == "Ubuntu Server LTS 24.04.3 (2)"


def test_failed_preparation_removes_the_half_made_vm(mirror, monkeypatch):
    entry = catalog.get("ubuntu-server-lts")
    monkeypatch.setattr(catalog, "resolve", lambda e, fetch=None: catalog.Resolved(
        entry=entry, version="24.04.3", series="24.04", url=mirror.url,
        filename="u.iso", checksum=download.Checksum("sha256", mirror.sha)))

    def boom(cfg, answers):
        raise unattended.RehomeError("nope", code="unattended-unsupported")
    monkeypatch.setattr(unattended, "prepare", boom)
    with pytest.raises(unattended.RehomeError):
        catalog.quick_create("ubuntu-server-lts", answers=unattended.Answers(password="x"))
    assert library.list_vms() == []


def test_cli_create_unattended_windows(tmp_path, capsys):
    from rehome.cli.main import main
    iso = fake_installers.build("win11", tmp_path / "Win11_24H2_English_x64.iso")
    pw = tmp_path / "pw"
    pw.write_text("Secret-1\n")
    rc = main(["create", "--iso", str(iso), "--unattended", "--user", "bob",
               "--password-file", str(pw), "--timezone", "America/New_York", "--locale", "en_US",
               "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    cfg = library.find(out["id"])
    assert cfg.machine.firmware == "uefi-secure" and cfg.machine.tpm == "2.0"
    xml = (cfg.dir / "unattended/media/autounattend.xml").read_text()
    assert "<Name>bob</Name>" in xml and "Secret-1" in xml and "Eastern Standard Time" in xml
    assert any("Press any key" in n for n in out["notes"])
    assert unattended.read_state(cfg)["press_boot_key"]
    # After the installation: remove the answer files (they hold the password).
    assert main(["unattended-cleanup", cfg.short_id]) == 0
    cfg = library.reload(cfg)
    assert not (cfg.dir / "unattended").exists()
    assert all(cd.file != unattended.ANSWER_ISO for cd in cfg.cdroms)


def test_cli_rejects_unsupported_unattended_without_leaving_a_vm(tmp_path, capsys):
    from rehome.cli.main import main
    iso = fake_installers.build("fedora42-workstation", tmp_path / "ws.iso")
    rc = main(["create", "--iso", str(iso), "--unattended", "--password", "x"])
    assert rc == 1
    assert "cannot install automatically" in capsys.readouterr().err
    assert library.list_vms() == []


def test_cli_quick_create_list_and_external(capsys):
    from rehome.cli.main import main
    assert main(["quick-create", "--list", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert {e["id"] for e in entries} >= {"ubuntu-server-lts", "windows-11"}
    assert main(["quick-create", "windows-server-eval"]) == 0
    assert "https://www.microsoft.com/evalcenter/" in capsys.readouterr().out
