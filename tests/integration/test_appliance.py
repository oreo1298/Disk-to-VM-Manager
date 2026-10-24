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

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

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
        build.build(cfg, build.Options(output=out, fmt="qcow2", maintenance_password=PASSWORD,
                                       kernel_args="console=tty1 console=ttyS0,115200"))
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


def test_a_vm_with_only_a_ps2_mouse_gets_the_computers_mouse(appliance, tmp_path):
    """Regression: in a Windows 98 appliance the mouse hit "invisible walls". cage cannot
    lock the pointer, so the hidden pointer stopped at the screen's edges while the
    guest's own cursor was elsewhere. A VM with only a PS/2 mouse now gets the computer's
    mice through QEMU's input-linux: every movement reaches the guest, at any position."""
    cfg, out = appliance
    image = tmp_path / "mouse.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(out), "-F", "qcow2",
                    str(image)], check=True, capture_output=True)
    pc = _boot_and_check(cfg, image, tmp_path, uefi=False)
    try:
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
                m = re.search(r"MOUSE X=(\d+) Y=(\d+) N=(\d+)", text)
                now = tuple(int(v) for v in m.groups()) if m else None
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
        x, y, n = where()
        assert (x, y) == (320, 240), (x, y)       # n may count an empty packet already
        def kernel():                         # the appliance's PS/2 driver, for failures
            return sh.run("journalctl -k -b --no-pager | grep -i psmouse | tail -5")[1]
        move(5, 0, 20)
        x, y, n2 = where()
        # Nested emulation is slow enough for the appliance's PS/2 driver to drop some
        # packets ("lost synchronization"); on a real computer the mouse is real.
        assert n2 > n and x > 320 and y == 240, (x, y, n2, kernel())
        # No wall anywhere: big moves reach every edge.
        move(40, 0, 60)
        assert where()[:2] == (639, 240), kernel()
        move(-40, 30, 60)
        assert where()[:2] == (0, 479), kernel()
        move(25, -40, 60)
        x, y, _ = where()
        assert y == 0 and x > 300, (x, y, kernel())
    finally:
        pc.stop()
