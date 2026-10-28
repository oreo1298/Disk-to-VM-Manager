"""Build an appliance from a FreeDOS VM and boot it (BIOS and UEFI) in QEMU.

The outer QEMU plays the target computer: an AHCI disk with the appliance,
a USB stick, a network card and a VGA screen. A serial console with a root
login (the build sets a maintenance password) is used to check what happens
inside; the screen is read with OCR.

Very slow without KVM (the inner VM is emulated inside an emulated
computer): opt in with REHOME_TEST_APPLIANCE=1. The first run also downloads
the base system (about 1.2 GB) into the cache.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from rehome.errors import RehomeError
from tests.fixtures import freedos

pytestmark = pytest.mark.slow

if os.environ.get("REHOME_TEST_APPLIANCE") != "1":
    pytest.skip("set REHOME_TEST_APPLIANCE=1 (slow, downloads ~1.2 GB once)",
                allow_module_level=True)
if freedos.available() or not shutil.which("qemu-system-x86_64"):
    pytest.skip(f"cannot build/boot FreeDOS: {freedos.available() or 'no QEMU'}",
                allow_module_level=True)

PASSWORD = "rehome-test"
# A DOS program switching to VESA mode 117h (1024x768, 16-bit colour), like a Windows desktop:
# mov ax,4F02h / mov bx,0117h / int 10h / ret
VBE_COM = bytes([0xB8, 0x02, 0x4F, 0xBB, 0x17, 0x01, 0xCD, 0x10, 0xC3])
# A DOS program turning the computer off through ACPI, as Windows does when it shuts down:
# mov dx,604h / mov ax,2000h / out dx,ax / ret (SLP_EN in the PM1a control register)
POWEROFF_COM = bytes([0xBA, 0x04, 0x06, 0xB8, 0x00, 0x20, 0xEF, 0xC3])
# Terminal escapes: OSC (systemd's shell context markers, titles) and CSI sequences.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")
KVM = os.access("/dev/kvm", os.R_OK | os.W_OK)
SLOW = 1 if KVM else 6              # timeouts scale without KVM


class Serial:
    """A root shell on the outer VM's serial console."""

    def __init__(self, path: Path, log: Path):
        self.path, self.log = path, log
        self.sock = None
        self.buf = ""

    def connect(self, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(str(self.path))
                s.settimeout(0.5)
                self.sock = s
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def _read(self):
        try:
            data = self.sock.recv(65536)
        except (TimeoutError, socket.timeout):
            return
        if data:
            self.buf = ESCAPES.sub("", self.buf + data.decode(errors="replace"))

    def expect(self, pattern: str, timeout: float) -> re.Match:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = re.search(pattern, self.buf, re.S | re.M)
            if m:
                self.buf = self.buf[m.end():]
                return m
            self._read()
        raise AssertionError(f"{pattern!r} not seen on the serial console; last output:\n"
                             f"{self.buf[-3000:]}")

    def send(self, text: str):
        self.sock.sendall(text.encode())

    def login(self, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            self.send("\r")
            try:
                self.expect(r"login: ?$|login:\s*$", 20)
                break
            except AssertionError:
                if time.monotonic() > deadline:
                    raise
        self.send("root\r")
        self.expect(r"Password:", 30)
        self.send(PASSWORD + "\r")
        self.expect(r"[#$] ?$|#\s*$", 60)
        self.send("export PS1='# '; stty -echo; export TERM=dumb SYSTEMD_PAGER=cat\r")
        time.sleep(1)
        self.buf = ""

    def run(self, cmd: str, timeout: float = 120) -> tuple[int, str]:
        tag = f"T{time.monotonic_ns()}"
        self.send(f"echo {tag}A; {cmd}; echo {tag}B $?\r")
        m = self.expect(rf"{tag}A\r?\n(.*?){tag}B (\d+)", timeout)
        return int(m.group(2)), m.group(1).replace("\r", "")


class Computer:
    """The outer QEMU: a PC that boots the appliance."""

    SCREEN = (1920, 1080)           # a typical laptop panel

    def __init__(self, image: Path, work: Path, uefi: bool = False):
        self.work = work
        self.serial_sock = work / "serial.sock"
        self.qmp_sock = work / "qmp.sock"
        stick = work / "stick.img"
        if not stick.exists():
            subprocess.run(["qemu-img", "create", "-f", "raw", str(stick), "64M"], check=True,
                           capture_output=True)
        argv = ["qemu-system-x86_64", "-machine", "q35", "-m", "3072", "-smp", "2",
                "-accel", "kvm" if KVM else "tcg",
                # Emulated CPUs advertise VMX/SVM that cannot actually run nested guests.
                "-cpu", "host" if KVM else "max,-vmx,-svm",
                "-device", "ahci,id=ahci",
                "-drive", f"if=none,id=appl,file={image},format=qcow2",
                "-device", "ide-hd,drive=appl,bus=ahci.0,bootindex=1",
                "-device", "qemu-xhci,id=xhci",
                "-drive", f"if=none,id=stick,file={stick},format=raw",
                "-device", "usb-storage,drive=stick,bus=xhci.0",
                # An absolute pointing device, so tests can put the pointer anywhere.
                "-device", "usb-tablet,bus=xhci.0",
                "-nic", "user,model=virtio-net-pci",
                # A sound card, so the appliance plays the VM's sound through ALSA.
                "-audiodev", "none,id=snd",
                "-device", "ich9-intel-hda", "-device", "hda-duplex,audiodev=snd",
                "-device", f"VGA,xres={self.SCREEN[0]},yres={self.SCREEN[1]}",
                "-display", "none",
                "-chardev", f"socket,id=ser,path={self.serial_sock},server=on,wait=off,"
                            f"logfile={work / 'serial.log'}",
                "-serial", "chardev:ser",
                "-qmp", f"unix:{self.qmp_sock},server=on,wait=off"]
        if uefi:
            code, vars_ = _ovmf()
            local = work / "OVMF_VARS.fd"
            shutil.copy(vars_, local)
            argv += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                     "-drive", f"if=pflash,format=raw,file={local}"]
        self.proc = subprocess.Popen(argv, stdout=open(work / "qemu.log", "w"),
                                     stderr=subprocess.STDOUT)
        self.serial = Serial(self.serial_sock, work / "serial.log")
        self.serial.connect()

    def qmp(self, cmd: str, args: dict | None = None):
        from rehome.vm.qmp import QMP
        with QMP(str(self.qmp_sock)).connect(retries=10) as q:
            return q.execute(cmd, args or {})

    def type(self, text: str) -> None:
        """Type on the computer's keyboard."""
        from rehome.vm.runner import _char_to_qcodes
        for ch in text:
            keys = [{"type": "qcode", "data": k} for k in _char_to_qcodes(ch)]
            self.qmp("send-key", {"keys": keys, "hold-time": 60})
            time.sleep(0.15)

    def screen_text(self, name: str) -> str:
        from tests.boot.watch import ocr
        png = self.work / f"{name}.png"
        self.qmp("screendump", {"filename": str(png), "format": "png"})
        return ocr(png)

    def picture_columns(self, name: str) -> tuple[int, int]:
        """First and last screen column showing the VM's picture (not the uniform side bars)."""
        from PIL import Image
        png = self.work / f"{name}.png"
        self.qmp("screendump", {"filename": str(png), "format": "png"})
        im = Image.open(png).convert("RGB")
        w, h = im.size
        bar = im.getpixel((0, h // 2))

        def is_bar(x):
            return all(im.getpixel((x, y)) == bar for y in range(0, h, 16))
        cols = [x for x in range(w) if not is_bar(x)]
        return (cols[0], cols[-1] + 1) if cols else (0, w)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)


def _ovmf() -> tuple[str, str]:
    from rehome.host import info
    host = info.gather()
    fw = info.choose_firmware(host, "x86_64", "q35", uefi=True)
    if fw is None or not fw.nvram_template:
        pytest.skip("no OVMF firmware with a variable store template")
    return fw.executable, fw.nvram_template


@pytest.fixture(scope="module")
def appliance(tmp_path_factory):
    # Share the downloads and base system between runs (and with the user's cache).
    os.environ.setdefault("REHOME_APPLIANCE_CACHE",
                          str(Path.home() / ".cache" / "rehome" / "appliance"))
    from rehome.appliance import build
    from rehome.vm import create, disks, library
    work = tmp_path_factory.mktemp("appliance")
    # Its own VM library: this module-wide fixture runs before the per-test one
    # (tests/conftest.py), and must not add VMs to the real library.
    env = pytest.MonkeyPatch()
    env.setenv("REHOME_HOME", str(work / "home"))
    try:
        img = work / "freedos.img"
        freedos.build(img)
        from rehome.inspect import gfs
        with gfs.session([gfs.Drive(str(img), "raw", False)]) as g:
            g.mount("/dev/sda1", "/")
            g.write("/VBE.COM", VBE_COM)
            g.write("/POWEROFF.COM", POWEROFF_COM)
            g.write("/MOUSE.COM", _mouse_com())
        disk = work / "disk.qcow2"
        disks.convert(str(img), str(disk), src_fmt="raw")
        cfg = create.create(create.NewVMSpec(name="FreeDOS Appliance", os="freedos1.3",
                                             existing_disk=str(disk), network="nat"))
        # The virtual hardware of the Windows XP profile: Cirrus graphics, AC97 sound,
        # USB 1.1 (UHCI) with a tablet, an RTL8139 network card on NAT.
        cfg.display.device = "cirrus-vga"
        cfg.sound.device = "AC97"
        cfg.usb.controller = "piix3-usb-uhci"
        cfg.input.pointer = "usb-tablet"
        for nic in cfg.nics:
            nic.model = "rtl8139"
        library.save(cfg)
        out = work / "appliance.qcow2"
        try:
            build.build(cfg, build.Options(output=out, fmt="qcow2",
                                           maintenance_password=PASSWORD,
                                           kernel_args="console=tty1 console=ttyS0,115200"))
        except RehomeError as e:            # the build's log is in the details
            pytest.fail(f"{e.message}\n{e.details}")
        yield cfg, out
    finally:
        env.undo()


def _inspect(path: Path) -> dict:
    from rehome.inspect import gfs
    with gfs.session([gfs.Drive(str(path), "qcow2", True)]) as g:
        parts = [(p["part_num"], g.part_get_gpt_type("/dev/sda", p["part_num"]))
                 for p in g.part_list("/dev/sda")]
        g.mount_ro("/dev/sda3", "/")
        g.mount_ro("/dev/sda2", "/boot/efi")
        return {"parts": parts, "uuid3": g.vfs_uuid("/dev/sda3"),
                "grub": g.cat("/boot/grub/grub.cfg"), "fstab": g.cat("/etc/fstab"),
                "efi": g.is_file("/boot/efi/EFI/BOOT/BOOTX64.EFI"),
                "bios_core": g.is_file("/boot/grub/i386-pc/core.img"),
                "conf": g.cat("/etc/rehome/appliance.toml"),
                "wrapper": g.is_file("/usr/local/bin/rehome")}


def test_image_layout(appliance):
    from rehome.appliance import build
    cfg, out = appliance
    info = _inspect(out)
    assert [t.upper() for _n, t in info["parts"]] == [build.GPT_BIOS, build.GPT_ESP,
                                                      build.GPT_ROOT_X86_64, build.GPT_LINUX_DATA]
    assert f"root=UUID={info['uuid3']}" in info["grub"]
    assert f"UUID={info['uuid3']}" in info["fstab"]
    assert info["efi"] and info["bios_core"] and info["wrapper"]
    assert cfg.id in info["conf"]


def _mouse_com() -> bytes:
    """tests/fixtures/mousecom.asm: shows where the PS/2 mouse moved a Windows-like cursor."""
    src = Path(__file__).resolve().parents[1] / "fixtures" / "mousecom.asm"
    out = subprocess.run(["nasm", "-f", "bin", "-o", "/dev/stdout", str(src)], check=True,
                         capture_output=True).stdout
    assert 100 < len(out) < 1000
    return out


def _boot_and_check(cfg, image: Path, work: Path, uefi: bool) -> Computer:
    pc = Computer(image, work, uefi=uefi)
    sh = pc.serial
    sh.login(timeout=600 * SLOW)
    deadline = time.monotonic() + 600 * SLOW
    while True:
        rc, _ = sh.run("test -e /run/rehome/vm-started")
        if rc == 0:
            break
        assert time.monotonic() < deadline, sh.run("systemctl status rehome-kiosk --no-pager; "
                                                   "journalctl -b -u rehome-kiosk --no-pager "
                                                   "| tail -40")[1]
        time.sleep(5)
    deadline = time.monotonic() + 600 * SLOW
    while True:
        rc, text = sh.run(f"rehome screen-text {cfg.id}")
        if freedos.MARKER in text:
            break
        assert time.monotonic() < deadline, text
        time.sleep(10)
    return pc


def test_boots_on_bios_and_runs_the_vm(appliance, tmp_path):
    cfg, out = appliance
    image = tmp_path / "bios.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(out), "-F", "qcow2",
                    str(image)], check=True, capture_output=True)
    subprocess.run(["qemu-img", "resize", str(image), "+2G"], check=True, capture_output=True)
    pc = _boot_and_check(cfg, image, tmp_path, uefi=False)
    try:
        sh = pc.serial
        # First boot: data partition grown, hardware recorded.
        rc, _ = sh.run("test -e /var/lib/rehome/appliance/firstboot.done && "
                       "test -s /var/lib/rehome/appliance/hardware.txt")
        assert rc == 0
        _, size = sh.run("df -B1 --output=size /var/lib/rehome | tail -1")
        assert int(size.strip()) > 2 * 2**30
        # The boot counts as good once the VM is on screen.
        _, env = sh.run("grub-editenv /boot/grub/grubenv list")
        assert "recordfail=1" not in env
        _, active = sh.run("systemctl is-active rehome-kiosk")
        assert active.strip() == "active"
        # The USB stick was given to the VM.
        deadline = time.monotonic() + 120 * SLOW
        while True:
            _, usb = sh.run(f"rehome usb list {cfg.id}")
            if "connected to the VM" in usb:
                break
            assert time.monotonic() < deadline, usb
            time.sleep(5)
        # The VM is on the real screen, full screen.
        text = pc.screen_text("bios-vm")
        if shutil.which("tesseract"):
            assert "FREEDOS" in text.upper() or "REHOME" in text.upper(), text
        # QEMU draws its window with OpenGL (SDL's 2D scaling breaks the pointer, D29).
        _, display = sh.run("grep -o -- '-display [^ ]*' /var/lib/rehome/data/vms/*/logs/qemu.log "
                            "| tail -1")
        assert "sdl,gl=on" in display, display
        # QEMU's own NAT (the VM starts before Wi-Fi is up; passt would hand out
        # link-local addresses), and the computer's sound card through ALSA.
        _, log = sh.run("tail -c 20000 /var/lib/rehome/data/vms/*/logs/qemu.log | "
                        "grep -o -- '-netdev [^ ]*\\|-audiodev [^ ]*' | tail -2")
        assert "-netdev user," in log and "-audiodev alsa," in log, log
        rc, _ = sh.run("pgrep -x passt")
        assert rc != 0
        # A 4:3 guest mode on the 16:9 screen: full height, centred, nothing cut off.
        sh.run(f"rehome type {cfg.id} vbe && rehome send-keys {cfg.id} ret")
        time.sleep(30 * SLOW)
        left, right = pc.picture_columns("bios-vesa")
        w, h = Computer.SCREEN
        expect = h * 4 // 3
        assert abs(left - (w - expect) // 2) <= 8 and abs(right - (w + expect) // 2) <= 8, \
            (left, right)
        _, alive = sh.run("pgrep -f '^/usr/bin/qemu-system' >/dev/null && echo alive")
        assert "alive" in alive
        # The power button reaches the appliance (FreeDOS ignores ACPI, so it keeps running).
        pc.qmp("system_powerdown")
        deadline = time.monotonic() + 60 * SLOW
        while True:
            _, j = sh.run("journalctl -b --no-pager | grep -c 'power button pressed' || true")
            if j.strip() not in ("", "0"):
                break
            assert time.monotonic() < deadline
            time.sleep(3)
        # The maintenance console, chosen for the next boot.
        sh.run("grub-editenv /boot/grub/grubenv set next_entry=maintenance")
        sh.send("systemctl reboot\r")
        time.sleep(10)
        sh.buf = ""
        sh.login(timeout=600 * SLOW)
        deadline = time.monotonic() + 120 * SLOW
        while True:
            _, st = sh.run("systemctl is-active rehome-maintenance")
            if st.strip() == "active":
                break
            assert time.monotonic() < deadline, st
            time.sleep(5)
        _, kiosk = sh.run("systemctl is-active rehome-kiosk")
        assert kiosk.strip() != "active"
        # This build has a maintenance password: log in on the screen like a person.
        time.sleep(10 * SLOW)
        pc.type("root\n")
        time.sleep(5 * SLOW)
        pc.type(PASSWORD + "\n")
        time.sleep(15 * SLOW)
        text = pc.screen_text("bios-maintenance")
        if shutil.which("tesseract"):
            assert "maintenance console" in text.lower() and "start the vm" in text.lower(), text
        # Start the VM from the menu.
        pc.type("1\n")
        deadline = time.monotonic() + 600 * SLOW
        while True:
            rc, _ = sh.run("test -e /run/rehome/vm-started")
            if rc == 0:
                break
            assert time.monotonic() < deadline
            time.sleep(10)
        # QEMU dies while the VM runs: the maintenance console says so, and which USB
        # devices the VM had.
        deadline = time.monotonic() + 120 * SLOW
        while True:
            _, usb = sh.run(f"rehome usb list {cfg.id}")
            if "connected to the VM" in usb:
                break
            assert time.monotonic() < deadline, usb
            time.sleep(5)
        rc, _ = sh.run("pkill -ABRT -f '^/usr/bin/qemu-system'")   # named after the VM
        assert rc == 0
        deadline = time.monotonic() + 180 * SLOW
        while True:
            _, st = sh.run("systemctl is-active rehome-maintenance")
            if st.strip() == "active":
                break
            assert time.monotonic() < deadline, st
            time.sleep(5)
        _, msg = sh.run("cat /run/rehome/console-message")
        assert "QEMU, the program that runs it, quit" in msg, msg
        assert "USB devices the VM had" in msg, msg
    finally:
        pc.stop()


def test_boots_on_uefi(appliance, tmp_path):
    cfg, out = appliance
    image = tmp_path / "uefi.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(out), "-F", "qcow2",
                    str(image)], check=True, capture_output=True)
    pc = _boot_and_check(cfg, image, tmp_path, uefi=True)
    try:
        sh = pc.serial
        # The build computer's time zone, and internet time.
        _, zone = sh.run("cat /etc/timezone; readlink /etc/localtime")
        from rehome.appliance import build
        expect = build.host_timezone() or "Etc/UTC"
        assert expect in zone and f"/usr/share/zoneinfo/{expect}" in zone, zone
        rc, _ = sh.run("systemctl is-enabled systemd-timesyncd")
        assert rc == 0
        # The QXL display (Windows XP after its guest tools): the appliance's QEMU has it.
        sh.run(f"rehome force-off {cfg.id}")
        deadline = time.monotonic() + 180 * SLOW
        while True:
            _, st = sh.run("systemctl is-active rehome-maintenance")
            if st.strip() == "active":
                break
            assert time.monotonic() < deadline, st
            time.sleep(5)
        rc, out = sh.run(f"rehome set {cfg.id} --display qxl-vga")
        assert rc == 0, out
        sh.run("rm -f /run/rehome/vm-started; systemctl --no-block start rehome-kiosk")
        deadline = time.monotonic() + 600 * SLOW
        while True:
            rc, text = sh.run(f"rehome screen-text {cfg.id}")
            if freedos.MARKER in text:
                break
            assert time.monotonic() < deadline, text
            time.sleep(10)
        _, display = sh.run("grep -o -- '-device qxl-vga[^ ]*' "
                            "/var/lib/rehome/data/vms/*/logs/qemu.log | tail -1")
        assert "qxl-vga" in display, display
        sh.run(f"rehome type {cfg.id} vbe && rehome send-keys {cfg.id} ret")
        time.sleep(30 * SLOW)
        left, right = pc.picture_columns("uefi-qxl-vesa")
        w, h = Computer.SCREEN
        expect_w = h * 4 // 3
        assert abs(left - (w - expect_w) // 2) <= 8 and abs(right - (w + expect_w) // 2) <= 8, \
            (left, right)
        _, alive = sh.run("pgrep -f '^/usr/bin/qemu-system' >/dev/null && echo alive")
        assert "alive" in alive
        # The guest shuts down (ACPI, like Windows): the computer turns off, instead of
        # the maintenance console reporting a crash (QEMU closes its socket right after
        # its SHUTDOWN event).
        sh.run(f"rehome type {cfg.id} poweroff && rehome send-keys {cfg.id} ret")
        deadline = time.monotonic() + 300 * SLOW
        while pc.proc.poll() is None:
            assert time.monotonic() < deadline, open(tmp_path / "serial.log", errors="replace").read()[-3000:]
            time.sleep(3)
    finally:
        pc.stop()


def _put(sh, text: str, path: str) -> None:
    """Write a file in the appliance through its serial console (in short lines)."""
    data = base64.b64encode(text.encode()).decode()
    rc, out = sh.run(f"rm -f {path}.b64")
    for i in range(0, len(data), 1000):
        rc, out = sh.run(f"echo {data[i:i + 1000]} >> {path}.b64")
        assert rc == 0, out
    rc, out = sh.run(f"base64 -d {path}.b64 > {path}")
    assert rc == 0, out


def test_a_vm_with_only_a_ps2_mouse_gets_the_computers_mouse(appliance, tmp_path):
    """Regression: in a Windows 98 appliance the mouse hit "invisible walls". cage cannot
    lock the pointer, so the hidden pointer stopped at the screen's edges while the
    guest's own cursor was elsewhere. A VM with only a PS/2 mouse now gets the computer's
    mice through QEMU's input-linux: every movement reaches the guest, at any position.
    Touchpads (D46) go through libinput to the VM's PS/2 mouse: no walls either, and
    tapping clicks."""
    cfg, out = appliance
    image = tmp_path / "mouse.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(out), "-F", "qcow2",
                    str(image)], check=True, capture_output=True)
    pc = _boot_and_check(cfg, image, tmp_path, uefi=False)

    def pointer_pixels(name):
        """Bright pixels where cage puts its pointer at first: the middle of the screen."""
        from PIL import Image
        png = tmp_path / f"{name}.png"
        pc.qmp("screendump", {"filename": str(png), "format": "png"})
        im = Image.open(png).convert("RGB")
        cx, cy = im.size[0] // 2, im.size[1] // 2
        return [(x, y) for x in range(cx - 8, cx + 32) for y in range(cy - 8, cy + 40)
                if min(im.getpixel((x, y))) >= 0xE0]
    try:
        before = len(pointer_pixels("pointer-tablet"))      # for comparison: not checked
        sh = pc.serial
        # The VM gets a PS/2 mouse only, like Windows 98: turned off with Rehome's tools
        # (the maintenance console opens), then started again as its menu's "Start the VM"
        # does (typing at it depends on whether it asks for the password first).
        rc, n = sh.run("sed -i 's/^pointer = \"usb-tablet\"/pointer = \"ps2\"/' "
                       "/var/lib/rehome/data/vms/*/vm.toml && "
                       "grep -c '^pointer = \"ps2\"' /var/lib/rehome/data/vms/*/vm.toml")
        assert rc == 0 and n.strip() == "1", n
        rc, out = sh.run(f"rm -f /run/rehome/vm-started; rehome force-off {cfg.id}")
        assert rc == 0, out
        deadline = time.monotonic() + 180 * SLOW
        while True:
            _, st = sh.run("systemctl is-active rehome-maintenance")
            if st.strip() == "active":
                break
            assert time.monotonic() < deadline, st
            time.sleep(5)
        rc, out = sh.run("rm -f /run/rehome/console-message; "
                         # every batch the touchpad agent sends (touchpad.py), for failures
                         "systemctl set-environment REHOME_TOUCHPAD_TRACE=/tmp/tp-trace.jsonl; "
                         "systemctl --no-block start rehome-kiosk.service")
        assert rc == 0, out
        deadline = time.monotonic() + 600 * SLOW
        while True:
            _, mice = sh.run("journalctl -b --no-pager | grep 'mouse event.* given to the VM' "
                             "|| true")
            if "mouse event" in mice:
                break
            assert time.monotonic() < deadline, sh.run("journalctl -b --no-pager | tail -40")[1]
            time.sleep(10)
        deadline = time.monotonic() + 600 * SLOW
        while True:
            _, text = sh.run(f"rehome screen-text {cfg.id}")
            if freedos.MARKER in text:
                break
            assert time.monotonic() < deadline, text
            time.sleep(10)
        sh.run(f"rehome type {cfg.id} mouse && rehome send-keys {cfg.id} ret")

        def where():
            deadline = time.monotonic() + 120 * SLOW
            last = None
            while True:                       # until it stops changing
                time.sleep(5 * SLOW)
                _, text = sh.run(f"rehome screen-text {cfg.id}")
                m = re.search(r"MOUSE X=(\d+) Y=(\d+) N=(\d+) L=(\d+) R=(\d+)( B=\d+)?", text)
                now = tuple(int(v) for v in m.groups()[:5]) if m else None
                with open(tmp_path / "mouse-samples.txt", "a") as f:   # for failures
                    f.write(f"{time.strftime('%H:%M:%S')} {m.group(0) if m else None}\n")
                if now and now == last:
                    return now
                assert time.monotonic() < deadline, text
                last = now

        def move(dx, dy, times):              # the computer's own PS/2 mouse
            for _ in range(times):
                pc.qmp("input-send-event", {"events": [
                    {"type": "rel", "data": {"axis": "x", "value": dx}},
                    {"type": "rel", "data": {"axis": "y", "value": dy}}]})
                time.sleep(0.05)
        x, y, n, *_ = where()
        assert (x, y) == (320, 240), (x, y)       # n may count an empty packet already
        def kernel():                         # the appliance's PS/2 driver, for failures
            return sh.run("journalctl -k -b --no-pager | grep -i psmouse | tail -5")[1]
        move(5, 0, 20)
        x, y, n2, *_ = where()
        # Nested emulation is slow enough for the appliance's PS/2 driver to drop some
        # packets ("lost synchronization"); on a real computer the mouse is real.
        assert n2 > n and x > 320 and y == 240, (x, y, n2, kernel())
        # No wall anywhere: big moves reach every edge.
        move(40, 0, 60)
        assert where()[:2] == (639, 240), kernel()
        move(-40, 30, 60)
        assert where()[:2] == (0, 479), kernel()
        move(25, -40, 60)
        x, y, *_ = where()
        assert y == 0 and x > 300, (x, y, kernel())

        # cage's pointer: nothing moves it (every mouse and touchpad goes to the VM; the
        # outer machine's tablet stays still), so it must be invisible, not a cursor stuck
        # in the middle of the screen. wlroots draws it into the picture on this VGA.
        _, env = sh.run("tr '\\0' '\\n' < /proc/$(pidof cage)/environ | grep ^XCURSOR_PATH= "
                        "|| true")
        assert "rehome-cursors" in env, env
        bright = pointer_pixels("pointer-ps2")
        print(f"cage's pointer: {before} bright pixels at the middle with a tablet VM, "
              f"{len(bright)} with a PS/2 mouse")
        assert not bright, bright[:10]

        # The touchpad alone from here. The outer machine's PS/2 mouse leaves (its driver is
        # unbound, so the mouse agent takes it from the VM), and MOUSE.COM starts again, with
        # its counters at zero and the BIOS's packet assembly reset. Under nested emulation
        # the outer mouse's button counts cannot be trusted: one run counted 5518 right-button
        # presses during the moves above, with no button pressed.
        # QEMU's own record of the mouse input it gets (with its source: -1 for QMP, the
        # touchpad agent; 0 for QEMU's window) and the PS/2 packets it sends, for failures.
        _put(sh, (Path(__file__).resolve().parents[1] / "fixtures" / "qemu_hmp.py").read_text(),
             "/root/qemu_hmp.py")
        rc, out = sh.run("python3 /root/qemu_hmp.py 'trace-event input_event_rel on' "
                         "'trace-event input_event_btn on' 'trace-event ps2_mouse_send_packet on'")
        assert rc == 0, out
        # QEMU's PS/2 queue holds five packets. What did not fit while this slow, nested
        # guest read the moves above stays in QEMU until the next input event, also across
        # the guest turning the mouse off and on, and came out with the touchpad's first
        # move. Empty it while MOUSE.COM still reads: empty moves until QEMU's packets are 0.
        deadline = time.monotonic() + 300 * SLOW
        while True:
            sh.run("python3 /root/qemu_hmp.py 'mouse_move 0 0'")
            time.sleep(2 * SLOW)
            _, last = sh.run("grep -a ps2_mouse_send_packet /var/lib/rehome/data/vms/*/logs/"
                             "qemu.log | tail -1")
            if " x 0 y 0 " in last:
                break
            assert time.monotonic() < deadline, last
        rc, out = sh.run("echo -n serio1 > /sys/bus/serio/drivers/psmouse/unbind")
        assert rc == 0, out
        deadline = time.monotonic() + 120 * SLOW
        while True:
            _, gone = sh.run("journalctl -b --no-pager | grep 'mouse event.* removed from the VM' "
                             "|| true")
            if "removed" in gone:
                break
            assert time.monotonic() < deadline, sh.run("journalctl -b --no-pager | tail -30")[1]
            time.sleep(5)
        sh.run(f"rehome send-keys {cfg.id} ret")      # MOUSE.COM ends on a key
        time.sleep(10 * SLOW)
        sh.run(f"rehome type {cfg.id} mouse && rehome send-keys {cfg.id} ret")
        assert where()[:2] == (320, 240)

        # A touchpad: uinput makes one (the outer machine has none), with real multi-touch
        # events, so libinput handles it as a laptop's.
        _put(sh, (Path(__file__).resolve().parents[1] / "fixtures" / "uinput_touchpad.py")
             .read_text(), "/root/touchpad.py")
        rc, out = sh.run("rm -f /run/tp && mkfifo /run/tp && "
                         "(setsid python3 /root/touchpad.py /run/tp >/run/tp.log 2>&1 &)")
        assert rc == 0, out
        deadline = time.monotonic() + 300 * SLOW
        while True:
            _, found = sh.run("journalctl -b --no-pager | grep \"moves the VM's mouse\" || true")
            if "Rehome test touchpad" in found:
                break
            assert time.monotonic() < deadline, sh.run(
                "cat /run/tp.log; journalctl -b --no-pager | tail -40")[1]
            time.sleep(5)

        def touch(gesture, times=1):
            for _ in range(times):
                _, before = sh.run("grep -c done /run/tp.log || true")
                rc, out = sh.run(f"echo '{gesture}' > /run/tp", timeout=60 * SLOW)
                assert rc == 0, out
                deadline = time.monotonic() + 60 * SLOW
                while True:
                    _, now = sh.run("grep -c done /run/tp.log || true")
                    if int(now.strip() or 0) > int(before.strip() or 0):
                        break
                    assert time.monotonic() < deadline, sh.run("cat /run/tp.log")[1]
                    time.sleep(1)

        x0, y0, _n, lefts, rights = where()      # the middle, and no button pressed yet
        assert (x0, y0, lefts, rights) == (320, 240, 0, 0), (x0, y0, lefts, rights)

        def trace():
            return sh.run("tail -c 2000 /tmp/tp-trace.jsonl; grep -a 'input_event\\|ps2_mouse' "
                          "/var/lib/rehome/data/vms/*/logs/qemu.log | tail -40")[1]
        # A swipe to the left moves left only.
        touch("swipe -1200 0")
        x, y, *_ = where()
        assert x < x0 and y == y0, (x0, y0, x, y, trace())
        # Each edge in turn, so the directions are right too, and no wall on the way.
        touch("swipe -3000 0", 6)
        assert where()[0] == 0
        touch("swipe 3000 2200", 6)
        assert where()[:2] == (639, 479)
        touch("swipe 0 -2000", 6)               # clear of the clickpad's button area
        assert where()[1] == 0
        # Tap to click, and a two-finger tap for the right button.
        touch("tap")
        assert where()[3:] == (lefts + 1, rights)
        touch("tap2")
        assert where()[3:] == (lefts + 1, rights + 1)
    finally:
        pc.stop()


