# Appliances: a VM that boots on its own computer

An appliance is a drive image that turns a computer into a dedicated machine
for one VM. Write it to an SSD, USB drive or SD card, boot a computer from
it, and the VM appears full screen within seconds, with the computer's
keyboard, mouse, screen, sound and USB ports. Turning the VM off turns the
computer off. Nobody sees the Linux underneath unless they ask for it.

Typical use: keep a Windows XP (or 98, or 7) installation running on a
modern computer after the original hardware died, without anybody having
to learn a virtualization program.

Design decisions: [DECISIONS.md](DECISIONS.md) D25–D28.

## Building one

Any Rehome VM can become an appliance: an imported disk, a VM you already
use, or a new VM whose installation you want to finish on the target
computer (its install ISO is copied along).

```sh
rehome appliance build "Windows XP" windows-xp.img
```

- The first build downloads and prepares the base system (Ubuntu 26.04 LTS
  with QEMU, about 1.2 GB) and caches it in `~/.cache/rehome/appliance`.
  Later builds reuse it for 30 days; `--refresh` fetches the latest updates.
- The VM must be turned off. Its disks are copied into the image as
  standalone files: the appliance never needs the original drive or image.
  Physical-disk imports are read through the same permission prompt as
  when the VM starts.
- No root is needed: the image is assembled inside libguestfs's helper VM.

Options:

