"""Opt-in update check: version comparison, settings and the (mocked) request."""

import io
import json
import urllib.error

import pytest

from rehome import __version__, update
from rehome.util import toml


@pytest.mark.parametrize("a,b,expected", [
    ("0.1.0", "0.1.1", -1),
    ("0.1.1", "0.1.0", 1),
    ("v0.2.0", "0.1.9", 1),
    ("1.0", "1.0.0", 0),
    ("v1.2.3", "1.2.3", 0),
    ("1.10.0", "1.9.0", 1),
    ("1.0.0rc1", "1.0.0", -1),
    ("1.0.0-beta.1", "1.0.0", -1),
    ("1.0.0rc1", "0.9.9", 1),
    ("2", "1.99.99", 1),
])
def test_compare_versions(a, b, expected):
    assert update.compare_versions(a, b) == expected


def test_is_newer_uses_running_version():
    assert not update.is_newer(__version__)
    assert update.is_newer("999.0.0")


def test_opt_in_defaults_to_off():
    assert not update.settings_path().exists()
    assert update.is_enabled() is False


def test_enable_disable_persists_and_keeps_other_settings():
    path = update.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('theme = "dark"\n')
    update.set_enabled(True)
    assert update.is_enabled() is True
    assert toml.load(path) == {"theme": "dark", "update_check": True}
    update.set_enabled(False)
    assert update.is_enabled() is False
    assert toml.load(path)["theme"] == "dark"


def test_broken_settings_file_means_off():
    path = update.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("update_check = [not toml")
    assert update.is_enabled() is False
    # A non-boolean value is not consent either.
    path.write_text('update_check = "yes"\n')
    assert update.is_enabled() is False


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.requests = payload, exc, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.exc:
            raise self.exc
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return FakeResponse(body)


def test_check_does_nothing_unless_opted_in():
    opener = FakeOpener(exc=AssertionError("must not touch the network"))
    res = update.check(opener=opener)
    assert res.checked is False and res.latest is None and res.error is None
    assert opener.requests == []


def test_check_reports_newer_release_with_anonymous_request():
    update.set_enabled(True)
    opener = FakeOpener({"tag_name": "v999.1.0", "html_url": "https://example.invalid/r"})
    res = update.check(opener=opener)
    assert res.checked and res.available
    assert res.latest == "999.1.0"
    assert res.url == "https://example.invalid/r"
    (req,) = opener.requests
    assert req.full_url == update.RELEASES_API
    assert req.get_method() == "GET" and req.data is None
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers == {"user-agent": f"rehome/{__version__}",
                       "accept": "application/vnd.github+json"}


def test_check_up_to_date():
    opener = FakeOpener({"tag_name": f"v{__version__}"})
    res = update.check(force=True, opener=opener)
    assert res.checked and not res.available and res.latest == __version__


@pytest.mark.parametrize("exc,needle", [
    (urllib.error.URLError("no route"), "Could not reach"),
    (TimeoutError("timed out"), "Could not reach"),
    (urllib.error.HTTPError(update.RELEASES_API, 404, "Not Found", {}, None), "No release"),
    (urllib.error.HTTPError(update.RELEASES_API, 403, "Forbidden", {}, None), "HTTP 403"),
])
def test_network_errors_are_reported_not_raised(exc, needle):
    res = update.check(force=True, opener=FakeOpener(exc=exc))
    assert res.checked and not res.available
    assert needle in res.error


def test_bad_payload_is_reported():
    assert update.check(force=True, opener=FakeOpener(b"<html>")).error
    assert "no version" in update.check(force=True, opener=FakeOpener({"name": "x"})).error


def test_maybe_check_respects_opt_in_and_interval():
    opener = FakeOpener({"tag_name": "v999.0.0"})
    assert update.maybe_check(opener=opener) is None          # off
    update.set_enabled(True)
    first = update.maybe_check(now=1_000_000.0, opener=opener)
    assert first is not None and first.available
    # The stamp is written with the real clock; a check "now" is within the interval.
    assert update.maybe_check(opener=opener) is None
    assert len(opener.requests) == 1


def test_install_method_install_sh(tmp_path, monkeypatch):
    data = tmp_path / "share" / "rehome"
    (data / "installer").mkdir(parents=True)
    (data / "install-manifest.json").write_text('{"app": "rehome"}')
    (data / "installer" / "install.sh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("REHOME_DATA_DIR", str(data))
    monkeypatch.setattr(update.paths, "source_root", lambda: tmp_path / "lib")
    m = update.install_method()
    assert m.kind == "install.sh"
    assert any("--update" in c and "curl" in c for c in m.update_commands)
    assert f"{data}/installer/install.sh --update" in m.update_commands


def test_install_method_package_and_git(tmp_path, monkeypatch):
    monkeypatch.setattr(update.paths, "source_root", lambda: tmp_path)
    monkeypatch.setattr(update, "find_manifest", lambda: None)
    m = update.install_method()
    assert m.kind == "package" and m.update_commands
    (tmp_path / ".git").mkdir()
    m = update.install_method()
    assert m.kind == "git" and m.update_commands == (f"git -C {tmp_path} pull",)


def test_cli_update_check_enable_disable(capsys):
    from rehome.cli.main import main
    assert main(["update-check", "--enable"]) == 0
    assert update.is_enabled()
    assert main(["update-check", "--disable"]) == 0
    assert not update.is_enabled()
    assert "off" in capsys.readouterr().out


def test_cli_update_check_reports_without_network(monkeypatch, capsys):
    from rehome.cli.main import main
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        FakeOpener(exc=urllib.error.URLError("offline")))
    assert main(["update-check"]) == 0
    out = capsys.readouterr().out
    assert "Could not check for updates" in out and "How to update" in out
