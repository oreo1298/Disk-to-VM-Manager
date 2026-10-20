"""Quick Create catalog: parsing and resolvers, offline.

Fixtures in tests/fixtures/catalog/:
* recorded on 2026-10-20 from the live sites: ubuntu-releases-index.html,
  ubuntu-26.04-SHA256SUMS, ubuntu-24.04-SHA256SUMS, sf-reactos-*.html
  (SourceForge pages trimmed to their file-list script);
* synthetic samples in the published formats (the hosts were blocked in the
  build environment, hashes are fake): everything else.

Set REHOME_NETWORK_TESTS=1 to also resolve the reachable entries live.
"""

import json
import os
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from rehome.errors import RehomeError
from rehome.media import catalog, download

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "catalog"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


def not_found(url):
    return RehomeError("not found", code="http-404", details=url)


class FakeWeb:
    """fetch() replacement: URL -> fixture text; anything else is a 404."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetched = []

    def __call__(self, url: str) -> str:
        self.fetched.append(url)
        if url in self.pages:
            return self.pages[url]
        raise not_found(url)


UBUNTU = {
    "https://releases.ubuntu.com/": fixture("ubuntu-releases-index.html"),
    "https://releases.ubuntu.com/26.04/SHA256SUMS": fixture("ubuntu-26.04-SHA256SUMS"),
    "https://releases.ubuntu.com/24.04/SHA256SUMS": fixture("ubuntu-24.04-SHA256SUMS"),
}


def entry(entry_id):
    return catalog.get(entry_id)


# --- the catalog file ----------------------------------------------------------------------

def test_catalog_file_is_valid_and_complete():
    entries = catalog.load()
    ids = [e.id for e in entries]
    assert len(ids) == len(set(ids))
    for want in ("ubuntu-desktop-lts", "ubuntu-server-lts", "fedora-workstation", "debian",
                 "opensuse-tumbleweed", "archlinux", "linuxmint", "freebsd", "freedos",
                 "reactos", "windows-11", "windows-10", "windows-server-eval"):
        assert want in ids
    for e in entries:
        assert e.name and e.description and e.homepage.startswith("https://") and e.license
        assert e.osinfo
        if e.external:
            host = urllib.parse.urlsplit(e.url).hostname
            assert host == "www.microsoft.com", e.id      # Microsoft's own pages only
            assert not e.resolver
        else:
            assert e.resolver["type"] in catalog.RESOLVERS
            for key in ("index",):
                if key in e.resolver:
                    assert e.resolver[key].startswith("https://"), e.id


def test_windows_is_never_downloaded():
    for e in catalog.load():
        if "windows" in e.id or e.osinfo[0].startswith("win"):
            assert e.kind == "external"
            with pytest.raises(RehomeError):
                catalog.resolve(e, fetch=lambda u: pytest.fail("must not fetch"))


def test_parse_rejects_bad_entries():
    good = {"id": "x", "name": "X", "osinfo": ["linux"], "kind": "direct",
            "resolver": {"type": "checksum-list"}}
    assert catalog.parse({"os": [good]})[0].id == "x"
    for bad in ({**good, "kind": "torrent"}, {**good, "resolver": {"type": "magic"}},
                {**good, "id": "Bad ID"}, {**good, "osinfo": []}, {**good, "colour": "red"},
                {**good, "kind": "external", "url": "http://example.com"}):
        with pytest.raises(RehomeError):
            catalog.parse({"os": [bad]})
    with pytest.raises(RehomeError):
        catalog.parse({"os": [good, good]})
    with pytest.raises(RehomeError):
        catalog.parse({"schema": 99, "os": []})


def test_unknown_entry():
    with pytest.raises(RehomeError) as ei:
        catalog.get("beos")
    assert "--list" in ei.value.fix


# --- helpers -----------------------------------------------------------------------------------

def test_version_helpers():
    assert sorted(["24.04", "24.04.5.1", "24.04.5", "22.04.5"], key=catalog.vkey)[-1] == "24.04.5.1"
    v = catalog.version_vars("13.1.0")
    assert v["major"] == "13" and v["major_minor"] == "13.1"
    assert catalog.fill(r"a-{version}-\d{4}", {"version": "1.2"}, regex=True) == r"a-1\.2-\d{4}"
    assert catalog.pick_file(["x-1.9.iso", "x-1.10.iso", "y.iso"], r"x-(\d+\.\d+)\.iso", {}) == \
        ("x-1.10.iso", "1.10")
    assert catalog.hrefs('<a href="22.2/">x</a> <a href="?C=N">s</a> <a href="https://a/b/c.iso">') \
        == ["22.2", "c.iso"]


def test_os_id_falls_back_to_known_entries():
    from rehome.media import osinfo
    if not osinfo.available():
        pytest.skip("libosinfo not available")
    assert catalog.pick_os_id(entry("ubuntu-server-lts"),
                              catalog.version_vars("26.04.1", "26.04")) == "ubuntu24.04"
    assert catalog.pick_os_id(entry("ubuntu-server-lts"),
                              catalog.version_vars("24.04.3", "24.04")) == "ubuntu24.04"
    assert catalog.pick_os_id(entry("debian"), catalog.version_vars("12.11.0")) == "debian12"
    assert catalog.pick_os_id(entry("freedos"), catalog.version_vars("1.4")) == "freedos1.3"
    assert catalog.pick_os_id(entry("reactos"), catalog.version_vars("0.4.16")) == "reactos0.4"
    assert catalog.pick_os_id(entry("linuxmint"), catalog.version_vars("22.2")) == "ubuntu24.04"


# --- resolvers (recorded data) --------------------------------------------------------------------

def test_ubuntu_lts_from_recorded_index():
    web = FakeWeb(UBUNTU)
    r = catalog.resolve(entry("ubuntu-desktop-lts"), fetch=web)
    assert (r.version, r.series) == ("26.04.1", "26.04")      # 26.10 is not an LTS
    assert r.url == "https://releases.ubuntu.com/26.04/ubuntu-26.04.1-desktop-amd64.iso"
    assert r.checksum.algo == "sha256"
    assert r.checksum.value == "601e30fbf5d97759367c632e2c33630665039b7e2158fd068403da3ccf1bda1f"
    assert r.checksum_url == "https://releases.ubuntu.com/26.04/SHA256SUMS"
    s = catalog.resolve(entry("ubuntu-server-lts"), fetch=web)
    assert s.filename == "ubuntu-26.04.1-live-server-amd64.iso"
    assert s.checksum.value == "cc8a95cde20f6ced61a322420de00f10cc3c90ced545daa46cb9c1a117f1d927"
    assert s.label == "Ubuntu Server LTS 26.04.1"


def test_ubuntu_falls_back_to_previous_lts_until_published():
    pages = dict(UBUNTU)
    del pages["https://releases.ubuntu.com/26.04/SHA256SUMS"]      # not out yet
    r = catalog.resolve(entry("ubuntu-desktop-lts"), fetch=FakeWeb(pages))
    assert r.version == "24.04.5.1"
    s = catalog.resolve(entry("ubuntu-server-lts"), fetch=FakeWeb(pages))
    assert s.version == "24.04.5"


def test_network_errors_are_not_mistaken_for_missing_files():
    def offline(url):
        raise RehomeError("The proxy refused", code="proxy-refused")
    with pytest.raises(RehomeError) as ei:
        catalog.resolve(entry("ubuntu-server-lts"), fetch=offline)
    assert ei.value.code == "proxy-refused"


def test_reactos_from_recorded_sourceforge_pages():
    web = FakeWeb({
        "https://sourceforge.net/projects/reactos/files/ReactOS/": fixture("sf-reactos-top.html"),
        "https://sourceforge.net/projects/reactos/files/ReactOS/0.4.16/":
            fixture("sf-reactos-0.4.16.html"),
    })
    r = catalog.resolve(entry("reactos"), fetch=web)
    assert r.version == "0.4.16" and r.filename == "ReactOS-0.4.16-i386.zip"   # not the RC
    assert r.url.endswith("/ReactOS/0.4.16/ReactOS-0.4.16-i386.zip/download")
    assert r.checksum.algo == "sha256"
    assert r.checksum.value == "e5851510cf5b79ef51a76b8155dd5ef7faca1155f44d0a1d41c35b77ece4ce2a"
    assert r.archive_member and "SourceForge" in r.verification


# --- resolvers (format samples) ---------------------------------------------------------------------

def test_debian_netinst():
    web = FakeWeb({"https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/SHA256SUMS":
                   fixture("debian-SHA256SUMS")})
    r = catalog.resolve(entry("debian"), fetch=web)
    assert r.filename == "debian-13.1.0-amd64-netinst.iso" and r.version == "13.1.0"
    assert r.url == "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/" + r.filename
    sums = download.parse_checksums(fixture("debian-SHA256SUMS"))
    assert r.checksum.value == sums[r.filename]


def test_archlinux():
    web = FakeWeb({"https://geo.mirror.pkgbuild.com/iso/latest/sha256sums.txt":
                   fixture("arch-sha256sums.txt")})
    r = catalog.resolve(entry("archlinux"), fetch=web)
    assert r.filename == "archlinux-2026.10.01-x86_64.iso" and r.version == "2026.10.01"
    assert r.url.startswith("https://geo.mirror.pkgbuild.com/iso/latest/")


def test_opensuse_tumbleweed_snapshot():
    web = FakeWeb({"https://download.opensuse.org/tumbleweed/iso/"
                   "openSUSE-Tumbleweed-DVD-x86_64-Current.iso.sha256":
                   fixture("opensuse-DVD-Current.iso.sha256")})
    r = catalog.resolve(entry("opensuse-tumbleweed"), fetch=web)
    assert r.filename == "openSUSE-Tumbleweed-DVD-x86_64-Snapshot20261017-Media.iso"
    assert r.version == "20261017" and r.checksum


def test_linuxmint_from_mirror_listing():
    web = FakeWeb({"https://mirrors.kernel.org/linuxmint/stable/": fixture("mint-stable.html"),
                   "https://mirrors.kernel.org/linuxmint/stable/22.2/sha256sum.txt":
                       fixture("mint-22.2-sha256sum.txt")})
    r = catalog.resolve(entry("linuxmint"), fetch=web)
    assert r.filename == "linuxmint-22.2-cinnamon-64bit.iso" and r.version == "22.2"
    assert "kernel.org" in r.verification


def test_freebsd_skips_release_without_published_images():
    base = "https://download.freebsd.org/releases/ISO-IMAGES/"
    web = FakeWeb({base: fixture("freebsd-iso-images.html"),
                   base + "14.3/CHECKSUM.SHA256-FreeBSD-14.3-RELEASE-amd64":
                       fixture("freebsd-14.3-CHECKSUM.SHA256")})
    r = catalog.resolve(entry("freebsd"), fetch=web)
    assert r.filename == "FreeBSD-14.3-RELEASE-amd64-disc1.iso"          # not .iso.xz
    assert r.url == base + "14.3/FreeBSD-14.3-RELEASE-amd64-disc1.iso"
    assert base + "15.0/CHECKSUM.SHA256-FreeBSD-15.0-RELEASE-amd64" in web.fetched
    assert r.checksum.value == download.parse_checksums(
        fixture("freebsd-14.3-CHECKSUM.SHA256"))[r.filename]


def test_freedos_without_checksum_file_says_so():
    base = "https://www.ibiblio.org/pub/micro/pc-stuff/freedos/files/distributions/"
    web = FakeWeb({base: fixture("freedos-distributions.html"),
                   base + "1.4/": fixture("freedos-1.4.html")})
    r = catalog.resolve(entry("freedos"), fetch=web)
    assert r.version == "1.4" and r.filename == "FD14-LiveCD.zip"
    assert r.checksum is None
    assert "only the HTTPS connection" in r.verification
    assert base + "1.4/verify.txt" in web.fetched                         # it did look
    assert r.archive_member


def test_freedos_uses_verify_txt_when_published():
    base = "https://www.ibiblio.org/pub/micro/pc-stuff/freedos/files/distributions/"
    digest = "e" * 64
    web = FakeWeb({base: fixture("freedos-distributions.html"),
                   base + "1.4/verify.txt": f"SHA-256 checksums\n{digest}  FD14-LiveCD.zip\n"})
    r = catalog.resolve(entry("freedos"), fetch=web)
    assert r.checksum.value == digest


def _fedora_web(json_text=None):
    rel = "https://dl.fedoraproject.org/pub/fedora/linux/releases/43/"
    return FakeWeb({
        "https://fedoraproject.org/releases.json": json_text or fixture("fedora-releases.json"),
        rel + "Server/x86_64/iso/": fixture("fedora-43-server-listing.html"),
        rel + "Server/x86_64/iso/Fedora-Server-43-1.1-x86_64-CHECKSUM":
            fixture("Fedora-Server-43-1.1-x86_64-CHECKSUM"),
        rel + "Workstation/x86_64/iso/": fixture("fedora-43-workstation-listing.html"),
        rel + "Workstation/x86_64/iso/Fedora-Workstation-43-1.1-x86_64-CHECKSUM":
            fixture("Fedora-Workstation-43-1.1-x86_64-CHECKSUM"),
    })


def test_fedora_releases_json_and_checksum_file():
    web = _fedora_web()
    s = catalog.resolve(entry("fedora-server"), fetch=web)
    assert s.version == "43"                                     # not "44 Beta", not aarch64
    assert s.filename == "Fedora-Server-dvd-x86_64-43-1.1.iso"
    assert s.url.startswith("https://download.fedoraproject.org/")
    assert s.checksum_url.startswith("https://dl.fedoraproject.org/")
    assert s.checksum.value == download.parse_checksums(
        fixture("Fedora-Server-43-1.1-x86_64-CHECKSUM"))[s.filename]
    w = catalog.resolve(entry("fedora-workstation"), fetch=web)
    assert w.filename == "Fedora-Workstation-Live-43-1.1.x86_64.iso"


def test_fedora_conflicting_checksums_are_refused():
    rel = json.loads(fixture("fedora-releases.json"))
    for r in rel:
        r["sha256"] = "f" * 64
    with pytest.raises(RehomeError) as ei:
        catalog.resolve(entry("fedora-server"), fetch=_fedora_web(json.dumps(rel)))
    assert ei.value.code == "checksum-conflict"


def test_missing_checksum_is_an_error_not_a_silent_download():
    web = FakeWeb({"https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/SHA256SUMS":
                   "0" * 64 + "  something-else.iso\n"})
    with pytest.raises(RehomeError) as ei:
        catalog.resolve(entry("debian"), fetch=web)
    assert ei.value.code == "no-release"


# --- quick create / archives ----------------------------------------------------------------------

def test_quick_create_external_returns_microsoft_url():
    res = catalog.quick_create("windows-11", fetch=lambda u: pytest.fail("no network"))
    assert res.external_url == "https://www.microsoft.com/software-download/windows11"
    assert res.config is None and res.iso is None
    assert any("microsoft.com" in n for n in res.notes)


def test_quick_create_refuses_unattended_for_unsupported_entries():
    from rehome.media.unattended import Answers
    with pytest.raises(RehomeError) as ei:
        catalog.quick_create("archlinux", answers=Answers(password="x"),
                             fetch=lambda u: pytest.fail("must fail before any download"))
    assert ei.value.code == "unattended-unsupported"


def test_extract_member(tmp_path):
    z = tmp_path / "ReactOS-0.4.16-i386.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ReactOS-0.4.16-i386-live.iso", b"live")
        zf.writestr("sub/ReactOS-0.4.16-i386.iso", b"boot" * 1000)
    out = catalog.extract_member(z, r"(?i)(?!.*live).*\.iso")
    assert out == tmp_path / "ReactOS-0.4.16-i386.iso" and out.read_bytes() == b"boot" * 1000
    assert catalog.extract_member(z, r"(?i)(?!.*live).*\.iso") == out      # re-used
    with pytest.raises(RehomeError):
        catalog.extract_member(z, r"nothing\.iso")


def test_extract_member_detects_corruption(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("x.iso", b"A" * 5000)
    data = bytearray(z.read_bytes())
    i = data.find(b"A" * 100)
    data[i + 10] = ord("B")                     # flip a byte inside the member
    z.write_bytes(bytes(data))
    with pytest.raises(RehomeError) as ei:
        catalog.extract_member(z, r"x\.iso")
    assert ei.value.code == "bad-archive"
    assert not z.exists() and not (tmp_path / "x.iso").exists()


# --- optional live checks -------------------------------------------------------------------------

live = pytest.mark.skipif(os.environ.get("REHOME_NETWORK_TESTS") != "1",
                          reason="set REHOME_NETWORK_TESTS=1 to query the real download sites")


@live
@pytest.mark.parametrize("entry_id", ["ubuntu-desktop-lts", "ubuntu-server-lts", "reactos"])
def test_live_resolve(entry_id):
    r = catalog.resolve(entry_id)
    assert r.url.startswith("https://") and r.checksum and r.version
