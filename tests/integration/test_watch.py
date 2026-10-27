"""Windows 9x VMs turn off once the guest has stopped for good (rehome/vm/watch.py, D45)."""

import shutil
import subprocess
import time

import pytest

from tests.fixtures import freedos

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not shutil.which("nasm"), reason="nasm is not installed")]

# Prints a line on the screen and COM1, then stops the way Windows 9x leaves the
# processor after shutting down without APM: real mode, interrupts off.
STOP = """
        bits 16
        org 0x7C00
        xor ax, ax
        mov ds, ax
        mov si, msg
next:   lodsb
        or al, al
        jz done
        mov ah, 0x0E
        xor bx, bx
        int 0x10
        mov dx, 0x3F8
        out dx, al
        jmp next
done:   cli
        {end}
msg     db "SAFE TO TURN OFF", 13, 10, 0
        times 510 - ($ - $$) db 0
        dw 0xAA55
"""


def _vm(tmp_path, end: str = "", image=None):
    from rehome.vm import create, disks, library
    if image is None:
        src = tmp_path / "stop.asm"
        src.write_text(STOP.format(end=end))
        raw = tmp_path / "stop.img"
        subprocess.run(["nasm", "-f", "bin", "-o", str(raw), str(src)], check=True)
        with open(raw, "r+b") as f:
            f.truncate(2**20)
        image = raw
    disk = tmp_path / "disk.qcow2"
    disks.convert(str(image), str(disk), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="Windows 98 end", os="win98",
                                         existing_disk=str(disk), network="none"))
    cfg.machine.accel = "tcg"
    library.save(cfg)
    assert cfg.os.family == "win9x"
    return cfg


def _wait_stopped(cfg, timeout: float) -> bool:
    from rehome.vm import runner
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not runner.is_running(cfg):
            return True
        time.sleep(0.5)
    return False


@pytest.mark.parametrize("end", ["hlt\n        jmp done", "jmp $"])
def test_a_windows_98_vm_turns_off_once_the_guest_stopped_for_good(tmp_path, end):
    from rehome.vm import runner
    cfg = _vm(tmp_path, end)
    runner.start(cfg)
    try:
        assert _wait_stopped(cfg, 60), (cfg.dir / "logs" / "watch.log").read_text()
    finally:
        if runner.is_running(cfg):
            runner.force_off(cfg)
    log = (cfg.dir / "logs" / "watch.log").read_text()
    assert "real mode at 0000:00007C" in log and "interrupts off" in log, log
    assert "turning the VM off" in log and "quitting" not in log, log
    assert "SAFE TO TURN OFF" in (cfg.dir / "logs" / "serial.log").read_text()


def test_a_dos_prompt_is_left_alone(tmp_path):
    """Interrupts on, waiting for keys: never turned off (Restart in MS-DOS mode)."""
    if freedos.available():
        pytest.skip(f"cannot build FreeDOS: {freedos.available()}")
    from rehome.vm import diagnostics, runner
    img = tmp_path / "freedos.img"
    freedos.build(img)
    cfg = _vm(tmp_path, image=img)
    runner.start(cfg)
    try:
        deadline = time.monotonic() + 300
        while freedos.MARKER not in runner.screen_text(cfg):
            assert time.monotonic() < deadline, runner.screen_text(cfg)
            time.sleep(2)
        time.sleep(15)
        assert runner.is_running(cfg), (cfg.dir / "logs" / "watch.log").read_text()
        log = (cfg.dir / "logs" / "watch.log").read_text()
        assert "interrupts on" in log and "turning the VM off" not in log, log
        # The diagnostics show the processor too (and the watcher's log).
        report = diagnostics.collect(cfg)
        assert "Processor (two samples" in report and "Last watch.log lines" in report
    finally:
        runner.force_off(cfg)
