# SPDX-License-Identifier: GPL-3.0-or-later
#
# Rehome RPM for Fedora (and RHEL-likes) and openSUSE.
# Requires/Recommends mirror the "fedora" and "suse" entries of data/packages.toml
# (required = true -> Requires, optional -> Recommends); tests/unit/test_layout.py
# fails if they drift apart. Build with packaging/rpm/build.sh.

# Pure Python with a private module directory: no debuginfo, and no bytecode in
# the package (the launcher keeps bytecode in the user's cache directory).
%global debug_package %{nil}
%global __brp_python_bytecompile %{nil}

Name:           rehome
Version:        0.3.4
Release:        1%{?dist}
Summary:        Turn installed operating systems into QEMU/KVM virtual machines
License:        GPL-3.0-or-later
URL:            https://github.com/oreo1298/Disk-to-VM-Manager
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3 >= 3.11

%if 0%{?suse_version}
Requires:       qemu-x86
Requires:       qemu-tools
Requires:       qemu-ui-dbus
Requires:       qemu-ui-opengl
Requires:       qemu-audio-pipewire
Requires:       qemu-hw-usb-host
Requires:       qemu-hw-display-virtio-vga
Requires:       qemu-hw-display-virtio-gpu
Requires:       qemu-hw-display-virtio-gpu-pci
Requires:       qemu-ovmf-x86_64
Requires:       swtpm
Requires:       libguestfs
Requires:       python3-libguestfs
Requires:       guestfs-tools
Requires:       smartmontools
Requires:       polkit
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       python3-gobject-Gdk
Requires:       typelib-1_0-Gtk-4_0
Requires:       typelib-1_0-Adw-1
Requires:       typelib-1_0-Libosinfo-1_0
Requires:       libosinfo
Requires:       osinfo-db
Requires:       xorriso
Requires:       dosfstools
Requires:       mtools
Requires:       usbutils
Requires:       pciutils
Requires:       util-linux
Recommends:     qemu-arm
Recommends:     qemu-uefi-aarch64
Recommends:     virt-v2v
Recommends:     nbdkit
Recommends:     nbdkit-basic-plugins
Recommends:     virtiofsd
Recommends:     passt
Recommends:     gnu_ddrescue
Recommends:     ewftools
%else
Requires:       qemu-kvm
Requires:       qemu-img
Requires:       qemu-ui-dbus
Requires:       qemu-ui-opengl
Requires:       qemu-ui-egl-headless
Requires:       qemu-audio-pipewire
Requires:       qemu-device-usb-host
Requires:       qemu-device-display-virtio-vga
Requires:       qemu-device-display-virtio-vga-gl
Requires:       qemu-device-display-virtio-gpu-gl
Requires:       edk2-ovmf
Requires:       swtpm
Requires:       swtpm-tools
Requires:       libguestfs
Requires:       python3-libguestfs
Requires:       guestfs-tools
Requires:       smartmontools
Requires:       polkit
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       libosinfo
Requires:       osinfo-db
Requires:       xorriso
Requires:       dosfstools
Requires:       mtools
Requires:       usbutils
Requires:       pciutils
Requires:       util-linux
Recommends:     qemu-system-aarch64
Recommends:     edk2-aarch64
Recommends:     virt-v2v
Recommends:     nbdkit-server
Recommends:     nbdkit-basic-plugins
Recommends:     virtiofsd
Recommends:     passt
Recommends:     ddrescue
Recommends:     libewf-tools
%endif

%description
Rehome gives an installed operating system a new home as a QEMU/KVM virtual
machine. Point it at a physical disk or a disk image and it identifies the
system, applies the fixes it needs to boot on virtual hardware, and starts it,
without ever writing to the original disk.

It also creates and manages virtual machines from scratch, with an embedded
console, checkpoints, cloning, export and import.

%prep
%autosetup -n Disk-to-VM-Manager-%{version}

%build
# Nothing to build.

%install
python3 packaging/layout.py install \
    --prefix %{_prefix} \
    --libexecdir %{_libexecdir}/rehome \
    --python /usr/bin/python3 \
    --destdir %{buildroot} \
    --quiet

%files
%license LICENSE
%doc README.md
%{_bindir}/rehome
%{_prefix}/lib/rehome/
%{_datadir}/rehome/
%{_libexecdir}/rehome/
%{_datadir}/polkit-1/actions/io.github.oreo1298.Rehome.policy
%{_datadir}/applications/io.github.oreo1298.Rehome.desktop
%{_datadir}/metainfo/io.github.oreo1298.Rehome.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.oreo1298.Rehome.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.oreo1298.Rehome-symbolic.svg

%changelog
* Fri Oct 23 2026 Rehome contributors - 0.3.4-1
- Appliances: a normal shutdown or restart of the VM is no longer reported as a crash.

* Fri Oct 23 2026 Rehome contributors - 0.3.3-1
- Guest tools by Windows era, also when the system was chosen by hand: XP gets the
  SPICE tools 0.65, Windows 7 the SPICE tools 0.141, Windows 8+ virtio-win.

* Fri Oct 23 2026 Rehome contributors - 0.3.2-1
- Appliances: time zone of the build computer and internet time; clean power-off screen.
- Windows XP: SPICE guest tools and QXL graphics for resolutions up to 2560x1600.

* Thu Oct 22 2026 Rehome contributors - 0.3.1-1
- Appliances: network through Wi-Fi that connects after the VM started; built-in
  USB devices and Bluetooth stay with the appliance; clearer crash reports.

* Thu Oct 22 2026 Rehome contributors - 0.3.0-1
- Choose where disk images are stored (Preferences, per disk, rehome disk folder).
- Replace a VM's disk with another image or a new empty disk (rehome disk replace).
- Recognise installer ISOs libosinfo does not know from their own files; the New
  VM wizard lets you change the detected system.

* Thu Oct 22 2026 Rehome contributors - 0.2.1-1
- Appliances: the VM fills the screen and the pointer reaches all of it; modern
  guests get the screen's native resolution; touchscreens stay with the appliance.

* Wed Oct 21 2026 Rehome contributors - 0.2.0-1
- Appliances: build a drive image that boots a computer straight into a VM,
  full screen, with a maintenance console (rehome appliance build).

* Wed Oct 21 2026 Rehome contributors - 0.1.3-1
- Repair the BIOS boot chain of DOS and Windows 9x disks prepared on Linux.

* Wed Oct 21 2026 Rehome contributors - 0.1.2-1
- Use libguestfs' C library directly when its Python bindings are missing.

* Wed Oct 21 2026 Rehome contributors - 0.1.1-1
- Built-in NBD server when nbdkit is missing; Secure Boot key enrollment
  with virt-fw-vars; clearer libguestfs diagnosis in rehome doctor.

* Tue Oct 20 2026 Rehome contributors - 0.1.0-1
- First package.
