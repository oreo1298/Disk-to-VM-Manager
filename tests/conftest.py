import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own Rehome library, cache and runtime dir."""
    home = tmp_path / "rehome-home"
    home.mkdir()
    monkeypatch.setenv("REHOME_HOME", str(home))
    # Socket paths must stay short (108-byte limit), so use a short /tmp dir.
    import shutil
    import tempfile
    run = tempfile.mkdtemp(prefix="rh", dir="/tmp")
    monkeypatch.setenv("REHOME_RUNTIME_DIR", run)
    monkeypatch.setenv("REHOME_DATA_DIR", str(ROOT / "data"))
    yield home
    shutil.rmtree(run, ignore_errors=True)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: needs QEMU/libguestfs and takes a while")
    config.addinivalue_line("markers", "boot: boots a real guest OS (needs images)")
