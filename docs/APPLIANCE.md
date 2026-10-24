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
| `--timezone` | The appliance's time zone, e.g. `Europe/Berlin` (default: this computer's). Windows VMs show its local time. |
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
2. "Starting Windows XP…" appears in the top-left corner for a few
   seconds (3 by default), with notes when something limits the VM: no
   virtualization, less memory than the VM was given (the VM gets what the
   computer can spare), a small drive. Pressing **M** then opens the
   maintenance console. Nothing on the screen says so: the console is for
   whoever looks after the computer, not for the people using the VM.
3. The VM appears full screen, scaled to the screen.

| You do | What happens |
|---|---|
| Shut down the VM | The computer turns off (or, if chosen in the maintenance console, the VM starts again or the console opens). |
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

DOS and Windows 9x VMs have only a PS/2 mouse, which moves the guest's own
cursor by small steps. The appliance hands the computer's mice straight to
such a VM, including mice plugged in later. It moves exactly like a mouse
on an old PC, with no invisible edges. A laptop's touchpad still goes
through the VM's window. In a VM with only a PS/2 mouse, that window
cannot keep the pointer from stopping at the screen's edges, so use a
mouse.

If the picture is garbled or the screen stays black with a particular
graphics card, use the maintenance console's **Screen** page: turn OpenGL
drawing off (garbled picture), or always start with basic graphics (black
screen). For a single start, press Esc while the computer starts and
choose **with basic graphics**, or select the **Start** entry, press `e`,
add `rehome.gl=off` to the line that starts with `linux`, and press F10.

### Network

The VM has a wired network card (Windows shows "Ethernet", never a list of
Wi-Fi networks). Its connection goes out through the computer's own
wired or Wi-Fi connection, whichever is up, through NAT. Connect the
computer to Wi-Fi once in the maintenance console (**Network, Wi-Fi and
time zone**);
it reconnects by itself on later starts. The VM can start before the
connection is up, or while it changes; it gets online as soon as the
computer does.

### Clock and time zone

The appliance uses the time zone of the computer it was built on
(`--timezone` chooses another), and sets its clock from the internet once
it is online. Windows and DOS VMs keep their clock in local time, which
they take from the appliance's time zone each time they start. Change the
zone in the maintenance console (**Network, Wi-Fi and time zone**) if the
appliance moves.

### Drivers

None are needed. The VM sees the same virtual hardware on every computer
(the one it was given in Rehome), never the computer's own, so it needs no
new drivers when the appliance moves to another computer. Optional guest
tools work as in Rehome (README, "Guest tools"); install them in Rehome
before building the appliance. Windows XP needs them for resolutions above
1280×1024 (the QXL display; the appliance's QEMU includes it).

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

A text menu for whoever looks after the computer. It opens from the boot
menu, by pressing M while "Starting …" shows, or by itself when the VM
cannot start (the reason is shown at the top). Its header shows the VM,
the network, the clock and the free space.

| | Page | What it does |
|---|---|---|
| 1 | Start the VM | |
| 2 | Checkpoints | Take one, go back to one, delete one (chosen by number). |
| 3 | Back up or restore the VM | **Back up** writes the VM to a USB drive as one `.rehome` file in a "Rehome backups" folder (FAT32 drives hold backups up to 4 GB; exFAT and NTFS drives any size). **Restore** replaces the VM with a backup from a USB drive; its checkpoints go, CD/DVD images the appliance already has stay. Rehome on a computer imports these files too (`rehome import-vm`), and any Rehome export can be restored. |
| 4 | VM settings | Memory and processors; **put a CD/DVD image in**: an `.iso` from a USB drive is copied to the appliance and put in the VM's CD drive (a way to get drivers and programs into Windows 98, which cannot read USB sticks), and taken out again. |
| 5 | USB devices | See which devices go to the VM; exclude or include one. |
| 6 | Network and Wi-Fi | Connect to a Wi-Fi network, forget one, test the connection. |
| 7 | Date, time and time zone | Internet time, or the date and time set by hand (for computers that are never online); the time zone. |
| 8 | Sound | The volume (a mixer; it is kept across restarts), a test sound. |
| 9 | Screen | Always start with basic graphics; OpenGL drawing on or off; restart once with basic graphics. |
| 10 | Startup and shutdown | At power-on, start the VM or open the console; how many seconds "Starting …" waits for M (0–60); when the VM shuts down, turn the computer off, start the VM again, or open the console. The power button always turns the computer off. |
| 11 | Maintenance password | Set, change or remove the password the console asks for (log in as `root`). Building with `--maintenance-password` sets one from the start. |
| 12 | Storage | Free space; the VM's disks, CD/DVD images and checkpoints. |
| 13 | Hardware report | Processor, virtualization, memory, graphics, displays, sound, network. |
| 14 | Logs | The appliance's log of this start and of the one before, and the VM's QEMU log. |
| 15 | Save diagnostics to USB | Logs and reports in one `.tar.gz` file. |
| 16 | Shell | A root shell for repairs (`rehome --help` works there). |
| R, P | Restart, Power off | |

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
  laptop. A VM with only a PS/2 mouse (DOS, Windows 9x) gets the
  computer's mice directly (D41), tested with the appliance's own PS/2
  mouse moving a cursor in FreeDOS. The maintenance console (D42) is
  tested in the same emulated computer: M opens it from the start screen,
  which does not mention it; a backup to a USB stick image restored over
  the VM, which then starts; a CD image from the stick; the VM shutting
  down into the console; and the screen, startup, sound and password
  settings, kept across a restart.
