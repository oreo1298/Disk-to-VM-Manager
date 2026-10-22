# Testing Rehome

| Layer | Where | Needs | Time |
|---|---|---|---|
| Unit | `tests/unit` | Python, PyGObject | seconds |
| Integration | `tests/integration` | QEMU, libguestfs, nasm, mtools, syslinux | minutes |
| Packaging | `tests/packaging` | Docker or Podman, network | 10–30 min |
| GUI tour | `tests/gui/tour.sh` | Xvfb, dbus-run-session | 1 min |
| Real-system boot tests | `tests/boot` | **your own disk images** (see below) | 5–20 min per image |
| Appliance | `tests/integration/test_appliance.py` | the integration tools, network once; opt in with `REHOME_TEST_APPLIANCE=1` | 15 min with KVM; about 1 h without |

```sh
python3 -m pytest tests/unit                       # always
python3 -m pytest tests/integration                # slow; builds its own test disks
tests/gui/tour.sh "" /tmp/shots                    # PNG screenshots of every window
REHOME_TEST_MEDIA=~/rehome-test-images python3 -m pytest -m boot tests/boot -v
```

All tests use image files only. No test writes to, formats or partitions a
real disk. The integration tests run with a throw-away `REHOME_HOME`, so they
never touch your VM library.

## What the integration tests build and check

The tests build their disks themselves at test time. No OS images are
downloaded or committed.

- **FreeDOS** (`tests/fixtures/freedos.py`): a bootable FAT16 disk with the
  FreeDOS kernel, FreeCOM and a marker in `AUTOEXEC.BAT`. Boot success is
  read from VGA text memory.
- **Synthetic Windows** (`tests/fixtures/windows.py`, `regf.py`): NTFS disks
  with the Windows directory layout, real registry hives (SYSTEM, SOFTWARE)
  written by our regf generator, PE files with version resources, BCD/NTLDR
  boot files, hiberfil.sys headers and BitLocker signatures. Variants:
  Windows 10, Windows 11, XP, 98 and MS-DOS.

| Test file | What it proves |
|---|---|
| `test_runner_freedos.py` | Full VM lifecycle on a real guest: start and stop, save and restore, pause and resume, reset, online checkpoints (with RAM), offline checkpoints, revert, start from a checkpoint, clone, export and import |
| `test_import.py` | Identification and patching of the synthetic Windows 10/11/XP/98 and MS-DOS disks, rollback of the patches, Windows 11 getting Secure Boot and a TPM, linked and copy imports of FreeDOS that boot to the prompt, and that the source's SHA-256 is unchanged |
| `test_dbus_display.py` | The console client against real QEMU: framebuffer, video mode changes, keyboard input reaching DOS, connection time |
| `test_unattended_quick_create.py` | Quick Create and unattended-install media generation (answer files, ISO remastering) |

## Real-system boot tests (your images)

These tests are the real proof: an actual installed system, imported by
Rehome, boots to its login screen or desktop with no manual steps, and the
image is unchanged afterwards. They need images of real installations, which
cannot be downloaded legally or automatically for most systems.

1. Put the images in a folder, for example `~/rehome-test-images`. They are
   opened read-only, and the test checks their SHA-256 before and after.
2. Describe them in `images.toml` in the same folder:

   ```toml
   [[image]]
   id = "xp-sp3-dell"                 # name for results (default: file name)
   file = "xp-sp3-dell.img"           # or files = ["disk1.vmdk", "disk2.vmdk"]
   family = "windows"                 # expected: windows, win9x, dos, linux, bsd
   version = "5.1"                    # expected version prefix (optional)
   timeout = 900                      # seconds until a login or desktop must appear
   # success_text = ["Welcome"]       # extra regexes for the success check (optional)
   # keys = ["123456-..."]            # BitLocker recovery key / LUKS passphrase
   # overrides = { memory_mib = 1024 }
   # mode = "copy"                    # default "linked"
   # keep_vm = true                   # keep the VM for inspection
   ```

3. Run the tests:

   ```sh
   REHOME_TEST_MEDIA=~/rehome-test-images python3 -m pytest -m boot tests/boot -v
   ```

The results are written to `~/rehome-test-images/results/<id>/`:

- `analysis.txt`: what Rehome identified,
- `screens/*.png`: screenshots taken during the boot,
- `verdict.txt`: why the test passed or failed.

Set `REHOME_TEST_ARTIFACTS` to put the results elsewhere. On a machine with
KVM, a boot takes a few minutes. Without KVM, allow 20–40 minutes per
Windows image.

### Which images help most

Images of a **real computer's disk** are best, because they contain the
original storage drivers that Rehome has to fix. Ways to get one:

- `dd`/`ddrescue` of the disk to a file,
- Disk2vhd (VHDX) on a running Windows,
- a backup tool's raw or VHD export.

Images of installs made inside a VM are useful too. Install with a SATA/AHCI
or NVMe controller and without virtio drivers, so they resemble real
hardware.

