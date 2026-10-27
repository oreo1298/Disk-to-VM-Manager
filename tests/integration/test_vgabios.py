"""Rehome's VGA BIOS for Windows 9x VMs (data/firmware, D43), against a boot sector
that calls an unimplemented video function the way Windows 98 does when it shuts
down (tests/fixtures/vgastub.asm)."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not shutil.which("nasm"), reason="nasm is not installed"),
              pytest.mark.skipif(not shutil.which("qemu-system-i386"),
                                 reason="qemu-system-i386 is not installed")]

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "data" / "firmware"
# Where distributions put QEMU's own SeaVGABIOS files.
STOCK_DIRS = [os.environ.get("REHOME_TEST_STOCK_VGABIOS_DIR", ""), "/usr/share/seabios",
              "/usr/share/qemu"]


@pytest.fixture(scope="module")
def stub_disk(tmp_path_factory):
    img = tmp_path_factory.mktemp("vgastub") / "vgastub.img"
    subprocess.run(["nasm", "-f", "bin", "-o", str(img),
                    str(ROOT / "tests" / "fixtures" / "vgastub.asm")], check=True)
    with open(img, "r+b") as f:
        f.truncate(2**20)
    return img


def run_stub(disk: Path, device: str, rom: Path, work: Path) -> tuple[int, int]:
    """(bytes the VGA BIOS wrote outside the caller's stack segment, AL it returned)."""
    serial = work / f"serial-{device}-{rom.parent.name}.txt"
    proc = subprocess.Popen(["qemu-system-i386", "-machine", "pc", "-accel", "tcg", "-m", "32",
                             "-nodefaults", "-device", f"{device},romfile={rom}",
                             "-drive", f"file={disk},format=raw,if=ide",
                             "-serial", f"file:{serial}", "-display", "none"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            text = serial.read_text(errors="replace") if serial.exists() else ""
            m = re.search(r"VGASTUB OUT=(\d+) AL=([0-9A-F]{2})", text)
            if m:
                return int(m.group(1)), int(m.group(2), 16)
            assert proc.poll() is None, proc.stdout.read().decode(errors="replace")
            time.sleep(0.5)
        raise AssertionError("the test program did not report")
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("device, name", [("cirrus-vga", "vgabios-cirrus.bin"),
                                          ("VGA", "vgabios-stdvga.bin")])
def test_rehomes_vga_bios_stays_on_the_callers_stack(stub_disk, tmp_path, device, name):
    rom = FIRMWARE / name
    if not rom.is_file():
        pytest.skip(f"{rom} is not built (scripts/build_vgabios.py)")
    assert b"stub %s:%d" not in rom.read_bytes()
    assert run_stub(stub_disk, device, rom, tmp_path) == (0, 0x12)


@pytest.mark.parametrize("device, name", [("cirrus-vga", "vgabios-cirrus.bin"),
                                          ("VGA", "vgabios-stdvga.bin")])
def test_qemus_own_vga_bios_writes_outside_the_stack(stub_disk, tmp_path, device, name):
    """The bug: SeaVGABIOS built with debug output (as distributions ship it)."""
    roms = [Path(d) / name for d in STOCK_DIRS if d and (Path(d) / name).is_file()]
    roms = [r for r in roms if b"stub %s:%d" in r.read_bytes()]
    if not roms:
        pytest.skip("no SeaVGABIOS with debug output is installed")
    outside, _al = run_stub(stub_disk, device, roms[0], tmp_path)
    assert outside > 0


def test_a_windows_98_vm_starts_with_rehomes_vga_bios(stub_disk, tmp_path):
    """Through Rehome: a VM with the Windows 98 profile gets the ROM (and the guest's
    video calls stay on its stack)."""
    rom = FIRMWARE / "vgabios-cirrus.bin"
    if not rom.is_file():
        pytest.skip(f"{rom} is not built (scripts/build_vgabios.py)")
    from rehome.vm import create, disks, library, runner
    img = tmp_path / "stub.qcow2"
    disks.convert(str(stub_disk), str(img), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="Windows 98 stub", os="win98",
                                         existing_disk=str(img), network="none"))
    cfg.machine.accel = "tcg"
    library.save(cfg)
    assert cfg.os.family == "win9x" and cfg.display.device == "cirrus-vga"
    runner.start(cfg)
    try:
        deadline = time.monotonic() + 120
        while True:
            m = re.search(r"VGASTUB OUT=(\d+) AL=([0-9A-F]{2})", runner.screen_text(cfg))
            if m:
                break
            assert time.monotonic() < deadline, runner.screen_text(cfg)
            time.sleep(1)
        assert (int(m.group(1)), m.group(2)) == (0, "12")
        log = (cfg.dir / "logs" / "qemu.log").read_text()
        assert f"romfile={rom}" in log
    finally:
        runner.force_off(cfg)