| Option | Meaning |
|---|---|
| `--no-media` | Leave the VM's ISO/floppy images out. |
| `--maintenance-password` | Ask for a password the maintenance console will require (also `--maintenance-password-file`). |
| `--network none` | No network for the VM (the default is NAT through the computer's wired or Wi-Fi connection). |
| `--no-usb-auto` | Do not give plugged-in USB devices to the VM automatically. |
| `--format qcow2` | A qcow2 image for testing in a VM instead of a raw image for a drive. |
| `--cpu-model` | A fixed processor model for the VM (e.g. `Westmere`). By default the VM keeps its own setting; VMs set to "host" see a different processor on each computer. |
| `--kernel-args` | Extra Linux arguments for every boot entry (e.g. `console=ttyS0`). |

The image is sparse: it takes little space on your disk, but writing it to
a drive writes its full size (about 7 GB plus the VM). On first boot the
appliance grows its data partition to fill the drive.

## Writing it to a drive

Use any image writer: GNOME Disks (**Restore Disk Image…**), balenaEtcher,
Rufus on Windows, or `dd`:

```sh
sudo dd if=windows-xp.img of=/dev/sdX bs=4M conv=fsync status=progress
```

Double-check `/dev/sdX`: everything on that drive is replaced. Rehome
itself never writes to drives.

## On the target computer

- Turn **Secure Boot off** in the firmware settings (the appliance's boot
  loader is not signed).
- Turn **virtualization on** (Intel VT-x / AMD-V, often under CPU or
  Advanced). Without it the VM still runs, much slower, and a note says so
  before it starts.
- Boot from the drive (BIOS and UEFI computers both work).

At power-on:

1. The boot menu is hidden; the VM starts directly.
2. A short message ("Starting Windows XP… Press M for the maintenance
   console") appears, with notes when something limits the VM: no
   virtualization, less memory than the VM was given (the VM gets what the
   computer can spare), a small drive.
3. The VM appears full screen, scaled to the screen.

| You do | What happens |
|---|---|
| Shut down the VM | The computer turns off. |
| Press the power button | The VM is asked to shut down (like pressing a PC's power button); then the computer turns off. Holding it forces the computer off. |
| Plug in a USB device | It is given to the VM, except keyboards and mice (they already reach the VM), network adapters, and the drive the appliance runs from. |
| Built-in devices (a laptop's camera, fingerprint reader, Bluetooth) | They stay with the appliance; include one in the maintenance console's **USB devices** to give it to the VM. |
| Press Esc while the computer starts | The boot menu appears. |

### Screen and mouse

The VM's picture fills the screen. Guests that can follow the screen's
resolution (Linux, Windows 7 and later) are offered the screen's native
resolution; older ones (XP, 98) keep the resolution set inside them and are
scaled to fit, with black bars when the shape differs (a 4:3 desktop on a
16:9 laptop). The pointer reaches the whole picture either way.

If the picture is garbled or the screen stays black with a particular
graphics card, press Esc while the computer starts, select the **Start**
entry, press `e`, add `rehome.gl=off` to the line that starts with `linux`,
and press F10; or choose **with basic graphics**.

### Network

The VM has a wired network card (Windows shows "Ethernet", never a list of
Wi-Fi networks). Its connection goes out through the computer's own
wired or Wi-Fi connection, whichever is up, through NAT. Connect the
computer to Wi-Fi once in the maintenance console (**Network and Wi-Fi**);
it reconnects by itself on later starts. The VM can start before the
connection is up, or while it changes; it gets online as soon as the
computer does.

### If the VM stops unexpectedly

The maintenance console opens and says what happened: for a crash of
QEMU, the program that runs the VM, its last message and the USB devices
the VM had. **Save diagnostics to a USB stick** collects the logs for a
report. If the crash comes back with a particular USB device, exclude it
in **USB devices**.

### The boot menu

- **Start** *VM* — the default.
- **Start** *VM* **with basic graphics** — for screens that stay black:
  uses the firmware's display and software rendering.
- **Maintenance console** (and its basic-graphics variant).
- **Firmware settings** (UEFI computers), **Restart**, **Power off**.

If a start did not get as far as showing the VM, the menu appears by itself
on the next start.

### The maintenance console

A text menu, reached from the boot menu, by pressing M before the VM
starts, or automatically when the VM cannot start (the reason is shown at
the top):

1. Start the VM
2. Checkpoints — take one, go back to one, delete one
3. USB devices — see which devices go to the VM; exclude or include one
4. Network and Wi-Fi — connect to a Wi-Fi network, test the connection
5. Hardware report — processor, virtualization, memory, graphics, displays, sound, network
6. Save diagnostics to a USB stick — logs and reports in one `.tar.gz` file
7. Shell — a root shell for repairs (`rehome --help` works there)
8. Restart with basic graphics
9. Restart
0. Power off

## What is inside

| Partition | Size | Contents |
|---|---|---|
| BIOS boot | 1 MiB | GRUB for BIOS computers |
| EFI system | 256 MiB | GRUB for UEFI computers (`EFI/BOOT/BOOTX64.EFI`) |
| Rehome system | 6 GiB | Ubuntu 26.04 LTS base, QEMU, cage, Rehome |
| Rehome data | the rest | `/var/lib/rehome`: the VM, its checkpoints, settings |

The VM runs as an unprivileged `rehome` user, in `cage` (a kiosk Wayland
compositor) showing QEMU's own window. It is a normal Rehome VM: the same
`vm.toml`, the same QEMU command line builder.

## Limits (phase 1)

- Graphics are emulated (what Windows XP and older need). Giving a
  dedicated GPU to the VM on computers with two GPUs, and 3D acceleration
  for games, are planned.
- The appliance's own Linux is not updated automatically. Build a new image
  with `--refresh` to get the latest updates.
- UEFI VMs start with fresh UEFI settings (Windows boots from the default
  path).
- Shared folders and PCI pass-through settings of the VM are left out.
- Tested automatically in emulated BIOS and UEFI computers with a FreeDOS
  VM on the Windows XP profile's virtual hardware (see
  [TESTING.md](TESTING.md)). Reported from a real laptop (ThinkPad X1
  Carbon): an Omarchy and a Windows 11 VM run full screen with the whole
  screen reachable by the pointer; the Windows 11 VM had no network and a
  Windows XP VM stopped after reaching its desktop, both addressed in
  0.3.1 ([DECISIONS.md](DECISIONS.md) D32) but not yet re-checked on that
  laptop. DOS and Windows 9x guests with a PS/2 mouse rely on SDL's mouse
  capture, which is untested in the kiosk.
