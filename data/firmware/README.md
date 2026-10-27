# VGA BIOS for Windows 95/98/ME VMs

Rehome gives Windows 9x VMs these instead of QEMU's own VGA BIOS files
(`rehome/vm/qemu_cmd.py`, `vga_bios()`; docs/DECISIONS.md D43, D47):

| File | For QEMU's | sha256 |
|---|---|---|
| `vgabios-cirrus.bin` | `cirrus-vga` | `7b1f3b1aa1edc1529e9da4175cd19007d7f4e55bf918bf2e4263180fdd7bcf0f` |
| `vgabios-stdvga.bin` | `VGA` | `42413225fb6266613f02a39f6a6b7f3c67b8024a826b77111df252400c36720c` |

They are SeaVGABIOS, part of SeaBIOS 1.17.0, built with QEMU's configuration
(`roms/config.vga-cirrus`, `roms/config.vga-stdvga`) and two options changed:

- `CONFIG_DEBUG_LEVEL=0`: the VGA BIOS prints no debug notes. Printing one
  crashed Windows 98 when it shut down ("A fatal exception 0D has occurred at
  C000:00002007").
- `CONFIG_VGA_ALLOCATE_EXTRA_STACK` off: every call runs on its caller's
  stack, as SeaVGABIOS did before 2013. Its own small stack lives in the
  BIOS's upper memory, which Windows 9x maps for each of its virtual
  machines, and Windows only ever made mode sets that left it on. Windows 98
  hung at a black screen when it shut down.

- Source: <https://github.com/coreboot/seabios> (also <https://www.seabios.org>),
  tag `rel-1.17.0`, commit `b52ca86e094d19b58e2304417787e96b940e39c6`.
- Built with `scripts/build_vgabios.py` (gcc 13.3.0, GNU ld 2.42, GNU Make 4.3,
  Ubuntu 24.04), unchanged source.
- License: SeaBIOS is distributed under the GNU LGPL version 3
  (`COPYING.LESSER` here). The source of these files is the source above;
  that script rebuilds them.
