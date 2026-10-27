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

- `cage` (a Wayland kiosk compositor) shows exactly one application: QEMU's
  SDL display, full screen and scaled to the screen. SDL rather than GTK:
  it has no menus and no hotkey that quits QEMU (GTK has Ctrl+Alt+Q), and
  GTK on Ubuntu 26.04 loads icons through glycin's sandboxed loaders, which
  failed in testing and make GTK abort. SDL runs without libdecor (its
  fallback never shows the window). Without a GPU render node, or with
  basic graphics, cage uses its CPU renderer (pixman); if cage still cannot
  start, it is retried with pixman.
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

## D29. Appliance display: scaling, pointer and resolution

Reported from a ThinkPad X1 Carbon running an Omarchy VM: the picture did
not fill the screen and the pointer could not reach large parts of it.

- **Cause of the pointer problem.** QEMU 10's SDL display converts mouse
  positions from window to guest pixels (`x * guest_width / window_width`).
  In its 2D mode it also sets SDL's logical size to the guest's size, and
  SDL (and sdl2-compat on SDL3) then already reports mouse positions in
  guest pixels. Scaled twice, the pointer stops short of the right and
  bottom of the screen whenever the picture is scaled: with a 1280×800
  guest on a 1920×1080 screen it reaches about two thirds.
- **Fix: QEMU's SDL OpenGL mode (`gl=on`).** There QEMU scales the picture
  itself (keeping the aspect ratio) and SDL reports window positions, so
  QEMU's conversion is the only one and every part of the guest's screen
  is reachable. With a 4:3 guest on a 16:9 screen the side bars belong to
  the edges, so the guest's cursor moves slightly faster than the hand;
  guests that draw their own cursor (Windows XP, 9x) look right.
- **The guest is offered the screen's resolution**: the display device's
  EDID (`xres`/`yres` on VGA and virtio-vga; VGA memory grows to fit).
  Linux and Windows 7+ guests start at the native resolution and fill the
  screen unscaled. Cirrus (XP/9x profiles) has no EDID; those guests keep
  the resolution set inside them and are scaled.
- **Without a GPU** (basic graphics, or no render node) Mesa renders the
  window in software (`LIBGL_ALWAYS_SOFTWARE=1`): in testing, Mesa's EGL
  crashed QEMU's 2D renderer when the guest changed resolution on such a
  machine.
- `rehome.gl=off` on the kernel command line (edit the boot entry with `e`
  in the boot menu) goes back to the 2D path, should a GPU's OpenGL driver
  misbehave.
- **Tested** on an emulated 1920×1080 computer without a GPU: a guest
  switching to 1024×768 is shown full height, centred, 4:3 (checked by
  `tests/integration/test_appliance.py`), and QEMU survives guest mode
  changes. The pointer mapping follows from QEMU's and sdl2-compat's
  source; measuring it needs a guest that drives the USB tablet, which was
  too slow to boot inside the emulated computer.

## D30. Recognising installer ISOs without libosinfo

Reported: a new VM from an ISO (Omarchy) was not recognised, so it got
generic hardware. libosinfo only knows releases it has a database entry
for, matched by volume label; new releases, derivatives (Omarchy, CachyOS)
and machines without libosinfo's GObject bindings fell through.

- **libosinfo first, then the disc's own files.** `rehome/media/isoscan.py`
  reads the ISO 9660 directory (Joliet and Rock Ridge names, no mounting,
  no root) and looks for the files each installer is known by:
  `TXTSETUP.SIF` (NT 4.0 to Server 2003, edition from the `WIN51xx` tag
  files), `WIN98`/`WIN9X`/`WIN95` folders, `sources/idwbinfo.txt`
  (Vista and later, release from the build branch), the UDF bridge README
  of Windows 8+ discs, `arch/` (archiso), `.disk/info` (Ubuntu, Debian,
  Mint…), `.treeinfo` (Fedora, RHEL), `bin/freebsd-version`, OpenBSD's
  `bsd.rd`, ReactOS and FreeDOS folders, and Linux kernels in the usual
  places. The result says which file it came from (`rehome os-detect`).
- **Derivatives keep their own name** ("Omarchy (based on Arch Linux)") and
  get their base system's profile.
- **The wizard shows what was detected and lets you change it** (an
  "Operating system" row on the hardware page); changing it re-plans the
  hardware. A wrong guess is never a dead end.
- Reading stops at the first volume descriptors and a few small files, so
  it is fast on multi-gigabyte ISOs and safe on files that are not ISOs.

## D31. Where disks are stored; replacing a VM's disk

Requested: choose where new disks go, so big VMs do not fill the system
drive, and switch an existing VM's disk for another one.

- **Default: the VM's folder**, as before. A **default folder** can be set
  in Preferences (`[storage] disk_dir` in `settings.toml`, or
  `rehome disk folder DIR`), and each new disk can get its own folder (New
  VM wizard, Copy import, Add/Replace a disk, Make Standalone, clone;
  `--disk-dir`/`--folder` on the command line, where `''` means the VM's
  folder).
- **Ownership is recorded, not guessed.** Disks Rehome creates outside the
  VM's folder are marked `owned = true` in `vm.toml` and named
  `<vm>-<id>-<disk>.qcow2`. They are deleted with the VM; with "keep the
  disk files" they stay where they are (moving them to the kept-disks
  folder could copy hundreds of gigabytes onto the system drive). Images
  the user attached are never moved or deleted, not even when replacing or
  removing them with "delete the old file" ticked.
- **New files never overwrite.** `qemu-img create` overwrites silently, so
  a new disk's name gets a `-2`, `-3`… suffix when the name is taken.
- **Replacing a disk** keeps its ID, bus, boot position, cache mode and
  serial, so the guest sees it at the same place. It is refused while the
  VM runs, while it has a saved (suspended) state — resuming onto other
  disks would corrupt them — and while it has checkpoints, which belong to
  the current disks. The old file is kept unless the user ticks "delete",
  and is deleted only after the new configuration is saved.
- **Disk changes in the Settings tab apply at once** (like Enlarge…), since
  they create or delete files; other unsaved edits on the page are kept.
