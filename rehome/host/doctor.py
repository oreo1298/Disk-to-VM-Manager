"""`rehome doctor`: check the host and explain every problem in plain English."""

from __future__ import annotations

import glob
import os
import shutil
from dataclasses import asdict, dataclass, field

from rehome import paths
from rehome.host import gpu as gpu_mod
from rehome.host import packages, qemu as qemu_mod, tools as tools_mod
from rehome.host.info import HostInfo, choose_accel, choose_firmware, gather
from rehome.host.system import desktop_family
from rehome.util.units import human

OK, INFO, WARN, ERROR = "ok", "info", "warning", "error"
MIN_QEMU = (7, 2, 0)


@dataclass
class Check:
    id: str
    section: str
    title: str
    status: str
    detail: str = ""
    fix: str = ""
    commands: list[str] = field(default_factory=list)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def add(self, *a, **kw) -> Check:
        c = Check(*a, **kw)
        self.checks.append(c)
        return c

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.status in (WARN, ERROR)]

    @property
    def worst(self) -> str:
        order = [OK, INFO, WARN, ERROR]
        return max((c.status for c in self.checks), key=order.index, default=OK)

    def to_dict(self) -> dict:
        return {"checks": [asdict(c) for c in self.checks], "facts": self.facts,
                "worst": self.worst}


def _install(keys: list[str], family: str) -> list[str]:
    cmd = packages.install_command(keys, family)
    return [cmd] if cmd else []


def check_cpu(r: Report, h: HostInfo) -> None:
    c = h.cpu
    sockets, cores, threads = h.topology
    r.facts["cpu"] = {"arch": c.arch, "vendor": c.vendor_label, "model": c.model_name,
                      "virtualization": c.virt_name, "sockets": sockets, "cores": cores,
                      "threads": threads, "hypervisor": c.hypervisor_vendor or None}
    model = c.model_name
    if c.vendor_label.lower() not in model.lower():
        model = f"{c.vendor_label} {model}".strip()
    desc = f"{c.arch}, {model}".strip(", ")
    r.add("cpu.model", "CPU", "Processor", OK,
          f"{desc} — {cores} cores / {threads} threads")
    if c.running_under_hypervisor:
        r.add("cpu.nested-host", "CPU", "Running inside a virtual machine", INFO,
              f"This system is itself a VM ({c.hypervisor_vendor or 'unknown hypervisor'}). "
              "VMs created here need nested virtualization on the outer host to use KVM.")
    if c.arch in ("x86_64", "i386"):
        if c.virt_extension:
            r.add("cpu.virt", "CPU", "Virtualization extensions", OK,
                  f"{c.virt_name} is supported by this CPU.")
        elif h.kvm_usable:
            r.add("cpu.virt", "CPU", "Virtualization extensions", OK,
                  "KVM works, although the CPU flags are hidden.")
        elif c.running_under_hypervisor:
            r.add("cpu.virt", "CPU", "Virtualization extensions", ERROR,
                  "The virtual CPU does not expose VT-x/AMD-V, so KVM cannot be used.",
                  fix="Enable nested virtualization on the outer hypervisor (for example "
                      "\"Expose hardware assisted virtualization\" in VMware, "
                      "\"Enable nested VT-x/AMD-V\" in VirtualBox, or "
                      "`Set-VMProcessor -ExposeVirtualizationExtensions $true` in Hyper-V).")
        else:
            setting = "SVM Mode / AMD-V" if c.vendor in ("AuthenticAMD", "HygonGenuine") \
                else "Intel Virtualization Technology (VT-x)"
            r.add("cpu.virt", "CPU", "Virtualization extensions", ERROR,
                  "Hardware virtualization is not available. It is most likely disabled in "
                  "your firmware settings.",
                  fix=f"Virtualization is disabled in your firmware settings. Reboot, open the "
                      f"UEFI/BIOS setup (usually F2, F10, F12 or Del during start-up) and "
                      f"enable \"{setting}\". Save, reboot and run `rehome doctor` again.")


