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
Version:        0.3.21
Release:        1%{?dist}
Summary:        Turn installed operating systems into QEMU/KVM virtual machines
# LGPL: data/firmware (SeaBIOS's VGA BIOS)
License:        GPL-3.0-or-later AND LGPL-3.0-only
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
Recommends:     syslinux
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
Recommends:     syslinux
Recommends:     syslinux-extlinux
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
* Tue Oct 27 2026 Rehome contributors - 0.3.21-1
- Appliances with a DOS or Windows 9x VM no longer show a second cursor stuck in the
  middle of the screen.

* Tue Oct 27 2026 Rehome contributors - 0.3.20-1
- Windows 95/98/ME: the VGA BIOS runs every call on the caller's stack, without its
  own stack in upper memory, which Windows maps for each of its virtual machines.
  Windows 98 hung at a black screen with a blinking cursor when it shut down.

* Tue Oct 27 2026 Rehome contributors - 0.3.19-1
- Appliances: a laptop's touchpad drives DOS and Windows 9x VMs directly, with tap to
  click and two-finger scrolling, instead of stopping at the screen's edges. Its speed
  and tap to click are set on the maintenance console's "Screen and touchpad" page.
- Diagnostics of a running VM include the BIOS's video state, the code the processor
  runs and its stack.

* Tue Oct 27 2026 Rehome contributors - 0.3.18-1
- Windows 95/98/ME: the VM turns off a few seconds after Windows has finished
  shutting down ("It's now safe to turn off your computer"), also without the APM
  driver in Windows.

* Tue Oct 27 2026 Rehome contributors - 0.3.17-1
- Windows 95/98/ME: the VGA BIOS keeps its extra stack for DOS again (0.3.16 dropped
  every other character before Windows started, under KVM), and keeps it off once
  Windows runs, so Windows 98 no longer hangs at a black screen when it shuts down.

* Tue Oct 27 2026 Rehome contributors - 0.3.16-1
- Windows 95/98/ME: the VGA BIOS no longer uses a stack in upper memory, so Windows
  98 no longer hangs at a black screen with a blinking cursor when it shuts down.

* Tue Oct 27 2026 Rehome contributors - 0.3.15-1
- rehome diagnostics: for a running VM, where its processor is (mode, CS:EIP, halted
  or not), to find out where a guest hangs.

* Tue Oct 27 2026 Rehome contributors - 0.3.14-1
- Windows 95/98/ME: a VGA BIOS without debug output (SeaBIOS 1.17.0), so Windows 98
  no longer stops with "A fatal exception 0D has occurred at C000:00002007" when
  it shuts down.
- Appliance maintenance console: a page that fails shows the error instead of
  closing the console; USB drives that can only be read are refused for saving.

* Sat Oct 24 2026 Rehome contributors - 0.3.13-1
- Appliances: after restoring a backup (or setting the maintenance password) the
  VM did not start: the appliance's configuration became unreadable to it.

* Sat Oct 24 2026 Rehome contributors - 0.3.12-1
- Appliances: the start screen no longer mentions the maintenance console (M still
  opens it). The console gains backups and restores on USB drives, memory and
  CD/DVD images, date and time, sound, screen, startup and shutdown choices, the
  maintenance password, storage and logs.

* Fri Oct 23 2026 Rehome contributors - 0.3.11-1
- Appliances: VMs with only a PS/2 mouse (DOS, Windows 9x) get the computer's mice
  directly; the mouse no longer stops at invisible edges.

* Fri Oct 23 2026 Rehome contributors - 0.3.10-1
- Windows 95/98: patcher9x's fix for "While initializing device NDIS: Windows
  protection error" on fast computers, emulated ones included.

* Fri Oct 23 2026 Rehome contributors - 0.3.9-1
- Console: the mouse moves again in DOS and Windows 9x (PS/2 mice were taken for
  absolute ones); captured moves follow the host pointer and line up at the edges.

* Fri Oct 23 2026 Rehome contributors - 0.3.8-1
- Switching Windows to virtio first makes the virtio disk driver load at boot
  (Windows 7 stopped with 0x7B); reverting the checkpoint restores the devices.

* Fri Oct 23 2026 Rehome contributors - 0.3.7-1
- Windows 95/98 disks prepared on Linux get Rehome's own boot sector, which starts
  IO.SYS like grub4dos (SYSLINUX's chain.c32 stopped at "Booting...").

* Fri Oct 23 2026 Rehome contributors - 0.3.6-1
- DOS/Windows 9x boot repair falls back to this computer's own syslinux when
  libguestfs's helper has none.

* Fri Oct 23 2026 Rehome contributors - 0.3.5-1
- DOS/Windows 9x boot repair falls back to extlinux when libguestfs lacks mtools;
  failed automatic fixes keep their details.

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
