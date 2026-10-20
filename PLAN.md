# Rehome — build plan

Rehome turns an installed operating system (on a physical disk or in a disk
image) into a bootable QEMU/KVM virtual machine, and manages VMs the way
Hyper-V Manager does. See `docs/DECISIONS.md` for the reasoning behind the
stack.

Each milestone ends with a working, tested, committed step.

## M0 — Skeleton
- Repository layout, license, PLAN/DECISIONS, dev environment notes.

## M1 — Host detection and `rehome doctor`
- CPU arch/vendor/flags, VT-x/AMD-V, `/dev/kvm` presence and access, nested virt.
- RAM, cores/threads → VM default sizing and over-allocation guard.
- Distro (`/etc/os-release`), package manager, desktop, Wayland/X11.
- QEMU binaries + version + capabilities, firmware via
  `/usr/share/qemu/firmware/*.json` descriptors, swtpm, libguestfs, virt-v2v,
  nbdkit, GPU + virgl/venus usability.
- Every problem → plain-English fix + copy-paste install command.
- Accelerator/machine/firmware selection (KVM vs TCG with warning).

## M2 — VM runner and create-from-scratch
- VM config model (TOML on disk), VM library under XDG dirs.
- QEMU command-line builder (`-blockdev` graphs, pflash, swtpm, virtiofs,
  USB, NICs, display, audio), driven by data profiles.
- QMP client (sync + fd passing), runner with sidecars, live state discovery.
- Start / shutdown / force off / pause / resume / reset / save state.
- Checkpoints (qcow2 internal snapshots, tracked as a tree), clone, export,
  import, delete (keep disks option).
- New VM from ISO with libosinfo detection; Quick Create catalog of free OSes
  from official sources (checksummed); unattended install where feasible.
- Headless boot tests (FreeDOS, Linux) verified with QMP screenshots.

## M3 — GUI shell (GTK4 + libadwaita)
- VM list with live status + thumbnails and actions.
- Embedded console over QEMU's D-Bus display: keyboard/mouse, clipboard,
  dynamic resolution, USB redirection, fullscreen.
- New VM wizard, hardware settings editor, checkpoints tree, per-VM logs and
  "Copy diagnostics", host check page.
- Long jobs with progress, ETA and cancel; plain-English error dialogs with
  expandable technical details; follows system light/dark + accent live.

## M4 — Import and identification
- Source discovery (block devices, image files), safety checks (mounted,
  host OS, in use, O_EXCL claim), SMART health, ddrescue advice.
- Minimal polkit helper that opens devices read-only and passes the fd back.
- Linked (qcow2 overlay) and Copy (`qemu-img convert`) modes; raw, E01,
  VHD/VHDX, VMDK, VDI, qcow2; multi-disk installs; optional SHA-256 log.
- Identification: libguestfs inspection + libosinfo + own probes (DOS,
  Win9x, BSD, macOS, BitLocker/LUKS/VeraCrypt, hibernation, multi-boot).
- Summary with user overrides before anything changes.

## M5 — Patch engine (one OS family at a time)
- Data-file profiles (hardware, firmware, RTC, patch list).
- Pre-patch snapshot, per-patch log, rollback.
- Windows NT: storage driver fixes (storahci/stornvme/msahci/MergeIDE),
  virt-v2v (in-place) where supported, HAL checks, hostile-software disable,
  hibernation discard, BitLocker key prompt, Windows 11 UEFI+SB+swtpm.
- Windows 9x/DOS: legacy device profiles, RAM cap, CPU timing, idle advice.
- Linux: initramfs with virtio, fstab/bootloader device names, GPU driver
  fallback. BSD: controller matching.
- Guest tools stage and switch to paravirtual devices.

## M6 — Install / packaging
- `install.sh` (pacman/apt/dnf/zypper) with `--update`, `--uninstall`,
  `--purge` and a manifest so uninstall reverses everything.
- PKGBUILD (AUR), .deb, .rpm; container tests; opt-in update check.
- Flatpak/Snap evaluation documented.

## M7 — Docs and verification
- README: install per distro, first run, troubleshooting, FAQ.
- Final test runs; list of OS families tested and those needing user media.
