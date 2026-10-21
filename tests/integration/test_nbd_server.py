"""Serving a read-only fd over NBD: Rehome's fallback server and nbdkit."""

import hashlib
import os
import shutil
import subprocess

import pytest

from rehome.disk import nbd
from rehome.util.proc import which
from tests.fixtures import freedos

pytestmark = pytest.mark.slow

if freedos.available():
    pytest.skip(f"cannot build FreeDOS: {freedos.available()}", allow_module_level=True)

BACKENDS = ["builtin"] + (["nbdkit"] if which("nbdkit") else [])


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return freedos.build(tmp_path_factory.mktemp("nbd") / "fd.img")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(params=BACKENDS)
def served(request, image, monkeypatch):
    if request.param == "builtin":
        monkeypatch.setenv("REHOME_NO_NBDKIT", "1")
    fd = os.open(image, os.O_RDONLY)
    sock = nbd.socket_for(str(image) + request.param)
    srv = nbd.NbdServer(fd, sock).start()
    yield nbd.uri(sock)
    srv.stop()
    os.close(fd)


def test_full_copy_matches_source(served, image, tmp_path):
    before = _sha(image)
    out = tmp_path / "copy.raw"
    subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "raw", served, str(out)], check=True)
    assert _sha(out) == before == _sha(image)
    info = subprocess.run(["qemu-img", "info", "--output=json", served], check=True,
                          capture_output=True, text=True).stdout
    assert f'"virtual-size": {os.path.getsize(image)}' in info


def test_writes_are_refused(served, image):
    before = _sha(image)
    cp = subprocess.run(["qemu-io", "-f", "raw", "-c", "write -P 0x55 0 4096", served],
                        capture_output=True, text=True)
    assert cp.returncode != 0 or "error" in (cp.stdout + cp.stderr).lower()
    assert _sha(image) == before


@pytest.mark.skipif(not shutil.which("guestfish") and not os.path.exists("/usr/bin/guestfish"),
                    reason="libguestfs not installed")
def test_libguestfs_reads_it(served):
    guestfs = pytest.importorskip("guestfs")
    sock = served.split("socket=", 1)[1]
    g = guestfs.GuestFS(python_return_dict=True)
    g.set_backend("direct")
    g.add_drive_opts("", format="raw", readonly=True, protocol="nbd",
                     server=[f"unix:{sock}"])
    g.launch()
    try:
        fss = g.list_filesystems()
        assert "vfat" in fss.values()
    finally:
        g.close()