- **Attaching never takes a physical disk, or an image in use.** A path
  under `/dev` or a block device is refused with a pointer to Import, which
  reads disks read-only through an overlay (`rehome set --attach-disk` and
  `rehome create --use-disk` previously accepted any path). So is an image
  another disk or VM already uses (two writers corrupt it) and the
  original of a linked import (it is only ever read).
- **Checkpoints and added disks.** Reverting (VM off) to a checkpoint
  taken before a disk was added leaves that disk as it is; it used to fail
  because the new disk lacks the checkpoint.

## D32. Appliance: QEMU's own NAT; built-in USB devices stay; crash reports

Reported from a ThinkPad X1 Carbon: a Windows 11 appliance had no network
although Wi-Fi was connected in the maintenance console; a Windows XP
appliance reached its desktop, then fell back to the maintenance console
after a few seconds.

- **Network.** The kiosk starts the VM within seconds of power-on, before
  the Wi-Fi is associated and has an address. passt reads the host's
  addresses once, at start; without a connection it enters "local mode"
  and hands the guest `169.254.2.1/16` (Windows: no internet) for the
  whole session. Reproduced with the appliance's passt (git 2026-01-20)
  and a disk-less VM in an offline network namespace: iPXE got
  `169.254.2.1`; with QEMU's own NAT (slirp) `10.0.2.15`, gateway
  `10.0.2.2`. slirp gives the guest a fixed private network and opens
  host sockets per connection, so it follows the host's connection as it
  comes and goes. The appliance always uses it (`REHOME_NO_PASST=1`);
  on desktops passt is still used, unless the computer has no default
  route when the VM starts.
- **USB.** The USB agent gave every device to the VM, including a
  laptop's built-in camera, fingerprint reader and Bluetooth. Windows 11
  gets them on xHCI; the XP profile has QEMU's USB 1.1 controller (UHCI),
  whose queueing code asserts (`uhci_queue_fill`) when a passed-through
  device completes a transfer synchronously, which happens when it
  disconnects; Intel Bluetooth chips re-enumerate when a driver resets
  them, which XP does after logon. This is the most likely cause of the
  XP crash (not confirmed: no log yet). Built-in devices (the kernel's
  `removable = fixed` or port `connect_type = hardwired`, from the
  firmware's port descriptions) and Bluetooth adapters now stay with the
  appliance unless included in the maintenance console.
- **Crash reports.** A QEMU crash while the VM ran was explained with
  start-up messages ("QEMU refused to start the VM"). The console now
  says that QEMU quit, quotes its last error line from this run, and lists
  the USB devices the VM had; the USB agent's decisions are in the
  journal (saved with the diagnostics).
- **Covered by** `tests/integration/test_appliance.py`: the test VM uses the
  XP profile's hardware (Cirrus, AC97 through the computer's sound card
  with ALSA, UHCI with a tablet, RTL8139 on NAT), a plugged-in USB stick
  still reaches the VM, QEMU's command line has `-netdev user` and no
  passt runs, and killing QEMU opens the maintenance console with the new
  message.

## D33. Appliance clock, Windows XP resolutions, the screen at power-off

Reported after D32: Windows 11's clock was wrong again after each restart;
Windows XP could not use 1920×1080; while the computer turned off, the
screen briefly showed "Starting <VM>".

- **Clock.** Windows (and DOS) profiles keep the VM's hardware clock in
  local time (`-rtc base=localtime`), which QEMU takes from the host's time
  zone. On a desktop that is the user's zone; the appliance had none (UTC),
  so Windows showed UTC as local time. Its own internet time correction
  lasted only until the VM restarted, since every start is a new QEMU that
  reads the host clock again. Now the appliance gets the build computer's
  time zone (`/etc/localtime`, `--timezone` to override, changeable in the
  maintenance console) and `systemd-timesyncd` sets the clock once online
  (the kernel then keeps the hardware clock in UTC, also fixing laptops
  whose clock was left in local time by Windows).
- **XP resolutions.** XP's built-in Cirrus driver stops at 1280×1024; the
  only other XP drivers for QEMU's cards are QXL's, in the SPICE guest
  tools 0.65 that libosinfo lists for Windows XP (QXL offers up to
  2560×1600 with its default 16 MB of video memory, 1920×1080 included).
  The guest tools step now gives XP/2003 those tools on a disc (current
  virtio-win no longer supports XP; it was offered before), and "switch to
  faster devices" moves XP to `qxl-vga` (checkpoint first). The switch is
  refused when this computer's QEMU lacks QXL (Debian/Ubuntu ship it in
  `qemu-system-modules-spice`, which the appliance now installs) instead of
  silently falling back to plain VGA. Not tested with Windows XP itself:
  the download host is unreachable from the development sandbox; the
  appliance test runs a VM on QXL (VGA and VBE modes).
