import pytest

from rehome.util import toml
from rehome.util.units import human, mib, parse_size


def test_parse_size():
    assert parse_size("4G") == 4 * 1024**3
    assert parse_size("512 MiB") == 512 * 1024**2
    assert parse_size("1.5TB") == int(1.5 * 1000**4)
    assert mib("2048") == 2048
    assert mib("2G") == 2048
    with pytest.raises(ValueError):
        parse_size("lots")


def test_human():
    assert human(1536 * 1024**2) == "1.5 GiB"
    assert human(10) == "10 B"


def test_toml_roundtrip(tmp_path):
    data = {
        "schema": 1, "name": 'My "VM"\nline2', "flag": True, "ratio": 0.5,
        "tags": ["a", "b"],
        "machine": {"type": "q35", "cpus": {"sockets": 1, "cores": 2}},
        "disks": [
            {"id": "d0", "bus": "sata", "source": {"kind": "image", "path": "/x y/a.vmdk"}},
            {"id": "d1", "bus": "virtio", "empty_list": []},
        ],
        "skipped": None,
    }
    path = tmp_path / "vm.toml"
    toml.dump_atomic(data, path)
    back = toml.load(path)
    expected = dict(data)
    expected.pop("skipped")
    assert back == expected
