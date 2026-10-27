# VGA BIOS for Windows 95/98/ME VMs

Rehome gives Windows 9x VMs these instead of QEMU's own VGA BIOS files
(`rehome/vm/qemu_cmd.py`, `vga_bios()`; docs/DECISIONS.md D43):

| File | For QEMU's | sha256 |
|---|---|---|
| `vgabios-cirrus.bin` | `cirrus-vga` | `1668460846ab4ac18102a229761debb365d73372aae511e5165c4cb19e7df77f` |
| `vgabios-stdvga.bin` | `VGA` | `a7d3156c30303a7d38773a4792e6d84df529c010bcd22ae629d9aca4e9ea95f7` |

They are SeaVGABIOS, part of SeaBIOS 1.17.0, unchanged. Only the build
configuration differs from QEMU's (`roms/config.vga-cirrus`,
`roms/config.vga-stdvga`): `CONFIG_DEBUG_LEVEL=0`, so the VGA BIOS prints no
debug notes. Printing one crashed Windows 98 when it shut down ("A fatal
exception 0D has occurred at C000:00002007").

- Source: <https://github.com/coreboot/seabios> (also <https://www.seabios.org>),
  tag `rel-1.17.0`, commit `b52ca86e094d19b58e2304417787e96b940e39c6`.
- Built with `scripts/build_vgabios.py` (gcc 13.3.0, GNU ld 2.42, GNU Make 4.3,
  Ubuntu 24.04).
- License: SeaBIOS is distributed under the GNU LGPL version 3
  (`COPYING.LESSER` here). You can rebuild or change these files from the
  source above with that script.