- **Power-off screen.** The kiosk prints "Starting <VM>" on the text screen
  before the VM's window covers it; when the window closes, that text was
  visible again. The kiosk now replaces it with what happens next
  ("Turning off...", "Restarting <VM>...", "Opening the maintenance
  console...").
- **Drivers in general**: appliances need none (the guest sees the same
  virtual hardware everywhere); documented in APPLIANCE.md "Drivers".

## D34. Guest tools by Windows era, also for systems chosen by hand

Reported: virtio-win 0.1.302 does not install on Windows XP. 0.3.2 gave XP
the SPICE guest tools only when the VM knew its Windows version; a system
chosen by hand in the New VM wizard (a profile such as "Windows XP / Server
2003") had none, and got current virtio-win.

- Rehome decides by version when it has one, and otherwise by the VM's
  hardware profile: XP/2003 → SPICE guest tools 0.65; Vista/7 → SPICE guest
  tools 0.141 (what libosinfo lists for Windows 7; their "faster devices"
  are virtio disks and network with QXL, which current virtio-win's
  installer, Windows 8 and later only, does not cover); 2000/NT → none;
  8 and later → virtio-win.
- New VMs with a hand-chosen system record the profile's lowest version
  (XP: 5.1, Windows 11: build 22000), like a detected system would.
- Not verified against real Windows 7 or Vista; Vista is not in
  libosinfo's list (the 0.141 tools are offered as for 7).

## D35. A normal shutdown reported as a crash

Diagnostics from an ASUS desktop (Windows XP on QXL): the maintenance
console reported a crash, but QEMU had printed no error, the kernel no
segfault, and the session ended cleanly. QEMU sends its SHUTDOWN event
(with the reason: guest shut down, restarted...) and closes its control
socket immediately. The QMP client read the event and then hit the closed
socket in the same call; the error discarded the event, and without a
reason the kiosk assumed a crash. Reproduced with a stand-in socket
(`tests/unit/test_qmp.py`).

- `poll_events` returns the events it read before the socket closed (the
  next call reports the closed connection); the kiosk also reads once more
  after QEMU has gone.
- The session logs why the VM stopped, and "Save diagnostics" now includes
  the console's message and the session's result.
- Confirmed with a real QEMU: FreeDOS powering itself off through ACPI
  (the PM1a control register, as Windows does) left the old client with
  no reason and the new one with `guest-shutdown`. The appliance test now
  ends with the VM powering itself off, and the test computer must turn
  off.

## D36. SYSLINUX without mtools; failures keep their details

Reported from Arch: importing a Windows 98 disk whose boot sector came from
mkfs.fat ("This is not a bootable disk") failed at "Make the disk bootable:
SYSLINUX could not be installed". The GUI and the patch log kept only that
sentence: the engine dropped the error's fix and libguestfs' message.

- libguestfs runs `syslinux` inside its helper VM, built from the host's
  packages, and that `syslinux` copies files with mtools. Arch's syslinux
  package does not depend on mtools; without it the helper VM fails with
  "mcopy: not found / failed to create ldlinux.sys" (reproduced by
  removing mtools inside the helper VM). The likeliest cause; not
  confirmed, as the details were lost.
- Fallback: `extlinux` on the mounted partition does the same through the
  kernel's FAT driver, without mtools. Tested end to end (FAT32, copy
  import, both libguestfs backends): the VM boots. Both `syslinux.cfg` and
  `extlinux.conf` are written.
- Failed fixes now keep the fix text (shown in the GUI) and the technical
  details (patch log, `rehome patch log VM`).
- SYSLINUX is a listed component (x86 only, so recommended rather than
  required in the deb/rpm packages, optional on Arch), and `rehome doctor`
  checks for it and for mtools.
- Windows 98's own IO.SYS through chain.c32 `msdos7=` has not been tested
  here (no Windows 98 image); FreeDOS is.
- Follow-up (0.3.6): on the same Arch computer, with syslinux and mtools
  installed, both libguestfs methods still failed, which fits a helper VM
  built without the SYSLINUX package at all. Third method: this computer's
  own `syslinux` (the mtools-based installer, no root) on a downloaded copy
  of the partition in the VM's folder, writing back only the 64 KiB blocks
  it changed. Tested end to end with both libguestfs methods failing (both
  backends): the VM boots, the source is unchanged, the copy is removed.

## D37. Rehome's own boot sector for Windows 95/98

Reported with 0.3.6: the Windows 98 VM reached SYSLINUX ("BPB detection
(sect): 7.0", "Booting...") and stopped there.

- Cause, reproduced with a stand-in IO.SYS whose loader reports how it was
  started: chain.c32's `msdos7=` mode, marked "TODO/TEST" in SYSLINUX's own
  documentation, starts IO.SYS's loader (MSLOAD, at 0070:0200) with SS =
  0070h and BP = 7C00h. SS:BP then points into IO.SYS, not at the boot
  sector: the "bytes per sector" read there is 0C89h instead of 0200h.
  MSLOAD reads the BPB through SS:BP, so it cannot load the rest of IO.SYS.
- Rehome cannot ship Microsoft's boot sector, so it writes its own
  (`rehome/patch/win9x_boot.asm`, FAT16 and FAT32, GPL). It does what
  grub4dos's `chainloader /io.sys` does, the long-standing way to start
  Windows 98 without Microsoft's boot code:
  - it skips MSLOAD (the first 2 KiB of IO.SYS),
  - loads the rest to 0070:0000,
  - starts it with DL = drive, DH = F8h, AX = BX = 0, BP = 7C00h,
    DS = ES = SS = 0 and SP = 400h.

  The partition's own BPB is kept. The OEM name becomes MSWIN4.1, the name
  Microsoft recommends for compatibility. On FAT32 the backup boot sector
  is written too.
- Tested in QEMU with a stand-in IO.SYS: 222 KiB, scattered over 20+
  fragments. Cases:
  - FAT32 and FAT16;
  - a partition starting near 128 GiB;
  - no MBR code, no active partition, type 0x83, hidden sectors 0.

  It receives exactly those registers and every byte in order. Not tested
  with a real Windows 98 IO.SYS (none here); the handover is grub4dos's.
- Before writing, Rehome checks that IO.SYS has MS-DOS 7's layout ("MZ",
  "BJ" at 200h, "MS" at 7FEh) and is at most 514 KiB. Windows Me's IO.SYS is
  compressed, and only MSLOAD can unpack it. For Me Rehome says so and
  changes nothing: start a Windows Me startup disk and run `SYS C:`.
- The repair now also fixes:
  - the BPB's hidden sectors (mkfs.fat writes 0 on an image file; boot code
    finds files with it);
  - a partition type DOS does not know (fdisk's 0x83);
  - a CHS type on a partition that reaches past 8 GB (0x0B → 0x0C,
    0x06 → 0x0E; not for MS-DOS 6, which has no LBA types);
  - an MBR without code, which gets Rehome's (`mbr.asm`) instead of
    SYSLINUX's.
- VMs repaired by 0.3.5/0.3.6 are recognised by their `syslinux.cfg`
  ("Written by Rehome", `msdos7=`). Running the fix again replaces SYSLINUX
  and removes its files.
- SYSLINUX stays for FreeDOS, MS-DOS 6 and PC DOS (`freedos=` is tested).
- Reads use the BIOS's INT 13h extensions, which every BIOS since the late
  1990s and SeaBIOS have.
- `scripts/gen_boot_code.py` assembles the sources into
  `rehome/patch/bootcode.py`, so running Rehome needs no assembler. A unit
  test checks the stored bytes match the sources.
- New warning `win9x.big_disk`: Windows 9x's IDE driver (ESDI_506.PDR) uses
  28-bit LBA. A partition ending past 128 GiB gets a warning, because
  Windows' writes beyond that point land at the start of the disk.
  The reported disk is 931.5 GiB.
- 86Box was suggested as an alternative. It emulates period hardware closely,
  but the disk would still need a boot sector that can start IO.SYS, and 86Box
  needs BIOS ROM files obtained separately. Not integrated.

## D38. Check Windows' drivers before switching to virtio

Reported: Windows 7 (32-bit) blue-screened after the SPICE guest tools were
installed and "Switch to faster devices" was applied.

- Cause: the switch only changed the VM's settings (disks virtio, network
  virtio, display QXL). Guest tools installers stage drivers in the driver
  store. Windows installs a staged driver only for a device that is present,
  and the VM had no virtio disk yet. So `viostor` was not set to load at
  boot, and Windows could not reach its own disk: 0x7B
  INACCESSIBLE_BOOT_DEVICE, the classic failure.