| Family | Suggested images | Where to get them |
|---|---|---|
| DOS | MS-DOS 6.22 or PC DOS 7 on a FAT16 disk | your own media; FreeDOS is covered automatically |
| Windows 95 / 98 / ME | an installed 98 SE and an ME system | your own media or old PCs |
| Windows NT 4 / 2000 | 2000 Professional SP4 | your own media |
| Windows XP / Server 2003 | XP SP3 from a real PC (IDE or AHCI) | your own PC |
| Windows Vista / 7 / 8.1 | 7 SP1 with AHCI | your own PC |
| Windows 10 / 11 | a real laptop with NVMe; one with BitLocker (with its recovery key); one hibernated with Fast Startup | your own PC; or install the free evaluation ISOs from Microsoft |
| Windows Server 2016–2025 | evaluation installs | free evaluation ISOs: <https://www.microsoft.com/evalcenter> |
| Linux | Ubuntu, Fedora, Arch, openSUSE installs from real PCs, including one with the NVIDIA driver and one with LUKS | your own PCs; the distributions' installers |
| BSD | FreeBSD and OpenBSD installs | <https://download.freebsd.org>, <https://www.openbsd.org> |

## What was verified while building 0.1.0

Development ran in a cloud container: Ubuntu 24.04, QEMU 8.2.2, GTK 4.14,
libadwaita 1.5 and libguestfs 1.52. The container had KVM during the first
part of the work and no KVM later, so both paths were exercised.

| Area | Result |
|---|---|
| `rehome doctor` | Correct with KVM and without it. The latter case is diagnosed as "this system is a VM without nested virtualization", with the fix. |
| Unit tests | All pass (3 network tests skipped by default) |
| Integration tests | All pass: FreeDOS lifecycle, import and patching of synthetic Windows 10/11/XP/98 and MS-DOS disks, the console client |
| Source safety | Source SHA-256 unchanged after import and boot of linked and copy imports. The polkit helper refuses the host's root disk and mounted disks, opens disks with `O_EXCL`, and the opened fd cannot write. Tested on a loop device backed by an image file. |
| ReactOS 0.4.15 (NT family) | Installed unattended from its official ISO into a raw disk image (a scratch `unattend.inf`, not a Rehome feature). The installed disk was then imported with `tests/boot`, linked and copy. Rehome identified it as NT 5.2 build 3790, BIOS, IDE. Both VMs booted to the desktop (OCR found "Recycle Bin"), and the source's SHA-256 was unchanged. |
| Ubuntu 24.04 cloud image | Imported with `tests/boot`: identified as Ubuntu 24.04.5 (UEFI), Linux fixes applied, boots to `login:` on the serial console. Source SHA-256 unchanged. |
| Quick Create + automatic install (Ubuntu Server) | `rehome quick-create ubuntu-server-lts --unattended --user alice --start` downloaded the 26.04.1 ISO and verified it against Ubuntu's `SHA256SUMS`. The installer ran without questions, including the security updates, and powered the VM off. The next start booted the installed system to `login:`. This took about 2 h 10 min under emulation. |
| Quick Create downloads | ReactOS (SourceForge SHA-256) and Ubuntu downloaded and verified; a repeated request reuses the file. The other catalog sources were blocked by the network policy and are tested against recorded sample responses. |
| GUI | Every window, wizard and dialog rendered on Xvfb in light and dark mode (`tests/gui/tour.sh`). The console showed a live FreeDOS guest and passed keyboard input. |
| Install script and packages | See [PACKAGING.md](PACKAGING.md) → Testing |

### Not verified (needs your hardware or media)

- Real Windows installations of any version, real Windows 9x, NT 4/2000,
  MS-DOS and BSD installations: run `tests/boot` with your images.
- Physical disks: only loop devices were used, by design.
- KVM performance, 3D (virgl) and USB pass-through with real devices.
- Install script and packages on a real Arch desktop (only in containers):
  run `./install.sh`, `./install.sh --update` and `./install.sh --uninstall`
  on your machine.
- Windows unattended installation: the answer files are generated and
  unit-tested, but no Windows ISO could be downloaded in the build
  environment.
- Automatic installation of Fedora, Debian, openSUSE and Ubuntu Desktop:
  the answer media are generated and checked, but the installers were not
  run, because their download sites were blocked.

## The appliance test

`REHOME_TEST_APPLIANCE=1 python3 -m pytest tests/integration/test_appliance.py`
builds an appliance from a FreeDOS VM and boots it in QEMU twice, as a BIOS
and as a UEFI computer with a USB stick plugged in. Through a root shell on
the serial console (the test build sets a maintenance password) and OCR of
the screen it checks: the image layout (partitions, both boot loaders, new
UUIDs); first boot (data partition grown, hardware report); the VM on
screen; GRUB's `recordfail` cleared once the VM shows; the USB stick handed
to the VM; the power button reaching the appliance; and, after choosing the
maintenance entry for the next boot, the console after a password login on
the screen and starting the VM from it.

The base system (about 1.2 GB of downloads) is cached in
`~/.cache/rehome/appliance` (`REHOME_APPLIANCE_CACHE` overrides it) and
shared with your own builds. Without KVM the VM inside the appliance is
emulated inside an emulated computer, so timeouts are scaled up.
