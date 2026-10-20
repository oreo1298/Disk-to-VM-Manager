import pytest

from rehome.cli import cmd_usb, main
from rehome.errors import NotFound
from rehome.vm import usb


def _dev(root, name, bus, devnum, vid, pid, cls="00", product=""):
    d = root / name
    d.mkdir()
    for k, v in {"busnum": bus, "devnum": devnum, "idVendor": vid, "idProduct": pid,
                 "bDeviceClass": cls, "product": product, "manufacturer": "", "speed": "480"}.items():
        (d / k).write_text(f"{v}\n")


@pytest.fixture
def fake_usb(tmp_path, monkeypatch):
    _dev(tmp_path, "usb1", 1, 1, "1d6b", "0002", cls="09", product="root hub")
    _dev(tmp_path, "1-1", 1, 4, "0781", "5581", product="Ultra")
    _dev(tmp_path, "1-2", 1, 5, "046d", "c52b", product="Receiver")
    _dev(tmp_path, "2-1", 2, 3, "046d", "c52b", product="Receiver")
    (tmp_path / "1-1:1.0").mkdir()              # interfaces are skipped
    monkeypatch.setattr(usb, "SYS_USB", tmp_path)
    return tmp_path


def test_list_skips_hubs_and_interfaces(fake_usb):
    keys = [d.key for d in usb.list_host_devices()]
    assert keys == ["001-004", "001-005", "002-003"]


def test_find_device_by_key_or_id(fake_usb):
    assert cmd_usb._find_device("001-004").product == "Ultra"
    assert cmd_usb._find_device("0781:5581").key == "001-004"
    with pytest.raises(NotFound, match="Several"):
        cmd_usb._find_device("046d:c52b")
    with pytest.raises(NotFound):
        cmd_usb._find_device("9-9")


def test_cli_list(fake_usb, capsys):
    assert main.main(["usb", "list"]) == 0
    out = capsys.readouterr().out
    assert "001-004" in out and "0781:5581" in out and "root hub" not in out
