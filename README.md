# Rehome

**Give an installed operating system a new home as a virtual machine.**

Rehome is a Linux desktop app for QEMU/KVM. It does two jobs:

- **Import:** point it at a physical disk (SATA, NVMe or USB) or a disk image
  (raw/dd, E01, VHD/VHDX, VMDK, VDI, qcow2). Rehome identifies the system on it
  (DOS, Windows 95 to 11, Windows Server, Linux, the BSDs), chooses matching
  virtual hardware and applies the fixes the system needs to boot as a VM.
  The source is never written to.
- **Create and manage:** new VMs from an installer ISO, with Quick Create for
  free systems and optional automatic installation. Rehome then works like
  Hyper-V Manager: start and stop VMs, open a console, take checkpoints, clone
  and export VMs, and pass USB devices through.

Everything the GUI does is also available on the command line (`rehome --help`).

> **Status: 0.3.8, early release.** The engine, CLI and GUI work, and they are
> tested on synthetic disks and on real FreeDOS, ReactOS and Ubuntu guests. Real
> Windows and 9x installations were not tested yet; see
> [What has been tested](#what-has-been-tested).

## Contents

- [Install](#install)
- [First run](#first-run)
- [Importing an installed system](#importing-an-installed-system)
- [Creating a VM](#creating-a-vm)
- [Managing VMs](#managing-vms)
- [Appliances: a VM on its own computer](#appliances-a-vm-on-its-own-computer)
- [Command line](#command-line)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Uninstall](#uninstall)
- [What has been tested](#what-has-been-tested)
- [Development](#development)

## Install

Rehome supports Arch Linux, Manjaro, EndeavourOS, Debian, Ubuntu (and
derivatives such as Linux Mint), Fedora and openSUSE. You need an x86-64
computer. The code also handles ARM64 hosts, but they have not been tested.
For usable speed, the CPU needs hardware virtualization (Intel VT-x or AMD-V)
enabled in the firmware settings.

### Any supported distribution: the install script

```sh
curl -fsSL https://raw.githubusercontent.com/oreo1298/Disk-to-VM-Manager/main/install.sh | bash
```

The script detects your distribution and shows what it is going to do. It
installs the required packages with pacman, apt, dnf or zypper, then installs
Rehome under `/usr/local`, along with its desktop entry, icon and polkit
policy. It records every change, so `--uninstall` can reverse it exactly. To
preview without changing anything, add `-s -- --dry-run` after `bash`. It uses
`sudo` only for the steps that need it, and it asks before:

- adding you to the `kvm` group,
- installing optional packages,
- on Debian/Ubuntu, making the kernel image readable for libguestfs.

| Command | Effect |
|---|---|
| `… \| bash -s -- --update` | Update to the latest release. VMs and settings are kept. |
| `… \| bash -s -- --uninstall` | Remove Rehome and undo the installer's changes. VMs and settings are kept. |
| `… \| bash -s -- --purge` | Same, then also delete your VMs and settings, after you type `delete`. |
| `… \| bash -s -- --help` | All options (`--with-optional`, `--skip-deps`, `--prefix`, …). |

After installing, the script is also kept at
`/usr/local/share/rehome/installer/install.sh`, so updating and uninstalling
work offline.

### Native packages

| Distribution | How |
|---|---|
| Arch, Manjaro, EndeavourOS | `cd packaging/arch && makepkg -si` (PKGBUILD, ready for the AUR) |
| Debian, Ubuntu, Mint | `packaging/deb/build.sh`, then `sudo apt install ./dist/rehome_*_all.deb` |
| Fedora | `packaging/rpm/build.sh`, then `sudo dnf install ./dist/rehome-*.noarch.rpm` |
| openSUSE | `packaging/rpm/build.sh --suse`, then `sudo zypper install ./dist/rehome-*.noarch.rpm` |

Packages install under `/usr` and are removed with the package manager as
usual. [docs/PACKAGING.md](docs/PACKAGING.md) has the details, including why
there is no Flatpak or Snap.

### What gets installed

The app, the `rehome` command, a desktop entry, icons and a polkit policy.
The policy covers a small root helper, `rehome-helper`. The helper opens disks
read-only for import and opens USB devices for pass-through, after asking for
your password. Nothing else runs as root: the GUI refuses to start as root,
and QEMU runs as your user.

## First run

Open **Rehome** from your app menu, or run `rehome`. On first start Rehome
checks your computer. You can repeat the check at any time from the menu
(**Host Check**) or with:

```sh
rehome doctor
```

The check covers:

- CPU virtualization and KVM,
- the QEMU version,
- UEFI firmware (OVMF), TPM emulation (swtpm) and libguestfs,
- the GPU (for 3D),
- free space and memory.

Every problem comes with a fix in plain English, plus commands you can copy.
The most common ones:

- **"Virtualization is disabled in your firmware settings."** Restart, open
  the firmware setup (often F2, Del or F10) and enable **Intel VT-x**
  ("Virtualization Technology") or **AMD SVM**.
- **"Add yourself to the kvm group."** Run `sudo usermod -aG kvm $USER`, then
  log out and back in.

Without KVM, Rehome still works, using QEMU's software emulation (TCG), which
is 10–50× slower. Rehome tells you when that happens.

Rehome checks GitHub for new releases only if you turn that on in
**Preferences**. Rehome has no telemetry.

## Importing an installed system

**GUI:** click **+ → Import Disk…** and pick a disk or image. Rehome only reads
from it. It then shows what it found: the system and version, its edition and
architecture, BIOS or UEFI, the original disk controller, and any encryption,
hibernation or dual-boot. You can change anything it got wrong before you
click **Import**.

**CLI:**

```sh
rehome sources                          # disks that can be imported (and why others cannot)
rehome inspect /dev/sdb                 # identify only, change nothing
rehome import /dev/sdb --name "Old PC"  # identify, confirm, create the VM
rehome import laptop.vhdx --mode copy --verify --start
```

### How your source is protected

- **Read-only, always.** The source is opened read-only. A physical disk is
  also opened exclusively (`O_EXCL`), so nothing else can mount it while the
  VM uses it. Every change, including Rehome's own fixes, goes into a qcow2
  overlay in the VM's folder.
- **Linked or Copy.**
  - **Linked** (the default) is instant: the VM reads unchanged data from the
    original disk, so that disk must stay attached.
  - **Copy** makes a standalone image, so the disk can be removed afterwards.
    You choose the folder it is stored in. A linked VM can be made
    standalone later (**Summary → Make Standalone**).
- **Unsafe sources are refused.** Rehome will not import:
  - the disk your running Linux is on,
  - a disk with mounted partitions or active swap,
  - a disk in use by LVM, RAID or dm-crypt.
- **Failing disks are handled gently.** Rehome reads SMART health first. If a
  disk is failing, it stops and explains how to rescue the data with
  `ddrescue` onto a healthy disk first, rather than wearing the failing disk
  out.
- **Proof, if you want it.** `--verify` (or **Verify integrity** in the
  wizard) records a SHA-256 of the source before and after the import, in
  `logs/import.log`. Later, `rehome verify-source VM` re-checks the source.

### What Rehome fixes automatically

Before changing anything, Rehome takes a checkpoint named **Before automatic
fixes**. Each fix is logged (`rehome patch log VM`). **Undo Fixes** on the
Summary tab, or `rehome patch rollback VM`, reverts all of them. The fixes are
chosen per operating system from profiles in `data/profiles/`:

- **Windows 2000 to 11**
  - Fixes the "INACCESSIBLE_BOOT_DEVICE" (0x7B) blue screen. Rehome either
    emulates the controller the system knows, or enables the in-box AHCI,
    NVMe and IDE drivers (`storahci`, `stornvme`, MergeIDE for 2000/XP).
  - Injects virtio drivers with virt-v2v where it is installed.
  - Checks the HAL on 2000/XP.
  - Disables services and filter drivers that only work on the original
    hardware: RAID utilities, OEM tools, old hypervisor guest tools.
  - Keeps the clock in local time.
  - Windows 11 gets UEFI, Secure Boot and a real (emulated) TPM 2.0.
- **Windows 95/98/ME**
  - Limits memory to what 9x can handle.
  - Uses emulation for stable timing.
  - Provides period-correct devices: Cirrus VGA, Sound Blaster 16, PCnet.
- **Linux**
  - Rebuilds the initramfs with the virtio drivers.
  - Rewrites device names in `fstab` and the boot loader.
  - Falls back from proprietary GPU drivers.
  - Fixes the network configuration that was tied to old hardware.
- **DOS, Windows 9x**
  - Repairs the BIOS boot chain when the disk was prepared on Linux:
    - a partition formatted with `mkfs.fat` ("This is not a bootable
      disk"),
    - no active partition, or a partition type DOS does not know,
    - an MBR without boot code.
  - Windows 95/98 get Rehome's own boot sector, which starts their `IO.SYS`
    the way grub4dos does. FreeDOS and MS-DOS 6 get SYSLINUX's `chain.c32`,
    which starts their `KERNEL.SYS` or `IO.SYS`.
  - Warns when a Windows 9x partition reaches past 128 GiB, which its IDE
    driver cannot address.
- **BSD**: chooses suitable hardware and reports what was found.

Rehome warns about two cases and decides nothing for you:

- **BitLocker or LUKS:** Rehome asks for the recovery key or passphrase.
- **Hibernation or Fast Startup:** Rehome asks whether to discard the
  hibernated session. It is discarded only in the VM's overlay, never on the
  disk.

No drivers are needed to start: Rehome gives each system virtual hardware it
has built-in drivers for (Windows 11: SATA disk, Intel network card, standard
VGA, Intel HD Audio, USB 3; Windows XP: IDE, Realtek network card, Cirrus
graphics, AC'97 sound, USB 1.1). The guest tools are optional and make it
faster or nicer. After the first successful boot, the Summary tab offers them:

- virtio-win for Windows 8 and later, downloaded from the Fedora project
  (its current releases do not support older Windows),
- for Windows XP, Server 2003 and 7: the SPICE guest tools that libosinfo
  lists for them (0.65 for XP, 0.141 for 7), with the QXL display driver
  (XP: resolutions up to 2560×1600 instead of the Cirrus card's 1280×1024),
- nothing for Windows 2000 and older (they keep their built-in drivers),
- the SPICE agent and the QEMU guest agent for Linux.

It then offers to switch the VM to the faster devices (virtio; QXL graphics
for XP), with a checkpoint first. Before a Windows VM's disks become virtio,
Rehome looks inside Windows:

- It makes the virtio disk driver load at boot. Installers only store it
  until a virtio disk appears, and without this step Windows stops with
  0x7B INACCESSIBLE_BOOT_DEVICE.
- A disk or network card whose driver Windows lacks keeps its current
  model.

Reverting to the checkpoint also puts the old devices back.

## Creating a VM

Click **+ → New Virtual Machine…** or run `rehome create`.

- **From an ISO.** Rehome identifies the system on the disc (via libosinfo,
  or from the installer's own files for releases and derivatives libosinfo
  does not know) and suggests the CPUs, memory, disk size, firmware and
  TPM. You can change the detected system on the hardware page, and choose
  where the disk image is stored (**Disk location**), for example on a
  larger second drive.
- **Quick Create.** Downloads a free system from its official site and
  verifies the download:
  - Ubuntu Desktop and Server LTS, Fedora Workstation and Server, Debian,
    openSUSE Tumbleweed, Arch Linux, Linux Mint,
  - FreeBSD, FreeDOS, ReactOS.

  For Windows, Rehome only opens Microsoft's official download page.
- **Automatic installation.** You enter a user name and password, and the
  installation runs without questions.
  - Supported for Windows (10, 11, Server) and for Ubuntu, Debian, Fedora
    and openSUSE. So far only Ubuntu has been tested end to end; see
    [What has been tested](#what-has-been-tested).
  - It never bypasses product activation: you give your own product key, or
    use evaluation media.
  - When the installation is done, remove the answer files, which contain
    your password, from the Summary tab.
- **Advanced settings** (in the VM's **Settings** tab, or `rehome set`):
  - CPUs, memory, CPU model and acceleration,
  - disks: add a new disk or an existing image file, **Replace…** a disk
    with another image or a new empty one (same bus and boot position),
    enlarge or remove one; CD drives and floppy,
  - BIOS, UEFI or Secure Boot, and the TPM,
  - NAT or bridged networking,
  - shared folders (virtiofs),
  - display, 3D, sound and USB controller.

```sh
rehome create --iso ~/Downloads/Win11_24H2.iso --unattended --user me
rehome quick-create ubuntu-desktop-lts --start         # `rehome quick-create --list` shows all
rehome create --os freedos1.3 --name DOS --memory 32M --disk 500M
rehome create --iso omarchy.iso --disk 128G --disk-dir /mnt/big/vm-disks
rehome disk replace "Old PC" disk0 --image ~/backup/old-pc.qcow2
```

## Managing VMs

- **VM list**: live state and thumbnails. Select a VM to see its
  **Summary**, **Checkpoints**, **Settings** and **Logs**.
- **Power**:
  - Start, Shut Down (ACPI), Pause/Resume,
  - Save (memory to disk; the next start resumes),
  - Reset, Turn Off.
- **Console**: the VM's screen in its own window, with:
  - clipboard sharing (needs the SPICE agent in the guest),
  - automatic guest resolution,
  - USB pass-through, full screen (Ctrl+Alt+F),
  - Ctrl+Alt to release the keyboard and mouse,
  - Ctrl+Alt+Del and other key combinations from the menu.
- **Checkpoints**: a tree of snapshots. Checkpoints of a running VM include
  its memory. You can revert to, start from or delete any checkpoint.
- **Clone, Export / Import**: `.rehome` files contain the VM and its disks.
  **Delete** can keep the disk files.
- **Where disks are stored**: in the VM's folder by default. Set **Folder
  for new disks** in Preferences (or `rehome disk folder /mnt/big`) to put
  new disks, copies and clones on another drive; each new disk can also get
  its own folder. Disks Rehome created there are deleted with their VM;
  disk images you attached yourself are never moved or deleted.
- **Logs and diagnostics**: QEMU's log, the import and patch logs, and **Copy
  Diagnostics** for bug reports.
- **Hooks**: scripts in `~/.config/rehome/hooks.d/` run when VMs start and
  stop. See [docs/HOOKS.md](docs/HOOKS.md).

VMs live in `~/.local/share/rehome/vms/<name>-<id>/`. Each VM folder holds
`vm.toml` (readable settings), `disks/` (unless disks are stored elsewhere),
`logs/` and `nvram`/`tpm` state.
QEMU runs detached, so VMs keep running when you close the window.

Keyboard shortcuts: Ctrl+N new VM, Ctrl+I import, Ctrl+, preferences, Ctrl+?
all shortcuts.

## Appliances: a VM on its own computer

**Build Appliance…** (in a VM's ⋮ menu, or `rehome appliance build VM
out.img`) turns a VM into a drive image. Write it to an SSD or USB drive,
boot a computer from it, and the VM starts full screen with that computer's
screen, keyboard, mouse, sound and USB ports, as if it were installed
there. Shutting the VM down turns the computer off. It is meant for old
systems (Windows XP, 98, 7…) that have to keep running after their
hardware is gone.

- Works on BIOS and UEFI computers, desktops and laptops; the VM gets the
  memory and processors the computer can spare.
- A hidden boot menu (Esc) offers basic graphics and a **maintenance
  console**: checkpoints, USB devices, Wi-Fi, a hardware report, saving
  diagnostics to a USB stick, and a root shell.
- Built without root. The first build downloads a base system (Ubuntu
  26.04 LTS with QEMU, about 1.2 GB) and caches it.
- Turn Secure Boot off and virtualization on in the target computer's
  firmware settings.

See [docs/APPLIANCE.md](docs/APPLIANCE.md).

## Command line

```
rehome                    open the GUI
rehome doctor             check this computer
rehome list | info VM     list VMs / show one
rehome start|shutdown|force-off|pause|resume|save|reset VM
rehome console VM         open the console window
rehome screenshot VM out.png | screen-text VM | type VM TEXT | send-keys VM ctrl-alt-delete
rehome create … | quick-create ID | os-detect ISO
rehome sources | inspect SRC… | import SRC… | verify-source VM
rehome patch log|run|rollback VM
rehome tools status|boot-ok|insert|paravirt VM
rehome checkpoint list|create|revert|delete VM …
rehome set VM --memory 8G --cpus 4 … | cd VM file.iso
rehome disk list|add|replace|remove|standalone VM … | disk folder [DIR]
rehome usb list|attach|detach …
rehome clone | export | import-vm | delete VM
rehome logs VM | diagnostics VM
rehome appliance build VM out.img | appliance base [--refresh]
```

VMs can be named by name, ID or ID prefix. Most commands have `--json` for
scripts. Add `-v` to any command for technical details.

## Troubleshooting

| Problem | What to do |
|---|---|
| "KVM is not available" | Run `rehome doctor`. Enable VT-x/SVM in the firmware settings, check that the `kvm_intel`/`kvm_amd` module is loaded, and add yourself to the `kvm` group (then log out and in). Inside another VM, enable nested virtualization on the outer host. |
| "No variable store with Microsoft's keys" (Arch, Manjaro, EndeavourOS, CachyOS) | Arch's `edk2-ovmf` has Secure Boot firmware but no Microsoft keys. Install `virt-firmware` (`sudo pacman -S --needed virt-firmware`); Rehome then enrolls the keys into its own copy of the store (in `~/.local/share/rehome/firmware/`). |
| `nbdkit` cannot be installed ("target not found" on Arch) | Nothing to do: nbdkit is only in the AUR, and Rehome uses its own built-in disk server when nbdkit is missing. |
| libguestfs is installed but has no Python bindings (Arch) | Nothing to do since 0.1.2: Rehome then uses libguestfs' C library directly. `rehome doctor` shows which way it uses. |
| Import or inspect fails with a libguestfs "supermin" or kernel error (Debian/Ubuntu) | libguestfs needs to read the kernel image, which Debian/Ubuntu make root-only. `rehome doctor` shows the `dpkg-statoverride` command. The install script offers to run it. |
| Windows shows `INACCESSIBLE_BOOT_DEVICE` (0x7B) | Open **Settings → Disk controller** and choose the controller the PC had (SATA/AHCI, IDE or NVMe; the import summary shows what was detected), or re-run the fixes with `rehome patch run VM`. |
| "Make the disk bootable" failed for FreeDOS or MS-DOS ("SYSLINUX could not be installed") | The disk's boot sector was written on Linux and Rehome replaces it with SYSLINUX. Install `syslinux` and `mtools` (on Debian/Ubuntu also `extlinux`), then run `rehome patch run VM --only dos.boot_record`. `rehome patch log VM` shows libguestfs' own error. Windows 95/98 do not need SYSLINUX. |
| Windows 7 blue-screens after "Switch to faster devices" (Rehome 0.3.7 and older) | Update Rehome, then revert to the checkpoint "Before switching to virtio" (Checkpoints tab, or `rehome snapshot revert VM "Before switching to virtio"`). Revert now also brings back the SATA disk and e1000 network. Switch again: Rehome now makes the virtio disk driver load at boot first. |
| Windows 98 stops at SYSLINUX's "Booting..." (VMs repaired by Rehome 0.3.5 or 0.3.6) | Update Rehome, turn the VM off and run `rehome patch run VM --only dos.boot_record`: it replaces SYSLINUX with Rehome's Windows 9x boot sector. |
| Windows asks to be activated again | Expected: the virtual hardware differs from the PC. Reactivate with your own licence; Rehome never bypasses activation. |
| Old Windows (9x/2000/XP) runs too fast, hangs or crashes on start | Use the profile's defaults: emulation (TCG) for 9x, at most 512 MB of RAM, a Pentium-class CPU model. |
| The console stays black | The guest may have switched its display off. Press a key in the console, or check **Logs**. `rehome screenshot VM x.png` shows what QEMU itself sees. |
| Mouse is offset or "stuck" in the console | Press Ctrl+Alt to release it. Guests without tablet or SPICE-agent support use relative mouse mode, which GTK 4 cannot fully confine on all desktops. |
| No network in the VM | NAT uses `passt` when it is installed, otherwise QEMU's built-in NAT. For a bridge, see **Settings → Network** (needs `qemu-bridge-helper` set up by your distribution). |
| A disk is "in use" and cannot be imported | Unmount its partitions, turn off swap on it, or deactivate LVM/RAID on it. Rehome never imports a disk the host is using. |
| Something else | **Logs → Copy Diagnostics**, then open an issue at https://github.com/oreo1298/Disk-to-VM-Manager/issues. |

## FAQ

**Is it legal to run my old Windows installation in a VM?** That depends on
your licence. Retail licences can usually be moved to a new (virtual) machine.
OEM licences are often tied to the original PC. Windows will usually ask to be
reactivated because the hardware changed. Rehome warns you about this. It
never bypasses activation, DRM or licence checks.

**Where do I get Windows?** From Microsoft:
<https://www.microsoft.com/software-download/>. Evaluation versions of
Windows 11 Enterprise and Windows Server are free for 90–180 days. Rehome
does not download Windows itself.

**Can I import a Mac or run macOS?** No. Rehome identifies macOS disks and
says so, but macOS guests are not supported (also for licensing reasons).

**GPU pass-through?** Not built in. [docs/HOOKS.md](docs/HOOKS.md) shows how to
add VFIO devices with a hook script if you want to experiment.

**Does Rehome change my physical disk?** No, never. All writes go to the
VM's overlay file. Use `--verify` if you want the SHA-256 proof.

**libvirt / virt-manager?** Rehome drives QEMU directly and does not need
libvirt. It does not touch your libvirt VMs. [docs/DECISIONS.md](docs/DECISIONS.md)
explains why.

## Uninstall

- **Install script:** `/usr/local/share/rehome/installer/install.sh --uninstall`,
  or `--purge` to also delete your VMs.
- **Packages:** use your package manager (`pacman -R rehome`,
  `apt remove rehome`, `dnf remove rehome`, `zypper remove rehome`).
- **Your VMs and settings** stay in `~/.local/share/rehome`, `~/.config/rehome`
  and `~/.cache/rehome` until you delete them.

## What has been tested

Tested so far (details and how to re-run: [docs/TESTING.md](docs/TESTING.md)):

- **Host check:** on Ubuntu 24.04, with and without KVM.
- **Lifecycle on a real FreeDOS guest:** start, stop, save/restore,
  pause/resume, reset, online and offline checkpoints, clone, export/import,
  console (display, keyboard), hooks.
- **Import:**
  - identification and patching of synthetic Windows 10, Windows 11, XP,
    98 and MS-DOS disks,
  - linked and copy imports of a real FreeDOS disk, which boot to the
    prompt with the source's SHA-256 unchanged,
  - an Ubuntu 24.04 cloud image, which boots to the login prompt,
  - a real ReactOS 0.4.15 installation (NT family), which boots to the
    desktop.
- **New VMs:**
  - ReactOS (NT family) from its official ISO boots to Setup,
  - Quick Create of Ubuntu Server with automatic installation: downloaded,
    verified, installed without questions, and boots to the login prompt.
- **Packaging:** the install script and the packages, in containers. See
  [docs/PACKAGING.md](docs/PACKAGING.md).
- **Appliances:** built without root from a FreeDOS VM, then booted in QEMU
  as a BIOS and as a UEFI computer: the VM appears full screen, first boot
  grows the data partition, a USB stick is handed to the VM, the power
  button reaches the appliance, and the maintenance console works (password
  login on the screen, starting the VM from it, powering off when the VM
  stops).

**Not tested yet:**

- real Windows installations of any version,
- real 9x and NT4/2000 installations,
- BSDs,
- physical disks (only loop devices),
- appliances on real computers (GPUs, laptops, Wi-Fi, sound output) and with
  a Windows guest inside.

These need installation media or disks that could not be used in the build
environment. [docs/TESTING.md](docs/TESTING.md) lists the images needed and the
one command that runs all boot tests on your machine.

## Development

```sh
git clone https://github.com/oreo1298/Disk-to-VM-Manager && cd Disk-to-VM-Manager
bin/rehome doctor          # run from the checkout
python3 -m pytest tests/unit
python3 -m pytest tests/integration   # needs QEMU and libguestfs; slow
```

- Plan and milestones: [PLAN.md](PLAN.md)
- Design decisions and their reasons: [docs/DECISIONS.md](docs/DECISIONS.md)
- Packaging: [docs/PACKAGING.md](docs/PACKAGING.md)

Rehome is written in Python 3.11+ with GTK 4 and libadwaita, and drives QEMU
over QMP.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