def _stick_with_files(path: Path) -> None:
    """A USB stick image: one FAT32 partition labelled STICK, with a CD image on it."""
    from rehome.inspect import gfs
    subprocess.run(["qemu-img", "create", "-f", "raw", str(path), "256M"], check=True,
                   capture_output=True)
    with gfs.session([gfs.Drive(str(path), "raw", False)]) as g:
        g.part_init("/dev/sda", "mbr")
        g.part_add("/dev/sda", "p", 2048, -1)
        g.part_set_mbr_id("/dev/sda", 1, 0x0C)
        g.mkfs("vfat", "/dev/sda1", label="STICK")
        g.mount("/dev/sda1", "/")
        g.mkdir("/Drivers")
        g.write("/Drivers/DRIVERS.ISO", b"CD001" * 4096)


def _wait_for(sh, cmd: str, want, timeout: float, every: float = 5) -> str:
    deadline = time.monotonic() + timeout
    while True:
        _, out = sh.run(cmd)
        if want(out):
            return out
        assert time.monotonic() < deadline, (cmd, out)
        time.sleep(every)


def test_maintenance_console(appliance, tmp_path):
    """The start screen does not mention the maintenance console, but M opens it; its
    pages back the VM up to a USB stick and restore it, take CD images from the stick,
    and keep startup, screen, sound and password settings across a restart (D42)."""
    import base64
    cfg, out = appliance
    image = tmp_path / "console.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(out), "-F", "qcow2",
                    str(image)], check=True, capture_output=True)
    _stick_with_files(tmp_path / "stick.img")
    pc = _boot_and_check(cfg, image, tmp_path, uefi=False)
    try:
        sh = pc.serial
        # The console's pages, run from the serial shell with typed answers.
        helper = ("export PYTHONPATH=/opt/rehome REHOME_DATA_DIR=/opt/rehome/data "
                  "REHOME_HOME=/var/lib/rehome REHOME_RUNTIME_DIR=/run/rehome REHOME_APPLIANCE=1\n"
                  "exec python3 -c 'import sys\n"
                  "from rehome.appliance import console, maintenance, runtime\n"
                  "conf = runtime.load_config()\n"
                  "exec(sys.argv[1])' \"$1\"\n")
        sh.run(f"echo {base64.b64encode(helper.encode()).decode()} | base64 -d > /tmp/console-do")

        def page(code: str, answers: str = "") -> str:
            arg = base64.b64encode(code.encode()).decode()
            rc, text = sh.run(f"printf '{answers}' | sh /tmp/console-do "
                              f"\"$(echo {arg} | base64 -d)\" 2>&1", timeout=900 * SLOW)
            assert rc == 0, text
            return text

        def maintenance_open():
            _wait_for(sh, "systemctl is-active rehome-maintenance", lambda s: s.strip() == "active",
                      300 * SLOW)

        def vm_id() -> str:
            return sh.run("sed -n 's/^vm = \"\\(.*\\)\"$/\\1/p' /etc/rehome/appliance.toml")[1].strip()

        # Long enough to press M in an emulated computer.
        sh.run(f"rm -f /run/rehome/vm-started; rehome force-off {cfg.id}")
        maintenance_open()
        page("console.startup_and_shutdown(conf)", "2\\n60\\n\\n")
        _, st = sh.run("cat /var/lib/rehome/appliance/settings.toml; "
                       "stat -c %U /var/lib/rehome/appliance/settings.toml")
        assert "delay = 60" in st and st.strip().endswith("rehome"), st
        sh.run("rm -f /run/rehome/console-message /run/rehome/limits.json; "
               "systemctl --no-block start rehome-kiosk.service")
        _wait_for(sh, "test -e /run/rehome/limits.json && echo yes", lambda s: "yes" in s,
                  300 * SLOW, every=2)
        time.sleep(4 * SLOW)
        text = pc.screen_text("starting").lower()
        assert "starting" in text, text
        assert "maintenance" not in text and "press" not in text, text
        pc.type("m")
        maintenance_open()
        rc, _ = sh.run("test -e /run/rehome/vm-started")
        assert rc != 0                                  # the VM never started

        # Back up to the stick (the VM gave it back when it stopped).
        _wait_for(sh, "blkid -L STICK || true", lambda s: "/dev/" in s, 120 * SLOW)
        text = page("console.back_up(conf)", "1\\n")
        assert "Saved \"Rehome backups/" in text, text
        stick = sh.run("blkid -L STICK")[1].strip()
        _, files = sh.run(f"mkdir -p /mnt/t && mount -o ro {stick} /mnt/t && "
                          "ls -l '/mnt/t/Rehome backups'; umount /mnt/t")
        assert ".rehome" in files, files

        # A CD image from the stick goes into the VM's drive.
        page("console.insert_cd(console._the_vm(conf))", "1\\n1\\n")
        _, vm = sh.run("grep -h DRIVERS.ISO /var/lib/rehome/data/vms/*/vm.toml; "
                       "stat -c %U /var/lib/rehome/data/vms/*/media/DRIVERS.ISO")
        assert "media/DRIVERS.ISO" in vm and vm.strip().endswith("rehome"), vm

        # Restore the backup: a new VM replaces the old one, and starts.
        old = vm_id()
        text = page("console.restore(conf)", "1\\n1\\nyes\\n")
        assert "Restored" in text, text
        new = vm_id()
        assert new and new != old
        _, vms = sh.run("ls /var/lib/rehome/data/vms; stat -c %U /var/lib/rehome/data/vms/*/vm.toml")
        assert len([v for v in vms.split() if v != "rehome"]) == 1 and "rehome" in vms, vms
        # The kiosk runs as "rehome": the rewritten configuration stays readable to it.
        rc, text = sh.run("runuser -u rehome -- cat /etc/rehome/appliance.toml")
        assert rc == 0 and new in text, text

        # When the VM shuts down: the maintenance console instead of turning off.
        page("console.startup_and_shutdown(conf)", "2\\n3\\n3\\n3\\n\\n")
        sh.run("rm -f /run/rehome/vm-started /run/rehome/console-message; "
               "systemctl --no-block start rehome-kiosk.service")
        started = _wait_for(sh, "test -e /run/rehome/vm-started && echo started || "
                                "cat /run/rehome/console-message 2>/dev/null || true",
                            lambda s: s.strip() != "", 600 * SLOW, every=10)
        assert "started" in started, started          # else: why the VM did not start
        _wait_for(sh, f"rehome screen-text {new}", lambda s: freedos.MARKER in s, 600 * SLOW,
                  every=10)
        sh.run(f"rehome type {new} poweroff && rehome send-keys {new} ret")
        maintenance_open()
        _, msg = sh.run("cat /run/rehome/console-message")
        assert "was shut down" in msg, msg
        assert pc.proc.poll() is None                   # the computer is still on

        # Settings kept across a restart: basic graphics, the console at power-on, the
        # sound volume, the password.
        page("console.screen(conf)", "1\\n2\\n\\n")
        page("console.startup_and_shutdown(conf)", "1\\n\\n")
        page("maintenance.set_password(" + repr(PASSWORD) + ", conf)")
        _, env = sh.run("grub-editenv /boot/grub/grubenv list; "
                        "cat /var/lib/rehome/appliance/settings.toml")
        assert "rehome_default=vm-basic" in env and "gl = false" in env, env
        assert 'open = "maintenance"' in env, env
        _, control = sh.run("amixer scontrols | sed -n \"1s/^Simple mixer control '\\(.*\\)',.*/\\1/p\"")
        control = control.strip()
        assert control, sh.run("amixer scontrols; cat /proc/asound/cards")[1]
        sh.run(f"amixer -q sset '{control}' 37% && alsactl store")
        _, volume = sh.run(f"amixer sget '{control}' | grep -o '[0-9]* \\[[0-9]*%\\]' | head -1")
        assert "%" in volume, volume
        menu = page("import threading, os\n"
                    "threading.Timer(15, lambda: (sys.stdout.flush(), os._exit(0))).start()\n"
                    "console.main()")
        assert "Back up or restore the VM" in menu and "Power off" in menu, menu
        assert "Disk disk0" in page("console.storage(conf)"), "storage page"
        sh.send("systemctl reboot\r")
        time.sleep(10)
        sh.buf = ""
        sh.login(timeout=600 * SLOW)
        maintenance_open()
        _, after = sh.run("cat /proc/cmdline; systemctl is-active rehome-kiosk; "
                          "grub-editenv /boot/grub/grubenv list; "
                          f"amixer sget '{control}' | grep -o '[0-9]* \\[[0-9]*%\\]' | head -1")
        assert "rehome.graphics=basic" in after and "rehome.mode=vm" in after, after
        assert "\nactive" not in after and "recordfail=1" not in after, after
        assert volume.strip() in after, (volume, after)
    finally:
        pc.stop()