def check_kvm(r: Report, h: HostInfo) -> None:
    k = h.kvm
    r.facts["kvm"] = {"device": k.device_exists, "accessible": k.accessible,
                      "module": k.module, "nested": k.nested}
    if k.device_exists and k.accessible:
        r.add("kvm.access", "CPU", "KVM", OK, "/dev/kvm exists and you can use it.")
    elif k.device_exists:
        user = os.environ.get("USER") or "your user"
        in_group_file = False
        if k.device_group:
            try:
                import grp
                in_group_file = user in grp.getgrnam(k.device_group).gr_mem
            except KeyError:
                pass
        if in_group_file:
            r.add("kvm.access", "CPU", "KVM permission", ERROR,
                  f"You were added to the '{k.device_group}' group, but this login session "
                  "started before that.",
                  fix="Log out and back in (or reboot) so the new group membership applies.")
        else:
            grp_name = k.device_group or "kvm"
            r.add("kvm.access", "CPU", "KVM permission", ERROR,
                  f"/dev/kvm exists but you cannot open it ({k.error}).",
                  fix=f"Add yourself to the {grp_name} group, then log out and back in.",
                  commands=[f"sudo usermod -aG {grp_name} $USER"])
    elif h.cpu.arch in ("x86_64", "i386") and h.cpu.virt_extension:
        mod = k.module or "kvm_intel"
        r.add("kvm.access", "CPU", "KVM", ERROR,
              "Your CPU supports virtualization, but the KVM kernel module is not loaded.",
              fix=f"Load the {mod} module. If loading fails with \"Operation not supported\", "
                  "virtualization is disabled in your firmware settings — enable it there.",
              commands=[f"sudo modprobe {mod}"])
    elif h.cpu.arch == "aarch64":
        r.add("kvm.access", "CPU", "KVM", WARN,
              "/dev/kvm is missing. On ARM boards KVM requires the kernel to start at EL2; "
              "many boards' firmware does not allow this.",
              fix="Check your board's documentation for enabling the hypervisor (EL2). "
                  "VMs will use slower emulation until then.")
    elif h.cpu.running_under_hypervisor:
        r.add("kvm.access", "CPU", "KVM", ERROR,
              "/dev/kvm does not exist because this system is a VM without nested "
              "virtualization; VMs will use slow software emulation.",
              fix="Enable nested virtualization on the outer hypervisor (see above).")
    else:
        r.add("kvm.access", "CPU", "KVM", WARN if h.cpu.arch not in ("x86_64", "i386") else ERROR,
              "/dev/kvm does not exist; VMs will use slow software emulation.",
              fix="Enable virtualization in your firmware settings and make sure the KVM "
                  "module for your CPU is loaded.")
    if k.nested is not None:
        if k.nested:
            r.add("kvm.nested", "CPU", "Nested virtualization", OK,
                  "Enabled: VMs can run their own hypervisors (WSL2, Hyper-V, Sandbox).")
        else:
            r.add("kvm.nested", "CPU", "Nested virtualization", INFO,
                  "Disabled. Only needed to run hypervisors (WSL2, Hyper-V, Docker Desktop) "
                  "inside a VM.",
                  fix="To enable it permanently, create a modprobe option and reload the module "
                      "while no VMs are running.",
                  commands=[f"echo 'options {k.module} nested=1' | sudo tee /etc/modprobe.d/rehome-nested.conf",
                            f"sudo modprobe -r {k.module} && sudo modprobe {k.module}"])


