import os
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
    monkeypatch.setenv("REHOME_DATA_DIR", str(ROOT / "data"))
    yield home


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: needs QEMU/libguestfs and takes a while")
    config.addinivalue_line("markers", "boot: boots a real guest OS (needs images)")
