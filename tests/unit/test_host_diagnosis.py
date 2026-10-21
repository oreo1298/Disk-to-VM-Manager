from rehome.host import packages, tools


def test_guestfs_diagnosis():
    detail, _ = tools.guestfs_diagnosis("libguestfs.so.0: cannot open shared object file")
    assert "cannot be loaded" in detail and "libguestfs.so.0" in detail
    assert tools.guestfs_diagnosis("No module named 'guestfs'")[0] == "libguestfs is not installed."


def test_aur_only_packages_are_never_installed_with_pacman():
    assert "nbdkit" not in packages.packages_for(["nbdkit"], "arch")
    assert packages.install_command(["nbdkit"], "arch") is None
    assert packages.aur_packages(["nbdkit", "virt-v2v"], "arch") == ["nbdkit", "virt-v2v"]
    assert packages.aur_packages(["nbdkit"], "debian") == []
    assert packages.packages_for(["nbdkit"], "debian") == ["nbdkit"]
    assert packages.install_command(["virt-firmware"], "arch") == "sudo pacman -S --needed virt-firmware"
