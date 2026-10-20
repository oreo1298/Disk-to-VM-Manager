"""Decide whether a booting guest reached a login/desktop or failed.

Signals, cheapest first: the serial log, VGA text memory (DOS, BIOS,
text-mode blue screens) and OCR of a screenshot (needs `tesseract`).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Text that shows the system reached a usable state, per OS family.
SUCCESS = {
    "windows": [r"Recycle\s*Bin", r"Ctrl\s*\+?\s*Alt\s*\+?\s*Del", r"\bWelcome\b", r"Log\s*on",
                r"Sign[\s-]*in", r"Other\s+user", r"My\s+Computer", r"\bThis\s+PC\b"],
    "win9x": [r"Recycle\s*Bin", r"My\s+Computer", r"Enter\s+Windows\s+Password",
              r"Welcome\s+to\s+Windows"],
    "dos": [r"^[A-Z]:\\[^\n]*>", r"\n[A-Z]:\\[^\n]*>"],
    "linux": [r"\blogin:", r"Password:", r"\bActivities\b", r"Log\s*In\b"],
    "bsd": [r"\blogin:"],
}
SUCCESS["other"] = SUCCESS["linux"] + SUCCESS["windows"]

# Text that means the boot failed; checked before the success patterns.
FAILURE = [r"STOP:\s*0x", r"\*\*\*\s*STOP", r"INACCESSIBLE_BOOT_DEVICE", r"ran into a problem",
           r"has been shut down to prevent damage", r"Windows protection error",
           r"Kernel panic", r"not syncing", r"grub rescue>", r"No bootable device",
           r"Missing operating system", r"Operating System not found", r"NTLDR is missing",
           r"BOOTMGR is (missing|compressed)", r"Boot failure", r"emergency mode",
           r"dracut-initqueue timeout", r"Gave up waiting for root"]


@dataclass
class Verdict:
    state: str                 # success / failure / waiting
    reason: str = ""
    evidence: str = ""
    screenshots: list[Path] = field(default_factory=list)


def ocr(png: Path) -> str:
    """Text on a screenshot ('' without tesseract). Upscaled: guest fonts are small."""
    if not shutil.which("tesseract"):
        return ""
    try:
        from PIL import Image
        img = Image.open(png).convert("L")
        if img.width < 1600:
            f = max(2, 1600 // max(img.width, 1))
            img = img.resize((img.width * f, img.height * f))
        big = png.with_suffix(".ocr.png")
        img.save(big)
        src = big
    except ImportError:
        src = png
    cp = subprocess.run(["tesseract", str(src), "-", "--psm", "11"], capture_output=True, text=True,
                        timeout=120)
    return cp.stdout


def _match(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(0)
    return None


def classify(texts: dict[str, str], family: str, success: list[str] | None = None) -> Verdict:
    for source, text in texts.items():
        hit = _match(FAILURE, text)
        if hit:
            return Verdict("failure", f"{source} shows \"{hit}\"", text[-2000:])
    for source, text in texts.items():
        hit = _match(success or SUCCESS.get(family, SUCCESS["other"]), text)
        if hit:
            return Verdict("success", f"{source} shows \"{hit}\"", text[-2000:])
    return Verdict("waiting")


def watch(cfg, family: str, timeout: float, artifacts: Path, success: list[str] | None = None,
          interval: float = 20.0) -> Verdict:
    """Poll a running VM until it reaches a login/desktop, fails, stops or times out."""
    from rehome.vm import runner
    artifacts.mkdir(parents=True, exist_ok=True)
    shots: list[Path] = []
    serial = cfg.dir / "logs" / "serial.log"
    deadline = time.monotonic() + timeout
    n = 0
    while True:
        st = runner.state(cfg)
        if not st.active:
            log = (cfg.dir / "logs" / "qemu.log")
            return Verdict("failure", f"the VM stopped ({st.status})",
                           log.read_text(errors="replace")[-3000:] if log.exists() else "", shots)
        texts = {}
        if serial.exists():
            texts["serial console"] = serial.read_text(errors="replace")[-20000:]
        try:
            texts["text screen"] = runner.screen_text(cfg)
        except Exception:  # noqa: BLE001 - graphics mode or QMP busy
            pass
        n += 1
        shot = artifacts / f"{n:03d}.png"
        try:
            runner.screenshot(cfg, shot)
            shots.append(shot)
            texts["screen (OCR)"] = ocr(shot)
        except Exception:  # noqa: BLE001
            pass
        if runner.guest_agent(cfg, "guest-ping", timeout=2) is not None:
            return Verdict("success", "the QEMU guest agent answers", "", shots)
        v = classify(texts, family, success)
        if v.state != "waiting":
            v.screenshots = shots
            return v
        if time.monotonic() > deadline:
            return Verdict("failure", f"no login or desktop after {int(timeout)} s",
                           texts.get("screen (OCR)", "")[-2000:], shots)
        time.sleep(interval)