- Now, before switching, Rehome opens the VM's disk offline (libguestfs):
  - Disk: it copies `viostor.sys` from the driver store to
    `System32\drivers` (unless it is already installed) and makes the
    `viostor` service boot-start. On Vista/7 it also adds
    CriticalDeviceDatabase entries for the virtio disk's PCI IDs (1AF4:1001
    transitional, as in Rehome's VMs, and 1AF4:1042). This is what virt-v2v
    does. When Windows finds the disk, it finishes installing the driver
    from the store.
  - Network: it switches only if `netkvm` is installed or staged.
  - If Windows has neither driver for a device, that device keeps its
    model and the user is told why. If nothing is left to switch, the
    checkpoint is removed again.
  - The registry changes go to `logs/patch.log`.
- Reverting to the "Before switching to virtio" checkpoint now also puts
  back the devices. Before, it reverted only the disk contents, so the VM
  stayed on virtio and failed the same way. The devices are saved with the
  checkpoint. Checkpoints from older versions fall back to the profile's
  devices for what the switch changes.
- Windows 8 and later have no CriticalDeviceDatabase. There, Rehome makes
  the driver boot-start and relies on the driver store's database (which
  staging fills in) for the device match. Untested with real Windows 8+.
- Tested on a synthetic 32-bit Windows 7 disk (registry, files and the
  device settings, plus revert). It cannot be booted here: there is no
  Windows 7 to test with, so the boot itself is unverified. The QXL display
  driver is not checked; without it, Windows uses its standard VGA driver
  on QXL.

## D39. Relative (PS/2) mouse in the console

Reported: on Windows 98, the mouse did not move across the screen.

- The bug: the console read QEMU's `IsAbsolute` property with
  `bool(v.unpack()) if v else True`. A GLib.Variant holding `false` is
  itself falsy, so every PS/2-only guest (DOS, Windows 9x) counted as
  absolute. The console sent `SetAbsPosition`, which QEMU refuses for a
  relative mouse. Clicks worked, but the cursor never moved. Fixed
  (`is not None`); the test VM is now seen as relative.
- Capturing the pointer had two more problems:
  - Windows 3.x/9x accelerate any move of more than 6 counts per packet,
    so the guest cursor ran ahead of the host pointer.
  - GTK 4 can neither lock nor warp the host pointer, so movement stopped
    at the window edge with the guest cursor short of its own edge.
- `rehome/ui/console/relmouse.py`:
  - Moves are sent in steps of at most 6 counts.
  - On capture, the guest cursor goes to the corner and then to where you
    clicked.
  - Reaching an edge of the guest image, or leaving the view, pushes the
    guest cursor to that edge, which lines the two up again.
  - QEMU's PS/2 queue holds 16 bytes (5 packets). What doesn't fit is added
    to the next packet, which then gets accelerated. A push that doesn't fit
    cancels the moves after it. So everything is queued, buttons included,
    and sent 3 packets every 3 ms, with an empty move at the end to flush
    QEMU. 4 per 2 ms arrived 1:1 from a plain test, but merged now and then
    with the GTK window and the emulated guest competing for the CPU.
  - A backlog above 64 packets (a fast flick) is merged into bigger packets
    rather than lagging. Merging never crosses a push or a button.
- Tested against a real QEMU (TCG) with a boot-sector guest
  (`tests/fixtures/ps2mouse.asm`). It reads the mouse through the BIOS,
  applies Windows' acceleration rule, and prints its cursor position.
  - The old way moved 100 host pixels as 200.
  - The new way moves them 1:1, aligns on capture, and lines up at every
    edge.
  - The console widget itself was driven under Xvfb.
- Not used: VBADOS (GPL, VMware "vmmouse" absolute mouse) and vmwmouse are
  Windows 3.x drivers, and neither is confirmed for Windows 9x. The USB
  tablet confines Windows 98's pointer to the upper-left quarter, as
  VirtualBox documents.

## D40. Windows 95/98 on fast computers: patcher9x's CPU speed fix

Reported: a Windows 98 VM worked in the desktop app. Built into an appliance
and started on another computer, it stopped with "While initializing device
NDIS: Windows protection error".

- Cause: NDIS.VXD calibrates a delay loop. It runs 1,048,576 LOOP
  instructions, reads the time with Get_System_Time (1 ms resolution) and
  divides by the elapsed milliseconds. A CPU that finishes within one
  millisecond makes that a division by zero (Microsoft KB 312108; OS/2
  Museum).
  - QEMU 7.2 and later emulate LOOP fast enough for this on a fast computer
    (noted by patcher9x). So emulation, which Rehome uses for Windows 9x,
    avoids the bug only on slower computers. Which computer runs the VM
    decides.
  - Windows 98 SE guards the division; 95 and 98 first edition do not. The
    reported system is 98 first edition (kernel32 4.10.1998).
- New patch `win9x.cpu_speed` (95/98 profiles), a port of
  [patcher9x](https://github.com/JHRobotics/patcher9x) (MIT; its notice is in
  `win9x_speed_data.py`):
  - The loop runs 100 times longer (drivers: 8 times), the result is scaled
    to match, and a zero time counts as 1 ms.
  - Patterns: NDIS v1–v4 and driver v1–v8.
  - Files: `SYSTEM\NDIS.VXD`, `SYSTEM\VMM32\NDIS.VXD`,
    `SYSTEM\IOSUBSYS\ESDI_506.PDR` and `SCSIPORT.PDR`, plus `IOS.VXD` and
    `NTKERN.VXD` when they exist as separate files in `SYSTEM\VMM32`.
  - The code is found by its bytes, skipping those patcher9x skips:
    encoding variants such as `29 D0`/`2B C2`, and relocated addresses,
    which stay untouched.
