# Hook scripts

Rehome runs your own scripts at fixed points of a VM's life. Hooks are the
extension point for things Rehome deliberately does not do itself, such as
GPU or other PCI pass-through (VFIO), CPU pinning, or starting a service
next to a VM.

## Where they live

Every executable file in `~/.config/rehome/hooks.d/` is a hook (the directory
follows `$XDG_CONFIG_HOME`). Hooks run in file-name order, as your user (never
as root), for every VM. Use the VM name or ID they receive to decide whether to
act.

```
~/.config/rehome/hooks.d/
├── 10-gpu-passthrough
└── 50-notify
```

## Phases

Each hook is run as `HOOK PHASE VM_ID`, with these environment variables:

| Variable | Value |
|---|---|
| `REHOME_PHASE` | `prepare`, `started` or `stopped` |
| `REHOME_VM_ID` | the VM's UUID |
| `REHOME_VM_NAME` | the VM's name |
| `REHOME_VM_DIR` | the VM's directory (contains `vm.toml`, `disks/`, `logs/`) |
| `REHOME_QEMU_ARGS_FILE` | `prepare` only: see below |

| Phase | When | A non-zero exit… |
|---|---|---|
| `prepare` | before QEMU and its helpers start | stops the VM from starting; the hook's output is shown as the technical details |
| `started` | after QEMU answers | is logged and ignored |
| `stopped` | after Rehome noticed the VM stop, **and** when a start fails after `prepare` already ran | is logged and ignored |

`stopped` always follows a successful `prepare`, even when the start fails
later. A hook can therefore safely undo in `stopped` whatever it set up in
`prepare`. Each hook may run for up to 120 seconds.

## Adding QEMU arguments

In the `prepare` phase a hook may append QEMU arguments to the file named by
`$REHOME_QEMU_ARGS_FILE`, **one argument per line** (not one option per line).
Blank lines and lines starting with `#` are ignored. Rehome adds them to the
end of the QEMU command line, and `rehome logs VM` shows the full command line.

## Example: VFIO pass-through of a second GPU

Rehome v1 does not configure PCI pass-through (see the README FAQ). This hook
shows how to try it yourself. It assumes that IOMMU is enabled, that the GPU
(`0000:01:00.0`) and its audio function (`0000:01:00.1`) are already bound to
`vfio-pci` (for example with `vfio-pci.ids=` on the kernel command line), and
that your user may open the `/dev/vfio/N` group devices (a udev rule).

```sh
#!/bin/sh
# ~/.config/rehome/hooks.d/10-gpu-passthrough
[ "$REHOME_VM_NAME" = "Gaming VM" ] || exit 0
case "$1" in
prepare)
    for a in -device vfio-pci,host=0000:01:00.0,multifunction=on \
             -device vfio-pci,host=0000:01:00.1; do
        echo "$a" >> "$REHOME_QEMU_ARGS_FILE"
    done ;;
esac
```

Mistakes here can freeze the host display or the whole machine, which is why
Rehome leaves this to you. The `[passthrough]` section of `vm.toml` is
reserved for a future version, and Rehome refuses to start a VM that uses it.
