#!/usr/bin/env python3
"""Build Rehome's VGA BIOS files (data/firmware/) from SeaBIOS's source (needs gcc, make).

QEMU's VGA BIOS, SeaVGABIOS, prints a debug note for video functions it does not
implement. Windows 95/98 calls one when it shuts down, on a stack with little
room left, and the note's digits are stored past the end of the 64 KB stack
segment: "A fatal exception 0D has occurred at C000:00002007" (D43). These are
the SeaVGABIOS release Ubuntu 26.04 ships, built with QEMU's own configuration
for each device and without the debug output (CONFIG_DEBUG_LEVEL=0). Rehome
gives them to Windows 9x VMs (rehome/vm/qemu_cmd.py, vga_bios()).

    git clone --branch rel-1.17.0 https://github.com/coreboot/seabios
    scripts/build_vgabios.py seabios

SeaBIOS is licensed under the LGPL version 3; data/firmware/README.md says where
its source is.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

TAG = "rel-1.17.0"
OUT = Path(__file__).resolve().parent.parent / "data" / "firmware"
# QEMU's roms/config.vga-* for each file, plus no debug output.
VARIANTS = {
    "vgabios-cirrus.bin": ["CONFIG_VGA_CIRRUS=y"],
    "vgabios-stdvga.bin": ["CONFIG_VGA_BOCHS=y"],
}
COMMON = ["CONFIG_BUILD_VGABIOS=y", "CONFIG_VGA_PCI=y", "CONFIG_DEBUG_LEVEL=0"]


def run(argv: list[str], src: Path) -> str:
    return subprocess.run(argv, cwd=src, check=True, capture_output=True, text=True).stdout


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1]).resolve()
    tag = run(["git", "describe", "--tags", "--exact-match"], src).strip()
    if tag != TAG or run(["git", "status", "--porcelain"], src).strip():
        sys.exit(f"{src} must be an unmodified checkout of {TAG} (it is {tag or 'not tagged'})")
    OUT.mkdir(parents=True, exist_ok=True)
    for name, options in VARIANTS.items():
        shutil.rmtree(src / "out", ignore_errors=True)
        (src / ".config").write_text("\n".join(COMMON + options) + "\n")
        run(["make", "olddefconfig"], src)
        config = (src / ".config").read_text()
        for line in COMMON + options:
            if line not in config.splitlines():
                sys.exit(f"{name}: {line} did not stay in the configuration")
        run(["make", "EXTRAVERSION=-rehome-nodebug", "out/vgabios.bin"], src)
        rom = (src / "out" / "vgabios.bin").read_bytes()
        if b"stub %s:%d" in rom:
            sys.exit(f"{name}: the debug output is still in the ROM")
        (OUT / name).write_bytes(rom)
        print(f"{name}  {len(rom)} bytes  sha256 {hashlib.sha256(rom).hexdigest()}")
    (src / ".config").unlink()
    shutil.rmtree(src / "out", ignore_errors=True)


if __name__ == "__main__":
    main()