- Not done: IOS and NTKERN inside `VMM32.VXD`. They are compressed (W3/W4),
  and patcher9x unpacks them; they passed on the reported computer. The
  TLB invalidation bug fix (also in patcher9x) is not ported either; it
  concerns KVM on new CPUs, while Rehome emulates Windows 9x.
- Alternative considered: `-icount shift=N` makes the emulated CPU's speed
  fixed. Windows 9x never idles the CPU, so on a computer slower than the
  fixed speed the guest's clock and sound would run slow. Also, `-icount`
  is now added only under emulation, since QEMU refuses it with KVM.
- Tested: unit tests for the search and replace, on every pattern with
  random relocated bytes. An import of a Windows 98 FE disk carrying such
  an NDIS.VXD and ESDI_506.PDR patches both in the overlay; the source is
  unchanged. Not run with Microsoft's real NDIS.VXD (none here).

## D41. Appliance: mice go straight to VMs that only have a PS/2 mouse

Reported: in a Windows 98 appliance, the mouse "hits invisible walls" and
has to be moved around a lot to get past them.

- A PS/2 mouse moves the guest's own cursor by deltas. QEMU's SDL window
  asks SDL for relative mode, which on Wayland needs the compositor's
  relative-pointer and pointer-constraints protocols. cage has neither in
  Ubuntu 26.04 (0.2.1). 0.3.1 adds relative-pointer only. So the hidden
  pointer stopped at the screen's edges while Windows' cursor, accelerated
  by Windows, was somewhere else.
- In the appliance the VM has the whole screen, so for VMs whose pointer is
  `ps2` (DOS, Windows 3.x/9x profiles), a mouse agent in the session gives
  every mouse to QEMU with `object-add input-linux`:
  - A mouse is an event device with REL_X, REL_Y and BTN_LEFT.
  - Mice plugged in later are added; unplugged ones are removed with
    `object-del`.
  - input-linux grabs the device (EVIOCGRAB) and sends its raw movement
    to the guest's PS/2 mouse, so cage and SDL no longer see it. QEMU's
    full-screen SDL window already hides the cursor.
  - The `rehome` user is in the `input` group.
- Touchpads (absolute devices) still go through cage and SDL. VMs with a
  tablet (Windows XP and later, Linux) keep the absolute path, which works.
- Tested: unit tests (mouse detection from sysfs, add/remove/no retry after
  a failure). In the appliance (nested, emulated), the agent gave the outer
  machine's PS/2 mouse ("ImExPS/2 Generic Explorer Mouse") to a FreeDOS VM
  switched to a PS/2-only mouse, and moving that mouse through QMP moved
  the cursor of a DOS program (`tests/fixtures/mousecom.asm`, Windows'
  acceleration rule) from the middle of the screen to its right, bottom,
  left and top edges with no walls on the way
  (`test_a_vm_with_only_a_ps2_mouse_gets_the_computers_mouse`, passed).

## D42. Appliance: a hidden way into a fuller maintenance console

Asked for: the start screen should not tell users they can press M for
the maintenance console (admins know). The console should do more.

- **Start screen.** "Starting <VM>…" stays in the top-left corner for the
  same few seconds (plus notes when something limits the VM). M still
  opens the console, but nothing says so. Any other key just starts the VM
  sooner. The wait is a console setting (0–60 s); with 0 the boot menu
  (Esc) is the way in.
- **Main menu.** Sixteen numbered pages plus R (restart) and P (power off),
  in two columns, so header and menu fit the 80×25 text screen of the
  basic-graphics boot. "Restart with basic graphics" moved to **Screen**.
- **Backups.** A backup is a `rehome export` file (`.rehome`), so Rehome on
  a computer can import it and any export can be restored.
  - It goes to a "Rehome backups" folder on a USB drive. Its size is
    measured first (`qemu-img measure`: the disks without checkpoints).
  - FAT32's 4 GB file limit is checked before starting.
  - The disks are converted on the appliance's data partition when it
    has room, so the USB drive needs space for the backup only once
    (`export(work_dir=)`).
  - The backup and restore run as root in the console process; the
    restored VM is then given to the `rehome` user.
- **Restore.** The backup's VM is imported, fitted for the appliance (the
  same `fit_vm` the build uses), and saved.
  - Only then does `/etc/rehome/appliance.toml` point at it and the old
    VM get deleted. A damaged backup or a full drive leaves the old VM
    in place.
  - CD/DVD images are not in backups; images the old VM used from its
    media folder are copied over.
  - The restored VM takes the backup's name.
  - On a computer, importing a backup made in an appliance switches its
    display from the appliance's full-screen window to Rehome's console.
- **CD/DVD images.** `.iso` files from a USB drive (its top two folder
  levels) are copied into the VM's media folder and put in its CD drive
  (one is added if it has none).
  - This is how files reach Windows 95/98, which cannot read USB sticks.
  - Taking the disc out offers to delete the copy.
- **Startup and shutdown** (settings.toml on the data partition, read by
  the kiosk):
  - At power-on: the VM, or the console. `boot()` redirects and clears
    GRUB's failure flag, as the console's boot entry does.
  - When the VM shuts itself down: power off (default), start it again
    (with the same "Starting …" wait, so M still works), or the console
    with "<VM> was shut down."
  - The power button always turns the computer off. The session marks a
    shutdown the button asked for (`power_button`), so "start again"
    cannot trap the computer on.
- **Screen.**
  - "Always basic graphics" is a GRUB environment variable
    (`rehome_default=vm-basic`) that the boot menu reads before
    `next_entry`.
  - OpenGL drawing is a setting. `rehome.gl=on|off` on the kernel
    command line still wins for one boot.
- **Date and time.**
  - Set by hand for computers that are never online. This turns internet
    time off: timedated refuses otherwise. `set-time` is retried while
    timesyncd stops.
  - The time zone page moved here from the network page.
- **Sound.** `alsamixer`, then `alsactl store`; ALSA's own service restores
  the state at boot. A test tone comes from `speaker-test`.
