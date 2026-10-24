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


def test_toml_rewrite_keeps_the_files_permissions(tmp_path):
    """Regression: a restore in an appliance rewrote /etc/rehome/appliance.toml as root
    with the temp file's 0600, and the kiosk (another user) could not read it."""
    path = tmp_path / "appliance.toml"
    toml.dump_atomic({"a": 1}, path)
    assert path.stat().st_mode & 0o777 == 0o600          # new files stay private
    path.chmod(0o644)
    toml.dump_atomic({"a": 2}, path)
    assert path.stat().st_mode & 0o777 == 0o644 and toml.load(path) == {"a": 2}


def test_default_route_detection(tmp_path):
    from rehome.vm import runner
    head = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    (tmp_path / "route").write_text(head + "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n")
    assert not runner.has_default_route(tmp_path)               # a local network only
    (tmp_path / "route").write_text(head + "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n")
    assert runner.has_default_route(tmp_path)
    (tmp_path / "route").write_text(head)
    (tmp_path / "ipv6_route").write_text(
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 ffffffff 00000001 00000000 00200200       lo\n")
    assert not runner.has_default_route(tmp_path)               # "unreachable" on lo
    with open(tmp_path / "ipv6_route", "a") as f:
        f.write("00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
                "fe800000000000000000000000000001 00000258 00000001 00000000 00000003    wlan0\n")
    assert runner.has_default_route(tmp_path)
    assert not runner.has_default_route(tmp_path / "missing")
