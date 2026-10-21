from rehome.host import packages, tools


def test_guestfs_load_error_is_shown():
    detail, fix = tools.guestfs_diagnosis("libguestfs.so.0: cannot open shared object file")
    assert "cannot be loaded" in detail and "libguestfs.so.0" in detail


def test_guestfs_bindings_for_another_python(tmp_path, monkeypatch):
    other = tmp_path / "lib" / "python3.11" / "site-packages"
    other.mkdir(parents=True)
    (other / "guestfs.py").write_text("")
    monkeypatch.setattr(tools, "GUESTFS_BINDING_GLOBS", (str(tmp_path / "lib/python3*/site-packages/guestfs.py"),))
    detail, _ = tools.guestfs_diagnosis("No module named 'guestfs'")
    assert "installed only for python3.11" in detail


def test_guestfs_without_bindings_or_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "GUESTFS_BINDING_GLOBS", ())
    monkeypatch.setattr(tools, "which", lambda name, *a: "/usr/bin/guestfish")
    assert "without Python bindings" in tools.guestfs_diagnosis("No module named 'guestfs'")[0]
    monkeypatch.setattr(tools, "which", lambda name, *a: None)
    monkeypatch.setattr(tools.glob, "glob", lambda pat: [])
    assert tools.guestfs_diagnosis("No module named 'guestfs'")[0] == "libguestfs is not installed."


def test_aur_only_packages_are_never_installed_with_pacman():
    assert "nbdkit" not in packages.packages_for(["nbdkit"], "arch")
    assert packages.install_command(["nbdkit"], "arch") is None
    assert packages.aur_packages(["nbdkit", "virt-v2v"], "arch") == ["nbdkit", "virt-v2v"]
    assert packages.aur_packages(["nbdkit"], "debian") == []
    assert packages.packages_for(["nbdkit"], "debian") == ["nbdkit"]
    assert packages.install_command(["virt-firmware"], "arch") == "sudo pacman -S --needed virt-firmware"