- **Maintenance password.** Set, change or remove it from the console.
  - It uses the build's mechanism (`chpasswd`, a unit drop-in running a
    login prompt, root's `.profile` opening the console). The texts are
    shared from `runtime` so both write the same files.
  - It takes effect the next time the console opens.
- **Storage, logs.** Free space, the VM's disks/images/checkpoints. The
  journal of this and the previous start, and the VM's QEMU log, in `less`.
- Tested:
  - Unit tests: the start screen's text, M, the wait, the shutdown
    choices and the power button. The settings' checks, the boot
    redirect and GRUB's order. The menu fitting 25 lines and the pages
    with typed answers.
  - With real files and `qemu-img`: a backup and restore round trip
    (disk contents, media, config, the old VM deleted), the FAT32 and
    free-space checks, a damaged backup leaving the VM alone, finding
    and copying ISO images, the password files.
  - The first emulated run found a bug in 0.3.12. The restore rewrote
    `/etc/rehome/appliance.toml` as root through `toml.dump_atomic`,
    whose temp file is private (0600), so the kiosk (user `rehome`) could
    not read it and the restored VM did not start. The password page
    writes the same file. Atomic rewrites now keep the replaced file's
    permissions (0.3.13), and the test checks the file as `rehome`.
  - In the emulated appliance (`test_maintenance_console`, passed with
    0.3.13):
    - M on the start screen (read by OCR) opens the console.
    - A backup to a FAT32 USB stick image, a CD image from it, and a
      restore whose VM then starts.
    - "When the VM shuts down: open the console".
    - Basic graphics, console at power-on, the volume and the password,
      checked after a restart.

## D43. Windows 9x VMs get a VGA BIOS without debug output

Reported: Windows 98 (in Rehome, and in an appliance with KVM) shows "A fatal
exception 0D has occurred at C000:00002007" when it shuts down.

- C000 is the VGA BIOS. At offset 2007h, SeaVGABIOS 1.17.0 as Ubuntu 26.04
  ships it (also in the appliance) has `mov [ecx],dl` with a 32-bit
  address, in `putuint`. That function writes the digits of a number for a
  debug message.
