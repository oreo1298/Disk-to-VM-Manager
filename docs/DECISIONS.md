# Design decisions

Each entry records what was decided, the alternatives considered, and why.
Newest entries are appended at the end.

---

## D1. Name, ID and license

- **Name:** Rehome. The app gives an installed OS a new home as a VM.
  The CLI is `rehome`, the application ID is `io.github.oreo1298.Rehome`,
  and the Python package is `rehome`.
- **License:** GPL-3.0-or-later. It matches the ecosystem we depend on
  (QEMU and libguestfs are GPL/LGPL, PyGObject and GTK are LGPL), it is the
  norm for GNOME-platform apps, and it keeps the patch engine (which encodes
  a lot of hard-won P2V knowledge) open for everyone.

## D2. Language: Python 3.11+

**Chosen:** Python 3.11+, using only the standard library, PyGObject and the
libguestfs Python bindings.

**Alternatives considered:** Rust with gtk4-rs, C++ with Qt 6, and Python with Qt.

**Why:**
- First-class bindings to everything the core needs. libguestfs (including
  its built-in hivex API for offline Windows registries) has official Python
  bindings. libosinfo, GTK 4 and libadwaita come through GObject
  introspection.
- No pip or venv at install time. Every dependency is a distro package on
  Arch, Debian/Ubuntu, Fedora and openSUSE, so the install script and native
  packages stay simple.
- The heavy lifting (emulation, disk I/O, guest inspection) happens in QEMU,
  qemu-img and libguestfs. Rehome orchestrates them, and Python is well
  suited to that.
- `tomllib` (3.11+) reads the data-driven OS profiles with no third-party
  parser.

**Trade-off:** no static typing guarantees. This is mitigated by type hints
and a broad pytest suite that runs against synthetic disk images.

## D3. UI toolkit: GTK 4 + libadwaita

**Chosen:** GTK ≥ 4.10 and libadwaita ≥ 1.4.

**Why:**
- **Follows the system theme across desktops.** libadwaita reads the
  color scheme and (from 1.6) the accent color from xdg-desktop-portal's
  `org.freedesktop.appearance` namespace, and updates live. KDE Plasma 6's
  portal backend implements both keys, so the app follows Plasma's
  light/dark setting and accent color too. For libadwaita 1.4–1.5,
  `rehome/ui/theme.py` reads the portal accent color itself and applies it
  through CSS, so the behavior is the same.
- **Platform support comes built in.** Wayland and X11, fractional HiDPI,
  keyboard navigation and AT-SPI accessibility are all native.
- **The console fits.** The best console option (D7) produces textures,
  and GTK 4 renders those directly, including DMABUFs (GTK ≥ 4.14).

**Qt 6 was rejected.** It is native on Plasma, but on GNOME it does not
follow the accent color. More importantly, no maintained Qt widget exists
for QEMU's modern display protocols.

**Distros without libadwaita ≥ 1.4** (Debian 12, Ubuntu 22.04) get the full
CLI, and `rehome doctor` explains that the GUI needs a newer distro.

## D4. QEMU directly over QMP, not libvirt

**Chosen:** Rehome builds QEMU command lines itself and talks to each VM over
QMP. VMs run as the desktop user.

**Why:**
- **Raw-disk safety and privilege separation.** A physical disk is opened
  read-only (and `O_EXCL`) by a tiny polkit helper. The file descriptor is
  handed to QEMU through `-add-fd`/fdsets, so neither the GUI nor QEMU ever
  runs as root.
  - libvirt's `qemu:///system` runs QEMU as a separate system user. That
    user cannot read the user's files, and granting management rights over
    polkit is effectively root.
  - `qemu:///session` cannot open root-owned devices at all.
- **Full control of the block graph.** Imports need a qcow2 overlay whose
  backing node is overridden at runtime (an fdset, an NBD export or a FUSE
  path). That is natural with `-blockdev` and awkward in domain XML.
- **Legacy hardware.** DOS and Win9x need ISA devices (`sb16`, `ne2k_isa`,
  `cirrus-vga`, floppy), PIT tuning, TCG and icount, which are first-class
  on the QEMU command line.
