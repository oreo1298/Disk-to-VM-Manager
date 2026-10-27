# VGA BIOS for Windows 95/98/ME VMs

Rehome gives Windows 9x VMs these instead of QEMU's own VGA BIOS files
(`rehome/vm/qemu_cmd.py`, `vga_bios()`; docs/DECISIONS.md D43, D44):

| File | For QEMU's | sha256 |
|---|---|---|
| `vgabios-cirrus.bin` | `cirrus-vga` | `7b1f3b1aa1edc1529e9da4175cd19007d7f4e55bf918bf2e4263180fdd7bcf0f` |
| `vgabios-stdvga.bin` | `VGA` | `42413225fb6266613f02a39f6a6b7f3c67b8024a826b77111df252400c36720c` |

They are SeaVGABIOS, part of SeaBIOS 1.17.0, unchanged. Only the build
configuration differs from QEMU's (`roms/config.vga-cirrus`,
`roms/config.vga-stdvga`):

- `CONFIG_DEBUG_LEVEL=0`: the VGA BIOS prints no debug notes. Printing one
  crashed Windows 98 when it shut down ("A fatal exception 0D has occurred at
  C000:00002007").
- `CONFIG_VGA_ALLOCATE_EXTRA_STACK` off: the VGA BIOS always runs on its
  caller's stack. Its own 512-byte stack is in the E-segment, upper memory
  that Windows 9x and EMM386 use for other things. With it, Windows 98 hung at
  a black screen when it shut down.

- Source: <https://github.com/coreboot/seabios> (also <https://www.seabios.org>),
  tag `rel-1.17.0`, commit `b52ca86e094d19b58e2304417787e96b940e39c6`.
- Built with `scripts/build_vgabios.py` (gcc 13.3.0, GNU ld 2.42, GNU Make 4.3,
  Ubuntu 24.04).
- License: SeaBIOS is distributed under the GNU LGPL version 3
  (`COPYING.LESSER` here). You can rebuild or change these files from the
  source above with that script.