- Why it is reached:
  - SeaVGABIOS answers video functions it does not implement with
    `debug_stub()`, which prints "stub <function>:<line>".
  - Once a video mode was set other than through the legacy INT 10h call
    (as Windows' display driver does), SeaVGABIOS stops switching to its
    own "extra stack" ("Disable extra stack if it appears a modern OS is
    in use").
  - The debug path is deep. On a caller's stack with little room left,
    the C code's 32-bit stack arithmetic goes below zero or past 64 KB.
    The digit store then leaves the 64 KB stack segment: exception 0D in
    the V86 mode Windows calls the BIOS in.
  - Under emulation (TCG) segment limits are not checked, so the same
    writes silently land outside the segment.
- Reproduced (`tests/fixtures/vgastub.asm`): with the extra stack off, INT
  10h AX=1200h BL=36h (a stub) on stacks with 0400h–0040h bytes left.
  SeaVGABIOS 1.16.3 (Ubuntu 24.04) and 1.17.0 (Ubuntu 26.04), Cirrus and
  standard VGA, write outside the caller's stack segment. The last call
  also loses AL.
- Fix: Rehome ships SeaVGABIOS 1.17.0 built with QEMU's own configuration
  and `CONFIG_DEBUG_LEVEL=0` (`scripts/build_vgabios.py`; `data/firmware/`,
  with the source and license notes). `qemu_cmd` passes it as `romfile=`
  for `cirrus-vga` and `VGA` in VMs whose OS family is win9x, existing
  VMs included. The stub then only returns. Nothing else in the VGA BIOS
  changes, and other VMs keep QEMU's.
- Rejected:
  - Patching the distribution's ROM: the bytes differ per build.
  - The old LGPL VGABios: no longer packaged by most distributions, and
    different in more ways.
  - Asking for each distribution's ROM: the appliance's copy and SeaBIOS's
    source were enough to find the cause.
- License: SeaBIOS is LGPL-3.0-only. The packages declare it next to
  Rehome's GPL-3.0-or-later.
- Tested:
  - `test_vgabios.py`: the stock ROMs fail as above. Rehome's write nothing
    outside the stack and return AL=12h. A Windows 98-profile VM started
    through Rehome gets the ROM, with the same result.
  - Windows 98 itself shutting down could not be run here. On the
    reporter's VM (0.3.14) the exception is gone, but the shutdown now
    ends at a black screen with a blinking cursor, and the VM stays on.
    SeaBIOS's APM power-off writes the same ACPI register as
    `POWEROFF.COM`, which turns QEMU off. So Windows does not get as far
    as asking the BIOS, or does not use APM.
  - To see where it stops, `rehome diagnostics` now reports a running VM's
    processor, sampled twice a second apart: its mode (real, V86,
    protected), CS:EIP, halted or not, and interrupts on or off, plus
    QEMU's register dump. On the test VM it reports the program's final
    `cli; hlt` as halted, interrupts off.

## D44. Windows 9x VMs: the VGA BIOS's extra stack stays off once Windows runs

Reported after D43 (0.3.15, KVM): Windows 98 no longer shows exception 0D
when it shuts down, but stops at a black screen with a blinking cursor, and
the VM stays on.

- The new diagnostics (D43) showed the processor in V86 mode inside the VGA
  BIOS, interrupts off, at C000:1E56 and C000:1A22.
  - Those are SeaVGABIOS's scroll and teletype code: printing and
    scrolling over and over.
  - SS=DS=ED6C with EBP=01C8 is SeaVGABIOS's own 512-byte "extra stack"
    ("VGA stack allocated at ..." in the E-segment, from the system
    BIOS's PMM). ESP=35DE lies far outside it: the stack had been
    trampled.
- How SeaVGABIOS uses that stack: it is for callers with small stacks (DOS
  programs).
  - Every INT 10h call switches to the same block, with no protection
    against a second call while one is running.
  - A mode set made the old way (INT 10h AH=00h) turns it on.
  - Any other mode set (VESA, as Windows' display driver does) turns it
    off: "Disable extra stack if it appears a modern OS is in use".
  - When Windows 98 shuts down, it sets text mode the old way. That turns
    the stack back on while Windows still runs several virtual machines,
    any of which can be inside the VGA BIOS when Windows switches between
    them. The calls then share one stack.
- 0.3.16 built the ROMs without the extra stack (`CONFIG_VGA_ALLOCATE_
  EXTRA_STACK` off). That gives up what the stack is for: DOS calls INT
  10h on small stacks, and under KVM the processor checks real-mode
  segment limits, so a deep call on such a stack faults half-way.
  Emulation (TCG) checks no limits, so the tests here cannot show that.
  - On the reporter's VM the DOS part of the boot then stayed on screen
    with every other character missing. That was first taken for this.
  - The next diagnostics (0.3.17, which has the stack again) showed the
    same screen with Windows' kernel looping in protected mode
    (0028:C0009809, CR2=EAX=F000F855). So the boot hung after the DOS
    part, and the garbled text is more likely the display left half-way
    between DOS and Windows' display driver.
  - The likely cause is the "Advanced Power Management support" device
    the reporter had just added (Safe Mode, which does not load it,
    starts). That is to be confirmed by removing it.
- 0.3.17: the extra stack again, with a two-change patch
  (`data/firmware/seavgabios-extra-stack.patch`). The stack is turned on
  when the VGA BIOS starts, and a mode set only ever turns it off. So
  DOS, and Windows while it boots, keep it, and once Windows' display
  driver has set a mode it stays off until the next boot (also for DOS
  boxes in Windows).
- Tested:
  - `tests/fixtures/vgastub.asm` reports the flag after a DOS-style mode
    set, a VESA one and a shutdown-style one: QEMU's 1.16.3 "101", 0.3.14
    "101", 0.3.16 "000", 0.3.17 "100". It still writes nothing outside
    the caller's stack, for cirrus and standard VGA.
  - FreeDOS as a Windows 98-profile VM: boots, switches to VESA and back,
    and scrolls, checked on QEMU's screenshot as well as in video memory.
  - Windows 98 itself: to be confirmed on the reporter's VM.
- Power-off: the reporter's Windows 98 has no "Advanced Power Management
  support" device, so it never asks the BIOS to turn the VM off.
  SeaBIOS's APM power-off writes the same ACPI register as `POWEROFF.COM`,
  which QEMU turns into a power-off. The device can be added in Windows
  (Add New Hardware → System devices).

## D45. Windows 9x VMs: turned off once Windows has finished shutting down

Reported after D44: with "Advanced Power Management support" removed again,
the reporter's Windows 98 boots normally, but shutting down ends at "It's
now safe to turn off your computer" and the VM stays on.

- Windows 95/98/ME turn the computer off only through their APM driver.
  - Without it they leave the processor stopped and show that screen.
  - With it, the reporter's Windows 98 hung while booting under KVM (D44).
  - So Rehome cannot count on it.
- 0.3.18 watches the processor instead (`rehome/vm/watch.py`).
  - A small helper process starts with every Windows 9x VM on a pc or
    q35 machine (a runner "sidecar", like swtpm). It has its own QMP
    socket.
  - Every 2 seconds it asks QEMU whether the VM runs and, if so, where
    the processor is (`info registers`).
  - "Stopped for good" means three samples in a row in real mode with
    interrupts off, and either halted or on the same instruction. Only
    a reset can change that.
  - Then it writes SLP_EN to PM1a_CNT (`o /w 0x604 0x2000`), which is
    what SeaBIOS's APM power-off and `POWEROFF.COM` do. QEMU's PIIX4
    and ICH9 power management turn that into a shutdown by the guest.
    So the runner, the GUI and the appliance (its "when the VM shuts
    down" choice) see what they see when any guest turns itself off.
    `quit` is the fallback if the port write fails.
- What it leaves alone:
  - Windows running: protected or V86 mode, which also covers its error
    screens.
  - A DOS prompt, including "Restart in MS-DOS mode": interrupts on.
  - A paused VM, or one being saved: its samples start again when it
    runs.
  - A DOS program that spins on one instruction with interrupts off for
    over 4 seconds would be turned off. Such a program has hung for
    good anyway.
- What Windows 98 leaves at that screen is not confirmed yet. If the rule
  does not fire on the reporter's VM, the logs show what to change:
  - `logs/watch.log` (in the diagnostics) records each entry into and out
    of real mode, a few places while interrupts are off, and how it ended.
  - The diagnostics also take two live samples.
- The log is bounded. It does not record every sample, because in a
  running Windows the place differs every time, and an appliance can run
  for months from a USB stick. It writes at most 200 changes per run, then
  only the ending. Across runs it is rotated at 2 MiB like `qemu.log`; the
  runner now does that for every sidecar's log.
- Tested:
  - Unit tests with a fake QEMU cover the rule, a paused VM and the
    log's size.
  - `tests/integration/test_watch.py` runs Windows 98-profile VMs (TCG)
    whose boot sector prints and then stops as above, with `hlt` and with
    `jmp $`. Each VM turns itself off within seconds, and the serial log
    shows that the guest ran.
  - FreeDOS with the Windows 98 profile stays on at its prompt.
  - The port write was also checked on a q35 machine: the shutdown
    reason is `guest-shutdown`.
- Reported after 0.3.18: the reporter's Windows 98 still stops at a black
  screen with a blinking cursor, and the VM stays on.
  - It is First Edition (the NDIS patch says so), so the Windows 98 SE
    Shutdown Supplement does not apply.
  - The 0.3.15 diagnostics of that screen showed V86 mode inside the VGA
    BIOS's teletype and scroll code, with interrupts off. CR2 was BF000,
    in text memory.
  - The rule above rightly leaves that alone: it is a hang before the end
    of the shutdown, not the end.
  - 0.3.19's diagnostics also record the BIOS's video variables
    (0040:0049, 0040:0084), the code at CS:EIP and the stack, whose return
    addresses name whatever keeps printing. They are read through the
    guest's own page tables (QEMU's `x`).

## D46. Appliance: touchpads go through libinput to VMs that only have a PS/2 mouse

Reported after D41: in a Windows 98 appliance on a laptop, the touchpad still
"gets stuck a lot". With a mouse it is better.

- D41 gave mice straight to QEMU (`input-linux`), but left touchpads with
  cage and QEMU's SDL window. That is the path that hits walls: cage cannot
  lock the pointer, so the hidden pointer stops at the screen's edges while
  Windows' accelerated cursor is elsewhere.
- `input-linux` cannot take a touchpad. A touchpad reports where the finger
  is, and QEMU would pass that on as positions.
- 0.3.19: the appliance reads touchpads itself, with libinput
  (`rehome/appliance/touchpad.py`).
  - libinput is what Linux desktops use for tapping, the click areas of
    clickpads, two-finger scrolling, palm detection and ignoring the
    touchpad while typing. The appliance has it already: cage needs it,
    through wlroots. It is used through ctypes (path backend).
  - Touchpads are the event devices udev marks `ID_INPUT_TOUCHPAD`. They are
    grabbed (`EVIOCGRAB`), so cage no longer moves its pointer with them.
    Keyboards join the libinput context ungrabbed, so "disable while
    typing" works with a laptop's own keyboard.
  - libinput's accelerated movement, its buttons and two-finger scrolling
    (one wheel click per 15 units) go to the VM's PS/2 mouse with QMP
    `input-send-event`. QEMU makes one PS/2 packet per command, so each
    button change gets its own command; a press and release in one would
    be lost.
  - Commands go over a QMP socket of their own (`qmp-input.sock`, only for
    kiosk VMs whose pointer is `ps2`). The other sockets are shared with
    short-lived users such as the USB agent, and a held connection would
    block them.
  - The movement is accelerated by libinput and again by Windows, like a
    mouse's raw movement is by Windows. No touchpad was at hand to tune
    against, so the maintenance console's "Screen and touchpad" page sets
    the speed (libinput's -0.5, 0 or 0.5) and turns tap to click on (the
    default) or off. Windows' own mouse settings change the speed too.
  - libinput's own touchpad units are not used: they differ between
    touchpads.
- VMs with a tablet (Windows XP and later, Linux) keep the touchpad on
  cage's absolute pointer, which has no walls.
- Not done: an absolute pointer for Windows 9x. That would need a
  third-party driver inside Windows (VBADOS's VBMOUSE.DRV, made for
  Windows 3.x). Under 9x it works only with workarounds, with reports of
  "insufficient memory" errors.
- Tested:
  - Unit tests: the settings, finding touchpads and keyboards from sysfs
    and udev's data, the conversion to PS/2 events (fractions kept, buttons
    one per batch, wheel clicks) and the agent against a scripted
    libinput.
  - The ctypes calls against libinput 1.25.
  - `test_a_vm_with_only_a_ps2_mouse_gets_the_computers_mouse` (passed,
    42 min without KVM): the emulated appliance has no touchpad, so the
    test makes one with uinput (`tests/fixtures/uinput_touchpad.py`, real
    multi-touch events). Through the appliance's libinput 1.31 and the
    agent, its swipes moved a DOS program's Windows-like cursor
    (`tests/fixtures/mousecom.asm`) to every edge of the screen. A tap
    counted as a left click, and a two-finger tap as a right click.
  - Not tested: a real laptop's touchpad, and the speed it gives in
    Windows 98.

## D47. Windows 9x VMs: a VGA BIOS without its extra stack

The diagnostics of D45 (0.3.19), taken at the reporter's black shutdown
screen, showed what had gone wrong:

- The code at CS:EIP (C000:0F90) was `20 07 20 07 ...`: blank text cells
  (a space in grey on black). The VGA BIOS's code had been overwritten.
  - The processor ran through those cells as instructions (`and [bx],al`)
    and wrapped around the segment forever, in V86 mode with interrupts
    off.
  - 0.3.15's "teletype and scroll" addresses (D44) were the same thing,
    read as code that was no longer there.
- The registers were the same as in 0.3.15's report, except ESP and EIP.
  - SS=DS=ED6C, EBP=01C8: SeaVGABIOS's extra stack in the E segment.
  - EDI=8000: the end of a 32 KB text clear.
  - CR2=BF000: the last page of text memory.
- The BIOS's video variables said mode 13h, Windows' boot logo. At shutdown,
  Windows uses the BIOS data area as it was when Windows started, and in
  that copy the extra stack's flag (BDA 40:B9) is on. That defeats 0.3.17's
  patch, which only kept the flag off in the copy where Windows' display
  driver had set a mode.
- SeaBIOS's history says the same:
  - The extra stack (2013) is "for old DOS programs that call the VGABIOS
    and expect it to work with very small amounts of stack space".
  - A 2015 commit turns it off after a VBE mode set made outside V86 mode,
    as "an ugly hack to work around a problem Windows Vista (and possibly
    later Windows releases) has with the VGA BIOS using a stack in the
    e-segment".
  - Windows 9x makes all its mode sets in V86 mode, so QEMU's own VGA BIOS
    never turns the stack off for it. Windows 9x maps upper memory for each
    virtual machine and switches between machines, so one shared stack
    there cannot be safe.
- 0.3.20 builds the Windows 9x ROMs without the extra stack
  (`CONFIG_VGA_ALLOCATE_EXTRA_STACK` off, unchanged source; the patch is
  gone). Every call runs on its caller's stack, as SeaVGABIOS did before
  2013, and as each of Windows' virtual machines has its own.
  - The entry code zero-extends SP and turns interrupts off, so a caller's
    32-bit ESP does not matter.
  - Left: a DOS program calling INT 10h with almost no stack left could
    fault under KVM, which checks real-mode segment limits. That is the
    case the extra stack was for, and no such program is known in Windows
    98's use.
- 0.3.16 was the same idea. It looked worse at the time only because of
  the "Advanced Power Management support" device, which hung the boot
  (D44).
- Tested (TCG; this build environment has no KVM):
  - `tests/fixtures/vgastub.asm`: no extra stack after any mode set
    ("000"), nothing written outside the caller's stack, cirrus and
    standard VGA.
  - A Windows 98-profile VM gets the ROM and is turned off by D45's
    watcher.
  - FreeDOS with the Windows 98 profile boots, switches to VESA mode 117h
    and back, and prints and scrolls a full screen of `dir` output with no
    character missing.
  - Windows 98 itself: to be confirmed on the reporter's VM.