def check_resources(r: Report, h: HostInfo) -> None:
    m = h.memory
    r.facts["memory"] = {"total_mib": m.total_mib, "available_mib": m.available_mib,
                         "default_vm_mib": m.default_vm_mib(), "max_vm_mib": m.max_vm_mib}
    detail = (f"{human(m.total_mib * 2**20)} total, {human(m.available_mib * 2**20)} available. "
              f"New VMs default to {human(m.default_vm_mib() * 2**20)}; a single VM may use up to "
              f"{human(m.max_vm_mib * 2**20)}.")
    if m.total_mib < 4096:
        r.add("mem", "Resources", "Memory", WARN, detail,
              fix="This machine has little RAM. Modern Windows and desktop Linux guests need "
                  "4 GiB or more; consider lighter guests.")
    else:
        r.add("mem", "Resources", "Memory", OK, detail)
    lib = paths.data_dir()
    probe = lib if lib.exists() else lib.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = 0
    r.facts["disk_free_bytes"] = free
    st = OK if free > 30 * 2**30 else WARN
    r.add("disk", "Resources", "Space for VMs", st,
          f"{human(free)} free in {probe}.",
          fix="" if st == OK else "Free up space or move the VM library: copied imports need as "
                                  "much space as the used part of the source disk.")


def check_system(r: Report, h: HostInfo) -> None:
    osr, s = h.os_release, h.session
    r.facts["system"] = {"distro": osr.pretty_name, "id": osr.id, "family": osr.family,
                         "package_manager": h.package_manager, "desktop": s.desktop,
                         "session": s.session_type}
    r.add("sys.distro", "System", "Distribution", OK,
          f"{osr.pretty_name} (family: {osr.family}, package manager: {h.package_manager or 'unknown'})")
    if osr.family not in packages.INSTALL_TEMPLATES:
        r.add("sys.pkgmgr", "System", "Package manager", WARN,
              "This distribution is not one Rehome knows how to install packages on.",
              fix="Install the programs listed below with your distribution's tools.")
    if s.is_graphical:
        r.add("sys.session", "System", "Desktop session", OK,
              f"{s.desktop} on {'Wayland' if s.session_type == 'wayland' else 'X11' if s.session_type == 'x11' else s.session_type}")
    else:
        r.add("sys.session", "System", "Desktop session", INFO,
              "No graphical session detected; the command line works, the GUI needs a desktop.")
    if os.geteuid() == 0:
        r.add("sys.root", "System", "Running as root", WARN,
              "Rehome should run as your normal user. It asks for permission only when it "
              "needs it (for example to open a physical disk).",
              fix="Run rehome without sudo.")


def check_qemu(r: Report, h: HostInfo) -> None:
    fam = h.os_release.family
    binary = qemu_mod.system_binary(h.arch if h.arch in qemu_mod.SYSTEM_BINARY else "x86_64")
    r.facts["qemu"] = {"binaries": h.qemu_binaries}
    if not binary:
        r.add("qemu.binary", "Virtualization", "QEMU", ERROR,
              f"No QEMU system emulator for {h.arch} was found.",
              fix="Install QEMU.", commands=_install(["qemu", "ovmf"], fam))
        return
    caps = h.caps(h.arch if h.arch in qemu_mod.SYSTEM_BINARY else "x86_64")
    r.facts["qemu"].update({"binary": binary, "version": caps.version_str,
                            "displays": sorted(caps.displays), "accels": sorted(caps.accels)})
    if caps.probe_error:
        r.add("qemu.binary", "Virtualization", "QEMU", ERROR,
              f"QEMU {caps.version_str} at {binary} could not be started.",
              fix="Reinstall QEMU.", commands=_install(["qemu"], fam))
        r.checks[-1].detail += f" ({caps.probe_error})"
        return
    if caps.version < MIN_QEMU:
        r.add("qemu.binary", "Virtualization", "QEMU", WARN,
              f"QEMU {caps.version_str} is older than the supported minimum "
              f"{'.'.join(map(str, MIN_QEMU))}.",
              fix="Update QEMU (your distribution may offer a newer version in backports).")
    else:
        others = ", ".join(sorted(qemu_mod.distinct_arches(h.qemu_binaries) - {os.path.basename(binary)[12:]}))
        r.add("qemu.binary", "Virtualization", "QEMU", OK,
              f"QEMU {caps.version_str} ({binary})" + (f"; also: {others}" if others else ""))
    if not qemu_mod.qemu_img():
        r.add("qemu.img", "Virtualization", "qemu-img", ERROR, "qemu-img is missing.",
              fix="Install the QEMU tools.", commands=_install(["qemu"], fam))
    if caps.dbus_display:
        r.add("qemu.dbus", "Virtualization", "Console display", OK,
              "QEMU's D-Bus display is available for the built-in console.")
    else:
        r.add("qemu.dbus", "Virtualization", "Console display", WARN,
              "QEMU's D-Bus display module is not installed, so the built-in console cannot "
              "show VMs (they still run; you can use an external SPICE viewer).",
              fix="Install the QEMU UI modules.", commands=_install(["qemu"], fam))
    accel = choose_accel(h, h.arch)
    r.facts["accel"] = asdict(accel)
    r.add("qemu.accel", "Virtualization", "Accelerator", OK if accel.accel == "kvm" else WARN,
          accel.reason if accel.accel == "kvm" else
          accel.reason + " VMs will run with software emulation (TCG), which is much slower.",
          fix="" if accel.accel == "kvm" else "Fix the CPU/KVM items above to enable KVM.")


