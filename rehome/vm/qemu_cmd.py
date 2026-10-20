"""Build the QEMU command line for a VM.

The builder is pure: given a :class:`VMConfig`, the host capabilities and the
runtime paths, it returns a :class:`LaunchPlan` (argv, fds to pass, sidecars
to start). The runner executes the plan. Keeping this pure makes every
device decision unit-testable without starting QEMU.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from rehome.errors import RehomeError
from rehome.host.firmware import Firmware
from rehome.host.qemu import QemuCaps
from rehome.vm.config import CdRom, Disk, Nic, VMConfig


@dataclass
class Sidecar:
    name: str
    argv: list[str]
    socket: str = ""              # wait until this socket exists
    pidfile: str = ""
    log: str = ""
    daemonizes: bool = False      # the program forks itself into the background


@dataclass
class RuntimePaths:
    run: Path                     # $XDG_RUNTIME_DIR/rehome/vms/<id>

    @property
    def qmp(self) -> str:
        return str(self.run / "qmp.sock")

    @property
    def qmp_ui(self) -> str:
        return str(self.run / "qmp-ui.sock")

    @property
    def pidfile(self) -> str:
        return str(self.run / "qemu.pid")

    @property
    def serial(self) -> str:
        return str(self.run / "serial.sock")

    @property
    def qga(self) -> str:
        return str(self.run / "qga.sock")

    @property
    def swtpm(self) -> str:
        return str(self.run / "swtpm.sock")

    @property
    def vnc(self) -> str:
        return str(self.run / "vnc.sock")

    @property
    def spice(self) -> str:
        return str(self.run / "spice.sock")

    def passt(self, nic_id: str) -> str:
        return str(self.run / f"passt-{nic_id}.sock")

    def virtiofs(self, index: int) -> str:
        return str(self.run / f"virtiofs-{index}.sock")


@dataclass
class LaunchOptions:
    accel: str = "kvm"                    # resolved accelerator
    headless: bool = False                # no interactive display at all (tests)
    paused: bool = False                  # start with -S
    incoming: str = ""                    # restore saved state from this file
    loadvm: str = ""                      # start from an internal snapshot with RAM
    firmware: Firmware | None = None
    nvram_path: str = ""                  # per-VM UEFI variable store
    nvram_format: str = "qcow2"
    fdsets: dict[str, int] = field(default_factory=dict)       # disk id -> fdset id
    source_paths: dict[str, str] = field(default_factory=dict) # disk id -> path override
    audio_backend: str = "none"
    passt: str | None = None              # path of passt binary, if usable
    virtiofsd: str | None = None
    swtpm: str | None = None
    log_dir: Path | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class LaunchPlan:
    argv: list[str]
    sidecars: list[Sidecar] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    console: str = "dbus"


def _js(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _cpu_flags(flags: list[str]) -> list[str]:
    out = []
    for f in flags:
        if f.startswith("+"):
            out.append(f"{f[1:]}=on")
        elif f.startswith("-"):
            out.append(f"{f[1:]}=off")
        else:
            out.append(f)
    return out


HYPERV_FLAGS = ["hv-relaxed=on", "hv-vapic=on", "hv-spinlocks=0x1fff", "hv-vpindex=on",
                "hv-runtime=on", "hv-time=on", "hv-synic=on", "hv-stimer=on", "hv-reset=on",
                "hv-frequencies=on", "hv-tlbflush=on", "hv-ipi=on"]


class Builder:
    def __init__(self, cfg: VMConfig, caps: QemuCaps, rt: RuntimePaths, opts: LaunchOptions):
        self.cfg = cfg
        self.caps = caps
        self.rt = rt
        self.opts = opts
        self.args: list[str] = []
        self.sidecars: list[Sidecar] = []
        self.warnings: list[str] = []
        m = cfg.machine
        self.pc_like = m.arch in ("x86_64", "i386", "i686")
        self.machine_type = m.type
        self.is_q35 = m.type == "q35"
        self.is_pc = m.type in ("pc", "isapc")
        self._ahci_ports = 0
        self._ide_slots = 0
        self._scsi = False
        self._vser = False
        self._usb_bus = ""
        self._console_kind = "none"

    def a(self, *items: str) -> None:
        self.args.extend(items)

    # --- top level ---------------------------------------------------------

    def build(self) -> LaunchPlan:
        cfg = self.cfg
        if cfg.passthrough.pci:
            raise RehomeError(
                "PCI/GPU pass-through is configured, but this version of Rehome does not "
                "support it yet.",
                fix="Remove the [passthrough] section from vm.toml, or use a hook script "
                    "(see docs/HOOKS.md) to experiment.")
        self.a(self.caps.binary)
        self.a("-name", f"{cfg.name},process=rehome-{cfg.short_id},debug-threads=on")
        self.a("-uuid", cfg.id)
        self.a("-nodefaults", "-no-user-config")
        self.a("-msg", "timestamp=on")
        self.a("-pidfile", self.rt.pidfile)
        self._machine()
        self._cpu()
        self._memory()
        self._rtc()
        self._monitors()
        self._display()
        self._usb_controller()
        self._input()
        self._storage()
        self._network()
        self._sound()
        self._tpm()
        self._shares()
        self._guest_channels()
        self._misc()
        self._direct_boot()
        if self.opts.paused:
            self.a("-S")
        if self.opts.incoming:
            self.a("-incoming", "defer")
        if self.opts.loadvm:
            self.a("-loadvm", self.opts.loadvm)
        self.args.extend(self.opts.extra_args)
        return LaunchPlan(argv=self.args, sidecars=self.sidecars, warnings=self.warnings,
                          console=self._console_kind)

    # --- machine / cpu / memory ---------------------------------------------

    def _machine(self) -> None:
        m, o = self.cfg.machine, self.opts
        props = [f"type={m.type}"]
        fw = o.firmware
        if m.firmware != "bios":
            if fw is None:
                raise RehomeError(
                    "This VM needs UEFI firmware, but none was found on this computer.",
                    fix="Install the OVMF/edk2 firmware package (run `rehome doctor`).")
            fmt = fw.executable_format
            self.a("-blockdev", _js({"driver": "file", "node-name": "fw-code-file",
                                     "filename": fw.executable, "read-only": True}))
            self.a("-blockdev", _js({"driver": fmt, "node-name": "fw-code",
                                     "file": "fw-code-file", "read-only": True}))
            props.append("pflash0=fw-code")
            if fw.mode != "stateless" and o.nvram_path:
                self.a("-blockdev", _js({"driver": "file", "node-name": "fw-vars-file",
                                         "filename": o.nvram_path}))
                self.a("-blockdev", _js({"driver": o.nvram_format, "node-name": "fw-vars",
                                         "file": "fw-vars-file"}))
                props.append("pflash1=fw-vars")
            if fw.requires_smm or m.firmware == "uefi-secure":
                if not self.is_q35 and self.pc_like:
                    raise RehomeError("Secure Boot needs the Q35 machine type.",
                                      fix="Change the machine type to q35 or turn off Secure Boot.")
                if self.pc_like:
                    props.append("smm=on")
                    self.a("-global", "driver=cfi.pflash01,property=secure,value=on")
        if self.pc_like and m.type in ("pc", "q35"):
            if not m.hpet:
                props.append("hpet=off")
            if not m.acpi:
                props.append("acpi=off")
        if self.cfg.shares:
            props.append("memory-backend=mem")
        if self.pc_like and self.cfg.sound.device != "none" \
                and self.cfg.os.family in ("dos", "win9x") and self.caps.at_least(5, 1):
            props.append("pcspk-audiodev=snd0")
        self.a("-machine", ",".join(props))
        if o.accel == "kvm":
            self.a("-accel", "kvm")
        else:
            tcg = ["tcg", "thread=multi"] if m.cpus > 1 else ["tcg"]
            self.a("-accel", ",".join(tcg))
        if m.icount:
            self.a("-icount", m.icount)

    def _cpu(self) -> None:
        m, o = self.cfg.machine, self.opts
        model = m.cpu_model or "host"
        if o.accel != "kvm" and model == "host":
            model = "max"
        parts = [model]
        if o.accel == "kvm" and m.hyperv and self.pc_like:
            parts += HYPERV_FLAGS
        parts += _cpu_flags(m.cpu_flags)
        self.a("-cpu", ",".join(parts))
        n = max(1, m.cpus)
        self.a("-smp", f"cpus={n},sockets=1,cores={n},threads=1")
        if o.accel == "kvm" and (m.hyperv or self.cfg.os.family == "windows") and self.pc_like:
            self.a("-global", "kvm-pit.lost_tick_policy=delay")

    def _memory(self) -> None:
        m = self.cfg.machine
        self.a("-m", f"{m.memory_mib}M")
        if self.cfg.shares:
            self.a("-object", f"memory-backend-memfd,id=mem,size={m.memory_mib}M,share=on")

    def _rtc(self) -> None:
        if not self.pc_like:
            return
        rtc = ["base=localtime" if self.cfg.machine.rtc == "localtime" else "base=utc", "clock=host"]
        # Tick re-injection keeps Windows' clock right under KVM. It is opt-in
        # per profile: ReactOS (and some others) hang with it.
        if self.cfg.machine.rtc_catchup and self.opts.accel == "kvm":
            rtc.append("driftfix=slew")
        self.a("-rtc", ",".join(rtc))

    # --- control and console --------------------------------------------------

    def _monitors(self) -> None:
        for i, path in enumerate((self.rt.qmp, self.rt.qmp_ui)):
            self.a("-chardev", f"socket,id=qmp{i},path={path},server=on,wait=off")
            self.a("-mon", f"chardev=qmp{i},mode=control")
        serial_log = ""
        if self.opts.log_dir:
            serial_log = f",logfile={self.opts.log_dir / 'serial.log'},logappend=on"
        self.a("-chardev", f"socket,id=serial0,path={self.rt.serial},server=on,wait=off{serial_log}")
        self.a("-serial", "chardev:serial0")

    def _display(self) -> None:
        d = self.cfg.display
        dev = d.device
        gl = d.gl
        if gl and not self.caps.virgl:
            self.warnings.append("3D acceleration is not available with this QEMU; using 2D.")
            gl = False
        if dev == "virtio-vga" and gl:
            dev = "virtio-vga-gl"
        if dev == "virtio-gpu-pci" and gl:
            dev = "virtio-gpu-gl-pci"
        if dev in ("virtio-vga", "virtio-vga-gl") and not self.pc_like:
            # Non-PC machines have no legacy VGA; use the PCI GPU instead.
            dev = "virtio-gpu-gl-pci" if gl else "virtio-gpu-pci"
        if dev != "none":
            if not self.caps.has_device(dev) and self.caps.devices:
                fallback = "VGA" if self.pc_like else "virtio-gpu-pci"
                self.warnings.append(f"The display device {dev} is not available; using {fallback}.")
                dev, gl = fallback, False
            props = [dev]
            if d.vram_mib and dev in ("VGA", "cirrus-vga", "vmware-svga", "qxl-vga"):
                props.append(f"vgamem_mb={d.vram_mib}")
            if dev in ("cirrus-vga", "VGA", "vmware-svga", "qxl-vga", "virtio-vga", "virtio-vga-gl"):
                props.append("id=video0")
            self.a("-device", ",".join(props))
        console = d.console
        if self.opts.headless:
            console = "none"
        elif console == "dbus" and not self.caps.dbus_display:
            self.warnings.append("QEMU's D-Bus display is not installed; using a VNC socket instead.")
            console = "vnc"
        self._console_kind = console
        if console == "dbus":
            self.a("-display", "dbus,p2p=yes" + (",gl=on" if gl else ""))
        elif console == "spice":
            self.a("-display", "none")
            self.a("-spice", f"unix=on,addr={self.rt.spice},disable-ticketing=on" + (",gl=on" if gl else ""))
        elif console == "vnc":
            self.a("-display", "none")
            self.a("-vnc", f"unix:{self.rt.vnc}")
        else:
            self.a("-display", "egl-headless" if gl else "none")

    # --- USB and input ----------------------------------------------------------

    def _usb_controller(self) -> None:
        ctrl = self.cfg.usb.controller
        if ctrl == "none":
            return
        if ctrl == "ich9-usb-ehci1":
            self.a("-device", "ich9-usb-ehci1,id=usb")
            for i, fn in enumerate((0, 1, 2), start=1):
                self.a("-device", f"ich9-usb-uhci{i},masterbus=usb.0,firstport={fn * 2}")
        else:
            self.a("-device", f"{ctrl},id=usb")
        self._usb_bus = "usb.0"

    def _input(self) -> None:
        inp = self.cfg.input
        ptr = inp.pointer
        if ptr in ("usb-tablet", "usb-mouse") and not self._usb_bus:
            self.warnings.append("A USB tablet needs a USB controller; using the PS/2 mouse.")
            ptr = "ps2"
        if ptr in ("usb-tablet", "usb-mouse"):
            self.a("-device", f"{ptr},bus={self._usb_bus}")
        elif ptr == "virtio-tablet":
            self.a("-device", "virtio-tablet-pci")
        if inp.keyboard == "usb" and self._usb_bus:
            self.a("-device", f"usb-kbd,bus={self._usb_bus}")
        elif inp.keyboard == "virtio":
            self.a("-device", "virtio-keyboard-pci")
        if not self.pc_like:
            # ARM/RISC-V virt machines have no PS/2.
            if ptr == "ps2":
                self.a("-device", "virtio-tablet-pci")
            if inp.keyboard == "ps2":
                self.a("-device", "virtio-keyboard-pci")

    # --- storage -------------------------------------------------------------------

    def _ahci_bus(self) -> str:
        if self.is_q35:
            if self._ahci_ports >= 6:
                raise RehomeError("Too many SATA devices (the Q35 controller has 6 ports).",
                                  fix="Use VirtIO or SCSI for additional disks.")
            bus = f"ide.{self._ahci_ports}"
        else:
            if self._ahci_ports == 0:
                self.a("-device", "ahci,id=ahci0")
            bus = f"ahci0.{self._ahci_ports}"
        self._ahci_ports += 1
        return bus

    def _ide_bus(self) -> str:
        if self.is_q35:
            return self._ahci_bus()
        if self._ide_slots >= 4:
            raise RehomeError("Too many IDE devices (the IDE controller has 4 slots).",
                              fix="Use a different bus for additional disks.")
        slot = self._ide_slots
        self._ide_slots += 1
        # Order: primary master, secondary master, primary slave, secondary slave
        # so that the first CD lands on the secondary channel like real PCs.
        order = [("ide.0", 0), ("ide.1", 0), ("ide.0", 1), ("ide.1", 1)]
        bus, unit = order[slot]
        return f"{bus},unit={unit}"

    def _scsi_bus(self) -> str:
        if not self._scsi:
            self.a("-device", "virtio-scsi-pci,id=scsi0")
            self._scsi = True
        return "scsi0.0"

    def _source_node(self, disk: Disk) -> str | None:
        src = disk.source
        if src is None or src.mode != "linked":
            return None
        node = f"{disk.id}-src"
        if src.kind == "block":
            fdset = self.opts.fdsets.get(disk.id)
            if fdset is None:
                raise RehomeError(f"The physical disk for {disk.id} was not opened.",
                                  code="internal")
            file_node = {"driver": "host_device", "filename": f"/dev/fdset/{fdset}",
                         "read-only": True, "aio": "threads"}
            fmt = "raw"
        else:
            path = self.opts.source_paths.get(disk.id, src.path)
            if not os.path.exists(path):
                raise RehomeError(
                    f"The source image for {disk.id} is missing: {path}",
                    fix="Reconnect the drive that holds the image, or convert this VM to a "
                        "standalone copy while the image is available.")
            file_node = {"driver": "file", "filename": path, "read-only": True}
            fmt = "raw" if src.kind == "ewf" else src.format
        self.a("-blockdev", _js({"driver": fmt, "node-name": node, "read-only": True,
                                 "file": file_node}))
        return node

    def _disk_node(self, disk: Disk) -> str:
        path = str(self.cfg.resolve(disk.file))
        if not os.path.exists(path):
            raise RehomeError(f"The disk file for {disk.id} is missing: {path}",
                              fix="Restore the file or remove the disk in the VM settings.")
        backing = self._source_node(disk)
        cache = {"direct": disk.cache == "none", "no-flush": disk.cache == "unsafe"}
        node = {"driver": disk.format, "node-name": disk.id, "cache": cache,
                "read-only": disk.readonly,
                "file": {"driver": "file", "filename": path, "cache": cache,
                         "read-only": disk.readonly,
                         "aio": "threads"}}
        if disk.discard and not disk.readonly:
            node["discard"] = "unmap"
            node["file"]["discard"] = "unmap"
            node["detect-zeroes"] = "unmap"
        if backing:
            node["backing"] = backing
        self.a("-blockdev", _js(node))
        return disk.id

    def _storage(self) -> None:
        cfg = self.cfg
        # Floppies first (pc machines have a built-in controller).
        for f in cfg.floppies:
            if not self.is_pc:
                self.warnings.append("Floppy drives need the PC (i440FX) machine type; skipped.")
                break
            if f.file:
                fmt = "raw"
                self.a("-blockdev", _js({"driver": "file", "node-name": f"{f.id}-file",
                                         "filename": str(cfg.resolve(f.file))}))
                self.a("-blockdev", _js({"driver": fmt, "node-name": f.id, "file": f"{f.id}-file"}))
                self.a("-device", f"floppy,unit={f.id[-1] if f.id[-1].isdigit() else 0},drive={f.id}")
            else:
                self.a("-device", "floppy,unit=0")
        for disk in cfg.disks:
            node = self._disk_node(disk)
            boot = f",bootindex={disk.boot_index}" if disk.boot_index else ""
            serial = f",serial={disk.serial}" if disk.serial else ""
            bus = disk.bus
            if bus == "ide" and self.is_q35:
                bus = "sata"
            if bus == "virtio":
                self.a("-device", f"virtio-blk-pci,drive={node},id={disk.id}-dev{boot}{serial}")
            elif bus == "scsi":
                self.a("-device", f"scsi-hd,bus={self._scsi_bus()},drive={node},id={disk.id}-dev{boot}{serial}")
            elif bus == "sata":
                self.a("-device", f"ide-hd,bus={self._ahci_bus()},drive={node},id={disk.id}-dev{boot}{serial}")
            elif bus == "ide":
                self.a("-device", f"ide-hd,bus={self._ide_bus()},drive={node},id={disk.id}-dev{boot}{serial}")
            elif bus == "nvme":
                ser = disk.serial or f"REHOME{disk.id.upper()}"
                self.a("-device", f"nvme,drive={node},id={disk.id}-dev,serial={ser}{boot}")
            elif bus == "usb":
                if not self._usb_bus:
                    raise RehomeError("A USB disk needs a USB controller.",
                                      fix="Enable a USB controller in the VM settings.")
                self.a("-device", f"usb-storage,bus={self._usb_bus},drive={node},id={disk.id}-dev{boot}")
            else:
                raise RehomeError(f"Unknown disk bus \"{disk.bus}\".")
        for cd in cfg.cdroms:
            self._cdrom(cd)

    def _cdrom(self, cd: CdRom) -> None:
        drive = ""
        if cd.file:
            path = str(self.cfg.resolve(cd.file))
            if not os.path.exists(path):
                self.warnings.append(f"The ISO for {cd.id} is missing ({path}); the drive will be empty.")
            else:
                self.a("-blockdev", _js({"driver": "file", "node-name": f"{cd.id}-file",
                                         "filename": path, "read-only": True}))
                self.a("-blockdev", _js({"driver": "raw", "node-name": f"{cd.id}-media",
                                         "file": f"{cd.id}-file", "read-only": True}))
                drive = f",drive={cd.id}-media"
        boot = f",bootindex={cd.boot_index}" if cd.boot_index else ""
        bus = cd.bus
        if bus == "ide" and self.is_q35:
            bus = "sata"
        if not self.pc_like and bus in ("ide", "sata"):
            bus = "scsi"
        if bus == "sata":
            self.a("-device", f"ide-cd,bus={self._ahci_bus()},id={cd.id}{drive}{boot}")
        elif bus == "ide":
            self.a("-device", f"ide-cd,bus={self._ide_bus()},id={cd.id}{drive}{boot}")
        elif bus == "scsi":
            self.a("-device", f"scsi-cd,bus={self._scsi_bus()},id={cd.id}{drive}{boot}")
        elif bus == "usb":
            if not drive:
                self.warnings.append("An empty USB CD drive is not supported; skipped.")
                return
            self.a("-device", f"usb-storage,bus={self._usb_bus},id={cd.id}{drive}{boot},removable=on")

    # --- network -----------------------------------------------------------------

    def _network(self) -> None:
        for nic in self.cfg.nics:
            if nic.mode == "none":
                continue
            netdev = f"hostnet-{nic.id}"
            if nic.mode == "nat":
                self._nat(nic, netdev)
            elif nic.mode == "bridge":
                if not nic.bridge:
                    raise RehomeError("Bridged networking needs a bridge name.",
                                      fix="Choose a bridge in the network settings.")
                self.a("-netdev", f"bridge,id={netdev},br={nic.bridge}")
            else:
                raise RehomeError(f"Unknown network mode \"{nic.mode}\".")
            model = nic.model
            if model == "ne2k_isa" and not self.is_pc:
                model = "ne2k_pci"
            props = [model, f"netdev={netdev}", f"mac={nic.mac}", f"id={nic.id}"]
            if model in ("usb-net",):
                props.append(f"bus={self._usb_bus}")
            self.a("-device", ",".join(props))

    def _nat(self, nic: Nic, netdev: str) -> None:
        if self.opts.passt and self.caps.at_least(7, 2):
            sock = self.rt.passt(nic.id)
            # --foreground: Rehome records the pid itself (passt drops to
            # "nobody" when started as root and could not write a pidfile).
            argv = [self.opts.passt, "--socket", sock, "--one-off", "--foreground", "--quiet"]
            for fwd in nic.port_forwards:
                proto, host_port, guest_port = _parse_forward(fwd)
                argv += ["-t" if proto == "tcp" else "-u", f"{host_port}:{guest_port}"]
            self.sidecars.append(Sidecar("passt", argv, socket=sock,
                                         pidfile=str(self.rt.run / f"passt-{nic.id}.pid"),
                                         log=str(self.opts.log_dir / f"passt-{nic.id}.log")
                                         if self.opts.log_dir else ""))
            self.a("-netdev", f"stream,id={netdev},server=off,addr.type=unix,addr.path={sock}")
        else:
            fwds = []
            for fwd in nic.port_forwards:
                proto, host_port, guest_port = _parse_forward(fwd)
                fwds.append(f",hostfwd={proto}:127.0.0.1:{host_port}-:{guest_port}")
            self.a("-netdev", f"user,id={netdev}" + "".join(fwds))

    # --- sound, tpm, shares, guest channels -------------------------------------

    def _sound(self) -> None:
        dev = self.cfg.sound.device
        if dev == "none":
            return
        backend = self.opts.audio_backend or "none"
        self.a("-audiodev", f"{backend},id=snd0")
        if dev in ("ich9-intel-hda", "intel-hda"):
            if dev == "intel-hda" and self.is_q35:
                dev = "ich9-intel-hda"
            self.a("-device", f"{dev},id=hda")
            self.a("-device", "hda-duplex,bus=hda.0,audiodev=snd0")
        elif dev == "sb16":
            if not self.is_pc:
                self.warnings.append("Sound Blaster 16 needs the PC machine type; sound disabled.")
                return
            self.a("-device", "sb16,audiodev=snd0")
        elif dev == "usb-audio":
            if self._usb_bus:
                self.a("-device", f"usb-audio,bus={self._usb_bus},audiodev=snd0")
        else:
            self.a("-device", f"{dev},audiodev=snd0")

    def _tpm(self) -> None:
        tpm = self.cfg.machine.tpm
        if tpm == "none":
            return
        if not self.opts.swtpm:
            raise RehomeError("This VM has a TPM, but swtpm is not installed.",
                              fix="Install swtpm (run `rehome doctor`), or remove the TPM.")
        state = self.cfg.dir / "tpm"
        argv = [self.opts.swtpm, "socket", "--tpmstate", f"dir={state},mode=0600",
                "--ctrl", f"type=unixio,path={self.rt.swtpm},mode=0600",
                "--terminate", "--daemon", "--pid", f"file={self.rt.run / 'swtpm.pid'}"]
        if tpm == "2.0":
            argv.insert(2, "--tpm2")
        if self.opts.log_dir:
            argv += ["--log", f"file={self.opts.log_dir / 'swtpm.log'},level=1"]
        self.sidecars.append(Sidecar("swtpm", argv, socket=self.rt.swtpm, daemonizes=True,
                                     pidfile=str(self.rt.run / "swtpm.pid")))
        self.a("-chardev", f"socket,id=chrtpm,path={self.rt.swtpm}")
        self.a("-tpmdev", "emulator,id=tpm0,chardev=chrtpm")
        if self.pc_like:
            dev = "tpm-crb" if tpm == "2.0" else "tpm-tis"
        else:
            dev = "tpm-tis-device"
        self.a("-device", f"{dev},tpmdev=tpm0")

    def _shares(self) -> None:
        if not self.cfg.shares:
            return
        if not self.opts.virtiofsd:
            raise RehomeError("Shared folders need virtiofsd, which is not installed.",
                              fix="Install virtiofsd (run `rehome doctor`) or remove the shares.")
        for i, sh in enumerate(self.cfg.shares):
            sock = self.rt.virtiofs(i)
            argv = [self.opts.virtiofsd, f"--socket-path={sock}", f"--shared-dir={sh.path}",
                    "--sandbox=namespace", "--cache=auto"]
            if sh.readonly:
                argv.append("--readonly")
            self.sidecars.append(Sidecar(f"virtiofsd-{sh.tag}", argv, socket=sock,
                                         pidfile=str(self.rt.run / f"virtiofsd-{i}.pid"),
                                         log=str(self.opts.log_dir / f"virtiofsd-{sh.tag}.log")
                                         if self.opts.log_dir else ""))
            self.a("-chardev", f"socket,id=vfs{i},path={sock}")
            self.a("-device", f"vhost-user-fs-pci,chardev=vfs{i},tag={sh.tag}")

    def _virtio_serial(self) -> str:
        if not self._vser:
            self.a("-device", "virtio-serial-pci,id=vser0")
            self._vser = True
        return "vser0.0"

    def _guest_channels(self) -> None:
        g = self.cfg.guest
        if self.cfg.os.family in ("dos", "win9x"):
            return
        if g.agent:
            bus = self._virtio_serial()
            self.a("-chardev", f"socket,id=qga0,path={self.rt.qga},server=on,wait=off")
            self.a("-device", f"virtserialport,bus={bus},chardev=qga0,name=org.qemu.guest_agent.0")
        if g.vdagent and self._console_kind == "dbus" and self.caps.has_device("virtserialport"):
            bus = self._virtio_serial()
            self.a("-chardev", "qemu-vdagent,id=vdagent0,clipboard=on,mouse=off")
            self.a("-device", f"virtserialport,bus={bus},chardev=vdagent0,name=com.redhat.spice.0")

    def _misc(self) -> None:
        fam = self.cfg.os.family
        if fam in ("linux", "bsd") or self.cfg.guest.paravirt:
            if self.caps.has_device("virtio-rng-pci") or not self.caps.devices:
                self.a("-object", "rng-random,id=rng0,filename=/dev/urandom")
                self.a("-device", "virtio-rng-pci,rng=rng0")
            if self.caps.has_device("virtio-balloon-pci") or not self.caps.devices:
                self.a("-device", "virtio-balloon-pci,id=balloon0")
        if self.pc_like and fam in ("linux", "windows") and \
                (self.caps.has_device("pvpanic") or not self.caps.devices):
            self.a("-device", "pvpanic")


    def _direct_boot(self) -> None:
        db = self.cfg.direct_boot
        if not db or not db.kernel:
            return
        self.a("-kernel", str(self.cfg.resolve(db.kernel)))
        if db.initrd:
            self.a("-initrd", str(self.cfg.resolve(db.initrd)))
        if db.append:
            self.a("-append", db.append)


def _parse_forward(spec: str) -> tuple[str, int, int]:
    """``tcp:2222:22`` -> ("tcp", 2222, 22)."""
    parts = spec.split(":")
    if len(parts) == 2:
        parts = ["tcp"] + parts
    if len(parts) != 3 or parts[0] not in ("tcp", "udp"):
        raise RehomeError(f"Invalid port forward \"{spec}\".",
                          fix="Use the form tcp:HOSTPORT:GUESTPORT, e.g. tcp:2222:22.")
    return parts[0], int(parts[1]), int(parts[2])


def build(cfg: VMConfig, caps: QemuCaps, rt: RuntimePaths, opts: LaunchOptions) -> LaunchPlan:
    return Builder(cfg, caps, rt, opts).build()