- **Fewer moving parts.** There is no daemon to enable, no `libvirt` group
  and no network service to start. This matches "the GUI never runs as
  root" and makes the install script trivial.

**Cost:** we implement lifecycle, sidecars (swtpm, virtiofsd), NAT
networking (passt, or slirp) and snapshots ourselves. QEMU provides every
primitive, and these are small, well-tested modules.

## D5. Process model: detached QEMU, no daemon

**Chosen:** Each VM's QEMU runs in its own session (`setsid`) and outlives
the GUI or CLI that started it, the way Hyper-V VMs keep running when the
manager closes.

**How it works:**
- Runtime state lives in `$XDG_RUNTIME_DIR/rehome/vms/<uuid>/`: QMP
  sockets, pidfiles and sidecar sockets.
- On startup Rehome rediscovers running VMs by pidfile, a
  `/proc/<pid>/cmdline` UUID check and a QMP handshake.
- swtpm runs with `--terminate` and virtiofsd exits with its vhost-user
  peer, so sidecars die with QEMU without a supervisor.
- Each VM has two QMP sockets: one for the GUI's long-lived event
  connection and one for short CLI operations, so `rehome` commands work
  while the GUI is open.

## D6. Storage

- **Every disk is described with `-blockdev`.** Sources are always
  `read-only=on`.
- **Linked import:** a per-disk qcow2 overlay whose backing is the source.
  - Image files are referenced by absolute path plus explicit
    `backing_fmt`.
  - Physical disks are backed at runtime by an fdset from the polkit
    helper.
  - For offline tools (qemu-img, libguestfs, virt-v2v), the same fd is
    served by `nbdkit file fd=N` on a per-user Unix socket, and that socket
    is what the overlay header names. Where nbdkit is not packaged, Rehome's
    own read-only NBD server does the same (D23).
- **Copy import:** `qemu-img convert -O qcow2` (sparse, compressed off),
  after which the source is no longer needed.
- **Image formats:**
  - raw/dd/img, VHD (`vpc`), VHDX, VMDK, VDI and qcow2 use QEMU's built-in
    drivers, read-only.
  - E01 goes through libewf. Linked mode uses `ewfmount` (FUSE); Copy mode
    uses `ewfexport`.
- **UEFI variable stores** are per-VM qcow2 files so that checkpoints
  capture them.

## D7. Embedded console: QEMU D-Bus display with our own GTK 4 widget

We evaluated three options.

| Option | Toolkit | Clipboard | Resize | USB | Availability |
|---|---|---|---|---|---|
| libmks | GTK 4 (C) | no | yes | no | Arch, Fedora; not in Debian/Ubuntu |
| spice-gtk | **GTK 3 only** | yes | yes | yes | everywhere |
| VNC (gtk-vnc) | **GTK 3 only** | partial | limited | no | everywhere |
| D-Bus display + own widget | GTK 4 | yes | yes | yes (see below) | QEMU ≥ 7.0, everywhere |

- spice-gtk and gtk-vnc cannot be embedded in a GTK 4 window.
- libmks is a good GTK 4 widget, but it is a 0.x API, it is missing from
  Debian and Ubuntu, and it has no clipboard support.