def check_firmware(r: Report, h: HostInfo) -> None:
    fam = h.os_release.family
    arch = h.arch if h.arch in ("x86_64", "aarch64") else "x86_64"
    machine = "q35" if arch == "x86_64" else "virt"
    plain = choose_firmware(h, arch, machine, uefi=True)
    sb = choose_firmware(h, arch, machine, uefi=True, secure_boot=True, enrolled_keys=True)
    r.facts["firmware"] = {"uefi": plain.to_dict() if plain else None,
                           "uefi_secure_boot": sb.to_dict() if sb else None,
                           "descriptors": len(h.firmware(arch))}
    key = "ovmf" if arch == "x86_64" else "aavmf"
    if plain:
        r.add("fw.uefi", "Virtualization", "UEFI firmware", OK,
              f"{plain.executable} (from {os.path.basename(plain.path)})")
    else:
        r.add("fw.uefi", "Virtualization", "UEFI firmware", ERROR,
              "No UEFI firmware was found; UEFI guests (Windows 11, most modern Linux) cannot boot.",
              fix="Install the UEFI firmware package.", commands=_install([key], fam))
    if arch == "x86_64":
        if sb:
            r.add("fw.secureboot", "Virtualization", "Secure Boot firmware", OK,
                  f"{sb.executable} with Microsoft keys enrolled.")
        else:
            r.add("fw.secureboot", "Virtualization", "Secure Boot firmware", WARN,
                  "No Secure Boot firmware with Microsoft keys was found; Windows 11 needs it.",
                  fix="Install the full UEFI firmware package.", commands=_install([key], fam))


