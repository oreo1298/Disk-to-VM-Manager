"""Import real installed systems and boot them to a login or desktop.

Needs your own disk images (they are never modified). Put them in a folder,
describe them in ``images.toml`` there and run:

    REHOME_TEST_MEDIA=~/rehome-test-images python3 -m pytest -m boot tests/boot -v

See docs/TESTING.md for the file format and the list of useful images.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from tests.boot import watch

MEDIA = Path(os.environ.get("REHOME_TEST_MEDIA", "")).expanduser()
MANIFEST = MEDIA / "images.toml"

pytestmark = [pytest.mark.boot, pytest.mark.slow]


def _entries():
    if not os.environ.get("REHOME_TEST_MEDIA") or not MANIFEST.is_file():
        return []
    data = tomllib.loads(MANIFEST.read_text())
    return [pytest.param(e, id=e.get("id") or Path(e["files"][0] if "files" in e else e["file"]).stem)
            for e in data.get("image", [])]


ENTRIES = _entries()
if not ENTRIES:
    pytest.skip("set REHOME_TEST_MEDIA to a folder with images.toml (see docs/TESTING.md)",
                allow_module_level=True)


@pytest.fixture(scope="session")
def artifacts_root():
    root = Path(os.environ.get("REHOME_TEST_ARTIFACTS", MEDIA / "results")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def real_home(monkeypatch, artifacts_root):
    """Keep test VMs out of the user's library but on the same disk as the images."""
    home = artifacts_root / "rehome-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("REHOME_HOME", str(home))
    return home


@pytest.mark.parametrize("entry", ENTRIES)
def test_import_and_boot(entry, real_home, artifacts_root):
    from rehome import importer
    from rehome.disk import hashing
    from rehome.vm import library, runner

    files = [str((MEDIA / f).resolve()) for f in entry.get("files", [entry.get("file")])]
    name = entry.get("id") or Path(files[0]).stem
    out = artifacts_root / name
    hashes = {f: hashing.sha256_path(f) for f in files} if entry.get("hash", True) else {}

    a = importer.analyze(files, keys=entry.get("keys", []), smart_check=False)
    o = a.os
    assert o is not None, f"nothing was identified: {a.notes} {a.blockers}"
    (out / "analysis.txt").parent.mkdir(parents=True, exist_ok=True)
    (out / "analysis.txt").write_text(f"{o.name} family={o.family} version={o.version} "
                                      f"build={o.build} boot={o.boot_mode} bus={o.original_bus}\n"
                                      + "\n".join(a.notes + a.blockers + a.questions))
    if "family" in entry:
        assert o.family == entry["family"], f"identified as {o.family} ({o.name})"
    if "version" in entry:
        assert str(o.version).startswith(str(entry["version"])), f"identified as {o.name} {o.version}"

    opts = importer.ImportOptions(name=f"boot-test {name}", mode=entry.get("mode", "linked"),
                                  discard_hibernation=entry.get("discard_hibernation", True),
                                  keys=entry.get("keys", []), overrides=entry.get("overrides", {}),
                                  smart_check=False)
    cfg = importer.run(a, opts)
    try:
        runner.start(cfg, headless=True)
        v = watch.watch(cfg, o.family, float(entry.get("timeout", 900)), out / "screens",
                        success=entry.get("success_text"))
        (out / "verdict.txt").write_text(f"{v.state}: {v.reason}\n\n{v.evidence}")
        assert v.state == "success", (f"{v.reason}; screenshots in {out / 'screens'}\n"
                                      f"{v.evidence[-800:]}")
    finally:
        if runner.state(cfg).active:
            runner.force_off(cfg)
        if not entry.get("keep_vm", False):
            library.delete(library.reload(cfg))
    for f, before in hashes.items():
        assert hashing.sha256_path(f) == before, f"{f} changed!"