**Chosen:** QEMU's D-Bus display in peer-to-peer mode (`-display
dbus,p2p=yes`, connected through QMP `add_client`), rendered by our own
widget (`rehome/ui/console/`).

A prototype (`tests/integration/test_dbus_display.py`) confirmed against
QEMU 8.2 that pure Python receives `Scanout` and `Update` calls fast enough
for 2D; a full 640×480 frame plus around 300 updates per 8 seconds under
TCG is negligible. GTK renders frames from `Gdk.MemoryTexture`. Newer QEMU
adds `Listener.Unix.Map` (shared memory, zero-copy), which we implement
when offered.

**How each console feature is provided:**
- **Clipboard:** `org.qemu.Display1.Clipboard`, with QEMU's built-in
  `qemu-vdagent` chardev talking to the guest's normal spice-vdagent.
- **Dynamic resolution:** `Console.SetUIInfo`, which virtio-gpu and QXL
  forward to the guest.
- **USB redirection:** the polkit helper opens the selected
  `/dev/bus/usb/BBB/DDD` and passes the fd. Rehome then sends QMP `add-fd`
  and `device_add usb-host,hostdevice=/dev/fdset/N`. This gives hot-plug
  without running QEMU as root or loosening device permissions.
- **3D (virgl):** `ScanoutDMABUF` → `Gdk.DmabufTextureBuilder` (GTK ≥ 4.14),
  experimental.
- **Fallback** (QEMU built without the D-Bus display module): QEMU gets a
  VNC (or, with `rehome set VM --console spice`, SPICE) Unix socket instead.
  The console window then offers **Open in External Viewer**
  (`remote-viewer`, or TigerVNC's `vncviewer`). `rehome console VM --external`
  does the same from the CLI.

Two behaviours of QEMU 8.2 found while testing shape the client (D16).

## D8. Privileged helper

**Chosen:** a single small helper, `rehome-helper`, invoked through `pkexec`
with one polkit action per verb.

The helper is written in Python, run as `python3 -I` (isolated mode), with
no imports outside the standard library. pkexec clears the environment and
closes fds above 2, so the reply channel is a socketpair passed as stdin.

**Verbs:**
- **`open-disk DEV`**
  - Checks that DEV is a block device and that it is not the host's root,
    `/boot`, swap or a holder of either.
  - Opens it `O_RDONLY|O_EXCL`. The kernel refuses `O_EXCL` on a whole
    disk while any partition is mounted, swapped on, or claimed by
    device-mapper, md or btrfs. The claim also stops the host from mounting
    it while the VM runs.
  - Returns the fd by `SCM_RIGHTS`.
  - Policy: `auth_admin_keep`.
- **`open-usb BUS DEV`**
  - Opens the USB device node read/write and returns the fd.
  - Policy: `yes` for the active local session, the same as spice-gtk's
    USB ACL helper.
- **`smart DEV`**
  - Runs `smartctl --json` and prints the result.
  - Policy: `yes` for the active local session (read-only health data).

**Alternatives considered:**
- udisks2 `OpenForBackup` gives us an fd, but it cannot express our host-OS
  checks or the `O_EXCL` guarantee, and it is not present on minimal
  installs.
- A D-Bus system service would be more code running as root.

## D9. Data-driven OS profiles and patches

- **Profiles** (`data/profiles/*.toml`) match on detected OS facts (family,
  NT version, build, architecture, distro). They declare hardware, firmware,
  RTC mode, accelerator preference, limits and an ordered patch list.
- **Patches** are small Python functions registered under stable IDs
  (`windows.storage.enable_boot_drivers`, `linux.initramfs.add_virtio`, …).
  Profiles reference them by ID with parameters. Lists of services to
  disable, driver names and device IDs also live in data files.
- **virt-v2v** (`virt-v2v-in-place`) is preferred where it supports the
  guest. The built-in patches cover older systems, or hosts without
  virt-v2v or virtio-win.

## D10. Checkpoints

**Chosen:** qcow2 internal snapshots.
- **Online:** QMP `snapshot-save`/`snapshot-load` jobs, which include RAM.
  This is the equivalent of Hyper-V's standard checkpoints.
- **Offline:** `qemu-img snapshot`.

Parent/child relationships, names and descriptions are tracked in the VM
config, so the UI can show a tree with a "Now" marker like Hyper-V.

## D11. Networking

- **NAT (default):** `passt` when installed, since it is unprivileged and
  faster than slirp with IPv6 support. Otherwise QEMU's built-in user-mode
  networking.
- **Bridged:** `qemu-bridge-helper` with an existing bridge. `doctor`
  checks the helper's setuid bit and `/etc/qemu/bridge.conf`.

## D12. Configuration format

- VM configs are TOML (`vm.toml`) so users can read and edit them.
  `tomllib` reads them, and a small writer in `rehome/util/toml.py` writes
  the subset we use.
- Profiles and catalogs are TOML too.

## D13. Packaging: native packages, no Flatpak or Snap (for now)

**Chosen:** an install script plus native packages: PKGBUILD for the AUR,
.deb and .rpm.

**Why not Flatpak or Snap:**
- **Raw disk access.** It needs a host-side polkit helper and `O_EXCL`
  device opens. The sandbox cannot install polkit actions, and
  `--device=all` with host filesystem access removes most of the sandbox's
  value.
- **libguestfs.** It boots an appliance built from the *host* kernel in
  `/boot` and needs `/dev/kvm`. Inside a Flatpak runtime the appliance
  would have to be bundled (hundreds of MB) and kept in sync with security
  updates.
- **Host integration.** swtpm, virtiofsd, passt, qemu-bridge-helper
  (setuid) and USB device nodes are all host-integrated.
- **GNOME Boxes** shows the trade-off. It ships as a Flatpak with QEMU
  bundled, and it can create VMs but cannot import physical disks.

**Possible later:** a Flatpak "create and manage only" edition that uses
host tools via `flatpak-spawn --host`. That would cost the sandbox anyway.

The full evaluation (Snap classic, AppImage, a possible limited Flatpak
edition) is in [PACKAGING.md](PACKAGING.md#flatpak-and-snap-decision-d13-extended).

## D14. Testing strategy

- **Unit tests** (pytest) for detection parsing, command-line generation,
  profile matching, TOML I/O, keymaps and the registry patch logic.
- **Integration tests** on synthetic disk images built at test time:
  - partitioned raw images with ext4, FAT, NTFS and UFS-like layouts
  - Windows-like NTFS trees with real registry hives built by a small
    regf generator plus hivex
  - FreeDOS and Win9x directory layouts
  - VMDK, VHDX and VDI conversions of these images
- **Boot tests** run QEMU headless. Progress is verified with QMP
  `screendump`, text-mode VGA memory reads (`pmemsave` of 0xB8000) for DOS
  and other text consoles, serial-console markers for Linux, and optional
  OCR with tesseract.
- **Source integrity check:** every import test hashes the source before
  and after import and boot.

## D15. Development environment for this build

Development ran in a cloud container: Ubuntu 24.04, no `/dev/kvm`, and a
restricted network policy.
- Boot tests ran under TCG, which is correct but slow.
- The install script was exercised in Docker containers.
- Hosts blocked by the network policy (Arch, Debian, Fedora and openSUSE
  mirrors, FreeBSD and Microsoft downloads) limited some tests.
- `/dev/kvm` was available during the first part of the work, then the
  container moved to a host without it, so both paths were exercised.
- [TESTING.md](TESTING.md) lists exactly what was verified and what must be
  re-run on a KVM host with real images.

## D16. Console client details learned from QEMU 8.2

- **Video mode changes.** QEMU does not send a new `Scanout` when the guest
  changes video mode, for example 640×480 → 720×400 when SeaBIOS hands over
  to DOS. It only updates the console's `Width`/`Height` properties. The
  `Update` calls for the new size can arrive before that property change.
  The client therefore resizes its framebuffer on both signals and keeps the
  overlapping pixels. Before this fix, the right edge of DOS screens was cut
  off.
- **No blocking calls after `RegisterListener`.** QEMU handles
  `RegisterListener` by authenticating the new listener connection
  synchronously, which needs our main loop to run. A synchronous D-Bus call
  made after it deadlocks both sides until the 25-second D-Bus timeout. All
  proxies, including the clipboard, are therefore set up first, and the
  listener is registered last. The integration test fails if connecting
  takes more than 5 seconds.
- **Relative mouse.** GTK 4 has no API to confine or warp the pointer. For
  guests without an absolute pointing device, the console hides the cursor
  and sends motion deltas while the pointer is over the view, and Ctrl+Alt
  releases it. Profiles therefore give every guest that supports one an
  absolute device (USB tablet, or virtio-tablet with the drivers). Only DOS,
  Windows 9x, NT 4 and ReactOS use a relative PS/2 mouse, because they have no
  usable tablet driver.

## D17. Networking fallback and helper processes

- If `passt` is installed but fails to start, Rehome starts the VM with
  QEMU's built-in NAT (slirp) and logs a warning instead of failing. This
  happens in sandboxes such as gVisor, where passt cannot create its socket.
- Rehome records the PIDs of helper processes (passt, swtpm, virtiofsd) in
  the VM's runtime directory. Before signalling one, it checks
  `/proc/PID/cmdline`, so a PID reused by an unrelated process is never
  killed. A failed helper's pidfile is removed immediately.

## D18. Clock catch-up only where it helps

`-rtc driftfix=slew` (catch-up of lost timer ticks) is what NT-family guests
expect. However, it stalled ReactOS for minutes at boot and slows TCG guests,
as bisecting the ReactOS boot showed. It is therefore a profile setting
(`rtc_catchup`), enabled for Windows NT-family profiles and applied only when
the VM runs on KVM.

## D19. Hooks as the pass-through extension point

GPU/PCI pass-through is out of scope for v1, but users can experiment safely:

- `prepare` hooks may write QEMU arguments to `$REHOME_QEMU_ARGS_FILE`.
- `stopped` hooks run even when a start fails after `prepare`, so they can
  undo host changes.
- The `[passthrough]` config section is reserved, and a VM that uses it is
  refused with a pointer to docs/HOOKS.md.

This keeps the risky part (unbinding host drivers) in the user's hands
without Rehome having to support it.

## D20. Guest tools

- **Windows:** the virtio-win ISO from the Fedora project (fedorapeople.org).
  It is the upstream, signed source of the virtio drivers, the QEMU guest
  agent and the SPICE agent. Rehome uses an installed copy if the
  distribution ships one, and downloads it (about 700 MB) only when the user
  asks.
- **Linux:** the distribution's own packages (`qemu-guest-agent`,
  `spice-vdagent`). Rehome shows the right install command for the guest's
  distribution.
- **Switching to virtio:** only after the user confirms the first boot and
  installs the drivers. A checkpoint is taken first, so it can be undone.

## D21. Verifying boots without a human

`tests/boot` reads the success or failure of a boot from several signals:

- the serial console log (Linux, BSD),
- VGA text memory (DOS, BIOS messages, text-mode blue screens),
- OCR of a QMP screendump with tesseract (graphical logins and desktops,
  graphical blue screens),
- the QEMU guest agent, when it is installed.

Failure patterns (STOP codes, kernel panics, "grub rescue>" and similar) are
checked first. Images and expectations come from an `images.toml` file next
to the user's images, so real Windows or 9x disks never have to be committed
or downloaded.


## D22. Quick Create and unattended installation

- **Sources.** Each catalog entry (`data/catalog.toml`) resolves its current
  release from the project's official site. Rehome verifies every download
  against the checksum file the project publishes, or SourceForge's SHA-256
  for ReactOS, says exactly what was verified, and resumes interrupted
  downloads. Windows entries only open Microsoft's download page.
- **Windows answer file.** Rehome writes its own `autounattend.xml`, put on
  a small `UNATTEND` disc, instead of libosinfo's script. libosinfo's
  Windows script (osinfo-db 2025-06) has no Windows 11 link, creates a
  single BIOS-style partition (wrong for UEFI), and requires a product key.
  Rehome's file:
  - partitions for UEFI or BIOS,
  - creates a local account using Microsoft's documented settings,
  - leaves the product key empty unless the user gives one (for evaluation
    media),
  - contains no TPM, Secure Boot, Microsoft-account or activation bypasses.

  Rehome presses a key once at "Press any key to boot from CD or DVD", on
  every start path (CLI, VM page, console, wizard).
- **Linux answer files.**
  - Ubuntu (Server 20.04+, Desktop 24.04+): its own autoinstall format on
    a `cidata` disc.
  - Fedora/RHEL family, openSUSE and Debian: libosinfo's scripts.
  - The installer's kernel is started directly, once (`-kernel`, with
    `-no-reboot`), and told to power off at the end. The next start then
    boots the installed disk, and a reboot can never start the installer
    again. This is incompatible with Secure Boot, which is refused up front
    with an explanation.
- **Secrets.** Answer files are created mode 0600 in a 0700 folder. The
  Ubuntu config stores only a password hash. Passwords never appear in logs.
  `rehome unattended-cleanup` and the Summary tab remove the files after the
  installation.

## D23. What Arch does not package

Real-world feedback from CachyOS (Arch family) showed two gaps in Arch's
official repositories.

- **nbdkit is AUR-only.** Rehome now ships a small read-only NBD server
  (`rehome/disk/nbdserve.py`, standard library only). It implements the
  fixed-newstyle handshake, read, flush and disconnect, and refuses every
  write with EPERM. It serves the helper's fd whenever nbdkit is missing, so
  nbdkit is optional everywhere.
  - **Tested** against qemu-img (a byte-identical full copy), qemu-io
    (writes refused) and libguestfs (filesystems listed). A full
    physical-disk import ran from a loop device through the real helper:
    linked with `--verify` and copy, booted, source hash unchanged.
  - virt-v2v is AUR-only too. `data/packages.toml` lists AUR-only packages
    under `aur`, so the installer never asks pacman for them and the
    doctor points to the AUR instead.
- **No Microsoft-enrolled Secure Boot store.** Arch's edk2-ovmf ships
  Secure Boot capable code but only an empty `OVMF_VARS.4m.fd`.
  - Rehome enrolls the keys into a private copy of that store, once per
    template, under `~/.local/share/rehome/firmware/`. It uses
    `virt-fw-vars --enroll-redhat --secure-boot` from Arch's
    `virt-firmware`, the same tool and certificates as Fedora's
    pre-enrolled store.
  - The copy contains Microsoft's KEK 2011/2023, the Windows Production PCA
    2011, Windows UEFI CA 2023 and UEFI CA 2011/2023.
  - **Tested** by masking the host's descriptors with Arch-style ones. An
    Ubuntu guest booted with Secure Boot enforced: shim accepted, kernel
    lockdown active.
  - `virt-firmware` is a required package only for the Arch family, since
    the others ship a pre-enrolled store.
- **libguestfs without Python bindings.** Arch's current libguestfs package
  ships the C library and guestfish, but no Python module (`pacman -Ql
  libguestfs` lists no `guestfs.py`).
  - Rehome now calls the C library directly through ctypes
    (`rehome/inspect/guestfs_ctypes.py`) whenever `import guestfs` fails.
    It is a drop-in for the bindings' `GuestFS` class: same names, return
    types and errors.
  - The C signatures and option structs come from `guestfs.h`.
    `scripts/gen_guestfs_sigs.py` generates `guestfs_sigs.py`, so nothing is
    compiled on the user's machine.
  - libguestfs keeps its ABI stable (optional arguments are only appended,
    with fixed bitmask bits), so the table made from 1.52 works with newer
    releases.
  - The header cannot tell an int result from a success code or a boolean,
    so the generator refuses any int-returning call that is not classified
    by hand. A unit test fails if the code calls a libguestfs function
    missing from the table.
  - The bindings stay preferred where installed. `REHOME_GUESTFS=ctypes`
    forces the C-library path, and the whole integration suite passes with
    it.

## D24. Repairing the BIOS boot chain of DOS-era disks

A Windows 98 VDI stopped at "This is not a bootable disk. Please insert a
bootable floppy". That is the placeholder boot code Linux's `mkfs.fat`
writes: the files had been copied onto a partition formatted on Linux, so
the partition never received Microsoft's boot code.

- `rehome/inspect/bootchain.py` reads the chain: the MBR code, which
  partition is active, and the boot sector code of the system's partition.
- The `dos.boot_record` fix repairs what is missing, in the overlay:
  - an MBR without code gets SYSLINUX's `mbr.bin` (the partition table is
    kept);
  - the system's partition is marked active;
  - an unbootable boot sector is replaced by SYSLINUX, whose `chain.c32`
    starts the system's own loader: `msdos7=/io.sys` for Windows 9x and
    MS-DOS 7, `msdos=` for MS-DOS 6, `pcdos=` for PC DOS, `freedos=` for
    FreeDOS.
- Microsoft's boot code cannot be shipped, and Windows 98's `SYS.COM` needs
  DOS to run; SYSLINUX is free and documents these modes.
- Files are written before the boot sector, so a failure leaves the boot
  code as it was.
- **Tested** with FreeDOS disks broken both ways: FAT16 with only the
  placeholder, and FAT32 with no MBR code and no active partition. Both
  boot after the repair, with both libguestfs backends, and the sources
  stay unchanged. The `msdos7` path to a real Windows 98 `IO.SYS` still
  needs a real Windows 98 disk to test.


## D25. Appliance base system: Ubuntu 26.04 LTS, built without root

An appliance needs a Linux that supports the newest laptops and desktops it
may be moved to, for years, and that Rehome can build on any distribution
without root.

- **Ubuntu 26.04 LTS** (`ubuntu-base`, 35 MB): a recent kernel and Mesa for
  new hardware, linux-firmware, QEMU 10, `cage` and `iwd` packaged, and
  security updates until 2031. Alpine was the other candidate (smaller), but
  its kernels lag for new GPUs and Wi-Fi, and its mirrors were not reachable
  from the build environment used for development.
- **No root, no network inside libguestfs.** libguestfs's helper VM gets its
  network from passt or a DHCP client, and both vary between distributions
  (on Ubuntu the appliance had no DHCP client; in containers passt cannot
  run). So Rehome downloads on the host and apt runs offline in the image:
  apt is shown the archive's indexes through a `file:` mirror, prints the
  packages it would fetch (`--print-uris`), Rehome downloads exactly those,
  copies them in, and apt installs them. apt's own dependency resolution is
  used; nothing is re-implemented.
- **Trust chain:** the ubuntu-base SHA256 is pinned in
  `data/appliance/base.toml` (checked against Ubuntu's signed SHA256SUMS
  when added); apt in the image checks each `InRelease` signature with the
  keyring from that base; the host checks indexes and packages against the
  signed hashes as it downloads them; apt checks the packages again.
- **Cached base.** The installed system (no Rehome configuration) is cached
  as a qcow2 file in `~/.cache/rehome/appliance` and reused for 30 days (or
  until `--refresh`), so later appliances build in minutes.
- `update-grub` is diverted: the appliance's boot menu is written by Rehome,
  not generated.

## D26. Appliance disk layout and boot

- GPT with a BIOS boot partition, a 256 MiB EFI system partition, a 6 GiB
  ext4 system partition and an ext4 data partition holding the VM. The same
  image boots on BIOS and UEFI computers (GRUB installed for both, the UEFI
  one as the removable-media path `EFI/BOOT/BOOTX64.EFI`, so no firmware
  boot entry is needed). Secure Boot is not supported: the person setting
  the computer up turns it off.
- Each build gets new filesystem UUIDs, so two appliance drives in one
  computer do not confuse each other's boot menus.
- The data partition is sized to the VM at build time and grown to fill the
  drive on first boot, so the image file stays small.
- GRUB's menu is hidden (Esc shows it) and has: start the VM, start it with
  basic graphics (`nomodeset`, software rendering), maintenance console.
  After a boot that did not reach the VM, the menu is shown automatically
  (`recordfail` in grubenv, cleared once the VM runs).

## D27. Running the VM full screen

- `cage` (a Wayland kiosk compositor) shows exactly one application; QEMU's
  GTK display runs in it full screen, scaled to the screen, without menus.
  If cage cannot use the GPU, it is restarted with software rendering.
- The VM runs as an unprivileged `rehome` user with a logind seat (the
  kiosk service uses `PAMName=login` on tty1), in the `kvm`, `audio`,
  `video`, `render` and `input` groups. A udev rule gives that user the USB
  devices, so pass-through needs no helper. VT switching is disabled.
- The VM is the normal Rehome VM (same `vm.toml`, same QEMU command line
  builder) with `REHOME_HOME=/var/lib/rehome`; only the console kind differs
  (`kiosk`). The disks are flattened into standalone qcow2 files at build
  time, so checkpoints keep working inside the appliance.
- Guest shuts down → QEMU exits → the computer powers off. The power button
  sends an ACPI shutdown to the guest instead of cutting it off.
- Audio goes straight to ALSA (no sound server needed).

## D28. Maintenance console

- A GRUB entry (`rehome.mode=maintenance`) boots to a text menu on tty1
  instead of the VM: start the VM, checkpoints, network and Wi-Fi, hardware
  report, save diagnostics to a USB stick, a root shell, reboot, power off.
- The kiosk also falls back to it when the VM cannot start, with the error
  on screen.
- It runs as root without a password by default: whoever reaches the boot
  menu has the machine in hand anyway. `--maintenance-password` sets a root
  password that the console asks for.