def check_tools(r: Report, h: HostInfo) -> None:
    fam = h.os_release.family
    inv = {t.key: t for t in tools_mod.inventory()}
    r.facts["tools"] = {k: {"found": t.found, "path": t.path, "version": t.version}
                        for k, t in inv.items()}

    def tool(key, section, title, status_missing, msg_missing, pkg_key, ok_msg=None):
        t = inv[key]
        if t.found:
            r.add(f"tool.{key}", section, title, OK,
                  ok_msg or f"{t.label} {t.version}".strip() + (f" — {t.note}" if t.note else ""))
        else:
            r.add(f"tool.{key}", section, title, status_missing, msg_missing,
                  fix=f"Install {t.label}.", commands=_install([pkg_key], fam))

    tool("swtpm", "Virtualization", "Software TPM", WARN,
         "swtpm is missing: Windows 11 and BitLocker guests need a TPM 2.0.", "swtpm")
    if inv["swtpm"].found and not inv["swtpm_setup"].found:
        r.add("tool.swtpm_setup", "Virtualization", "TPM setup", WARN,
              "swtpm_setup is missing, so new TPMs cannot be initialised.",
              fix="Install the swtpm tools.", commands=_install(["swtpm"], fam))
    tool("libguestfs", "Import", "libguestfs", ERROR,
         "libguestfs' Python bindings are missing: imported systems cannot be identified or "
         "patched.", "libguestfs", ok_msg="libguestfs Python bindings are installed.")
    if inv["libguestfs"].found:
        check_appliance_kernel(r, fam)
    tool("virt-v2v", "Import", "virt-v2v", INFO,
         "virt-v2v is not installed. Rehome's own patcher will be used; virt-v2v adds "
         "automatic virtio driver installation for Windows 7+ and some Linux distributions.",
         "virt-v2v")
    tool("nbdkit", "Import", "nbdkit", WARN,
         "nbdkit is missing: physical disks cannot be inspected or copied.", "nbdkit")
    tool("smartctl", "Import", "Disk health (SMART)", WARN,
         "smartmontools is missing: disk health cannot be checked before import.", "smartmontools")
    tool("ddrescue", "Import", "ddrescue", INFO,
         "GNU ddrescue is not installed; it is recommended for imaging failing disks.", "ddrescue")
    tool("ewfmount", "Import", "E01 images", INFO,
         "libewf tools are not installed; E01 forensic images cannot be imported.", "libewf")
    tool("virtiofsd", "Virtualization", "Shared folders", INFO,
         "virtiofsd is not installed; shared folders are unavailable.", "virtiofsd")
    tool("passt", "Virtualization", "NAT networking", INFO,
         "passt is not installed; QEMU's built-in (slower) user networking will be used.", "passt")
    tool("xorriso", "Create", "Unattended install media", INFO,
         "xorriso is not installed; unattended installs that need an answer-file ISO are "
         "unavailable.", "media-tools")
    tool("libosinfo", "Create", "OS recognition (libosinfo)", WARN,
         "libosinfo is missing: installer ISOs cannot be recognised automatically.", "libosinfo")
    gtk, adw = inv["gtk4"], inv["libadwaita"]
    if gtk.found and adw.found:
        ver = tuple(int(x) for x in adw.version.split(".")[:2]) if adw.version else (0, 0)
        if ver < (1, 4):
            r.add("ui.libs", "Desktop", "GUI libraries", WARN,
                  f"libadwaita {adw.version} is too old for the GUI (needs 1.4 or newer). "
                  "The command line works fully.",
                  fix="Upgrade to a newer release of your distribution.")
        else:
            r.add("ui.libs", "Desktop", "GUI libraries", OK,
                  f"GTK {gtk.version}, libadwaita {adw.version}")
    else:
        r.add("ui.libs", "Desktop", "GUI libraries", WARN,
              "GTK 4 or libadwaita is missing; only the command line is available.",
              fix="Install GTK 4 and libadwaita.", commands=_install(["python-gi", "gtk4"], fam))
    helper = tools_mod.helper_installed()
    policy = tools_mod.polkit_policy_installed()
    pk = inv["pkexec"].found
    if helper and policy and pk:
        r.add("helper", "Import", "Permission helper", OK,
              f"{helper} with its polkit policy is installed.")
    else:
        missing = [n for n, ok in (("helper", helper), ("polkit policy", policy), ("pkexec", pk))
                   if not ok]
        r.add("helper", "Import", "Permission helper", WARN,
              "Physical disks and USB pass-through need Rehome's permission helper; missing: "
              + ", ".join(missing) + ". Disk images still work.",
              fix="Install Rehome with install.sh or your distribution package, which sets "
                  "this up." + ("" if pk else " pkexec is also required."),
              commands=_install(["polkit"], fam) if not pk else [])
    br = qemu_mod.bridge_helper()
    if br:
        setuid = bool(os.stat(br).st_mode & 0o4000)
        allowed = qemu_mod.bridge_conf_allows()
        if setuid and allowed:
            r.add("net.bridge", "Virtualization", "Bridged networking", OK,
                  f"qemu-bridge-helper may attach to: {', '.join(allowed)}")
        else:
            why = [] if setuid else ["qemu-bridge-helper is not setuid root"]
            if not allowed:
                why.append("/etc/qemu/bridge.conf allows no bridges")
            r.add("net.bridge", "Virtualization", "Bridged networking", INFO,
                  "Bridged networking is not set up (" + "; ".join(why) + "). NAT works without it.",
                  fix="Create a bridge (for example br0) with NetworkManager, then allow it.",
                  commands=["echo 'allow br0' | sudo tee -a /etc/qemu/bridge.conf"] +
                           ([] if setuid else [f"sudo chmod u+s {br}"]))


