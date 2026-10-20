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
    is what the overlay header names.
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
- **Fallback** (QEMU built without the D-Bus display module): "Open in
  external viewer" uses SPICE over a Unix socket with `remote-viewer`.

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
  `README.md` → "Testing" lists exactly what was verified and what must be
  re-run on a KVM host.
