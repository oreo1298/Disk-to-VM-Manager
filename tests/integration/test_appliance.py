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
    img = work / "freedos.img"
    freedos.build(img)
    disk = work / "disk.qcow2"
    disks.convert(str(img), str(disk), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="FreeDOS Appliance", os="freedos1.3",
                                         existing_disk=str(disk), network="none"))
    cfg.usb.controller = "piix3-usb-uhci"       # the DOS profile has none; USB is tested here
    library.save(cfg)
    out = work / "appliance.qcow2"
    build.build(cfg, build.Options(output=out, fmt="qcow2", maintenance_password=PASSWORD,
                                   kernel_args="console=tty1 console=ttyS0,115200"))
    return cfg, out


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
    finally:
        pc.stop()


def test_boots_on_uefi(appliance, tmp_path):
    cfg, out = appliance
    image = tmp_path / "uefi.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(out), "-F", "qcow2",
                    str(image)], check=True, capture_output=True)
    pc = _boot_and_check(cfg, image, tmp_path, uefi=True)
    pc.stop()