def check_appliance_kernel(r: Report, family: str) -> None:
    """libguestfs (supermin) needs a readable host kernel; Ubuntu ships it 0600."""
    if os.environ.get("LIBGUESTFS_BACKEND", "").startswith("libvirt"):
        return
    if os.environ.get("SUPERMIN_KERNEL") or os.path.isdir("/usr/lib64/guestfs/appliance") \
            or os.path.isdir("/usr/lib/guestfs/appliance") or glob.glob("/usr/lib/*/guestfs/appliance"):
        return
    kernels = sorted(glob.glob("/boot/vmlinuz-*")) + sorted(glob.glob("/lib/modules/*/vmlinuz"))
    if not kernels:
        return
    unreadable = [k for k in kernels if not os.access(k, os.R_OK)]
    if unreadable and len(unreadable) == len(kernels):
        cmds = [f"sudo dpkg-statoverride --update --add root root 0644 {k}" for k in unreadable[-1:]] \
            if family == "debian" else [f"sudo chmod 0644 {k}" for k in unreadable[-1:]]
        r.add("tool.appliance", "Import", "libguestfs appliance", WARN,
              "libguestfs needs to read your kernel image to build its helper appliance, "
              "but it is readable only by root.",
              fix="Make the kernel readable (this is what libguestfs' documentation "
                  "recommends on Ubuntu).", commands=cmds)


def check_gpu(r: Report, h: HostInfo, deep: bool) -> None:
    gpus = gpu_mod.enumerate_gpus()
    caps = h.caps("x86_64" if h.arch in ("x86_64", "i386") else h.arch)
    r.facts["gpus"] = [asdict(g) for g in gpus]
    if not gpus:
        r.add("gpu", "Graphics", "GPU", INFO,
              "No GPU with a DRM driver was found; 3D acceleration for VMs is unavailable.")
        return
    for g in gpus:
        r.add(f"gpu.{g.card}", "Graphics", "GPU", OK,
              f"{g.label} — driver {g.driver or 'none'}" +
              (f", render node {g.render_node}" if g.render_node else ""))
    if caps is None or not caps.virgl:
        r.add("gpu.virgl", "Graphics", "3D acceleration (virgl)", INFO,
              "This QEMU build has no virtio-gpu GL support; VMs use 2D graphics.",
              commands=_install(["qemu"], h.os_release.family))
        return
    node = next((g for g in gpus if g.render_node), None)
    if not node or not node.render_node_access:
        r.add("gpu.virgl", "Graphics", "3D acceleration (virgl)", INFO,
              "No accessible GPU render node; 3D acceleration is unavailable.",
              fix="Make sure you are in the 'render' (or 'video') group.",
              commands=["sudo usermod -aG render $USER"])
        return
    if deep:
        ok, err = gpu_mod.probe_egl(caps.binary, node.render_node)
        r.facts["egl"] = {"ok": ok, "error": err}
        if not ok:
            r.add("gpu.virgl", "Graphics", "3D acceleration (virgl)", WARN,
                  "QEMU could not initialise OpenGL on your GPU, so 3D acceleration is off.",
                  fix="Make sure Mesa (or the NVIDIA driver with GBM support) is installed.")
            r.checks[-1].detail += f" ({err})"
            return
    r.add("gpu.virgl", "Graphics", "3D acceleration (virgl)", OK,
          "Available for Linux guests" + (" (venus/Vulkan also supported)." if caps.venus else "."))


def run(deep: bool = True, refresh: bool = False) -> Report:
    h = gather(refresh=refresh)
    r = Report()
    check_cpu(r, h)
    check_kvm(r, h)
    check_resources(r, h)
    check_system(r, h)
    check_qemu(r, h)
    check_firmware(r, h)
    check_tools(r, h)
    check_gpu(r, h, deep)
    r.facts["desktop_family"] = desktop_family(h.session.desktop)
    return r
