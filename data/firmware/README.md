# VGA BIOS for Windows 95/98/ME VMs

Rehome gives Windows 9x VMs these instead of QEMU's own VGA BIOS files
(`rehome/vm/qemu_cmd.py`, `vga_bios()`; docs/DECISIONS.md D43, D44):

| File | For QEMU's | sha256 |
|---|---|---|
| `vgabios-cirrus.bin` | `cirrus-vga` | `c85fd6c20d3e073968c10c14097309cd1be285a0a7aa6d29fdb4e1c5a7bfb5f8` |
| `vgabios-stdvga.bin` | `VGA` | `45d88842cf90c3bbdb8a5d29aeab75ba9d9dec9d28714076d7ca0b274087834b` |

They are SeaVGABIOS, part of SeaBIOS 1.17.0, built with QEMU's configuration
(`roms/config.vga-cirrus`, `roms/config.vga-stdvga`) and two changes:

- `CONFIG_DEBUG_LEVEL=0`: the VGA BIOS prints no debug notes. Printing one
  crashed Windows 98 when it shut down ("A fatal exception 0D has occurred at
  C000:00002007").
- `seavgabios-extra-stack.patch` (here): the VGA BIOS's own small stack for
  callers with small stacks is on from the start, and once an OS has set a
  mode its own way (as Windows' display driver does) it stays off until the
  next boot. Unchanged, a mode set made the old way, as Windows 98 makes when
  it shuts down, turned it back on while Windows' virtual machines share it,
  and Windows 98 hung at a black screen.

- Source: <https://github.com/coreboot/seabios> (also <https://www.seabios.org>),
  tag `rel-1.17.0`, commit `b52ca86e094d19b58e2304417787e96b940e39c6`.
- Built with `scripts/build_vgabios.py` (gcc 13.3.0, GNU ld 2.42, GNU Make 4.3,
  Ubuntu 24.04).
- License: SeaBIOS is distributed under the GNU LGPL version 3
  (`COPYING.LESSER` here). The source of these files is the source above with
  `seavgabios-extra-stack.patch` applied; that script rebuilds them.
