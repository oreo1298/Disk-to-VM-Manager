"""Aggregated host facts and the policy that turns them into VM defaults."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from rehome.host import cpu as cpu_mod
from rehome.host import firmware as fw_mod
from rehome.host import memory as mem_mod
from rehome.host import qemu as qemu_mod
from rehome.host import system as sys_mod


@dataclass
class HostInfo:
    cpu: cpu_mod.CpuInfo
    kvm: cpu_mod.KvmStatus
    memory: mem_mod.MemoryInfo
    topology: tuple[int, int, int]
    os_release: sys_mod.OsRelease
    package_manager: str | None
    session: sys_mod.Session
    qemu_binaries: dict[str, str] = field(default_factory=dict)
    _caps: dict[str, qemu_mod.QemuCaps] = field(default_factory=dict, repr=False)
    _firmware: dict[str, list[fw_mod.Firmware]] = field(default_factory=dict, repr=False)

    @property
    def arch(self) -> str:
        return self.cpu.arch

    @property
    def kvm_usable(self) -> bool:
        return self.kvm.device_exists and self.kvm.accessible

    @property
    def threads(self) -> int:
        return self.topology[2]

    @property
    def cores(self) -> int:
        return self.topology[1]

    def caps(self, guest_arch: str) -> qemu_mod.QemuCaps | None:
        binary = qemu_mod.system_binary(guest_arch)
        if not binary:
            return None
        if binary not in self._caps:
            self._caps[binary] = qemu_mod.capabilities(binary)
        return self._caps[binary]

    def firmware(self, guest_arch: str) -> list[fw_mod.Firmware]:
        arch = "x86_64" if guest_arch in ("i386", "i686") else guest_arch
        if arch not in self._firmware:
            self._firmware[arch] = fw_mod.discover(arch)
        return self._firmware[arch]


_lock = threading.Lock()
_cached: HostInfo | None = None


def gather(refresh: bool = False) -> HostInfo:
    global _cached
    with _lock:
        if _cached is not None and not refresh:
            return _cached
        if refresh:
            # Programs may have been installed since (Host Check's "Check again").
            from rehome.util.proc import which
            which.cache_clear()
        c = cpu_mod.detect_cpu()
        osr = sys_mod.detect_os_release()
        _cached = HostInfo(
            cpu=c, kvm=cpu_mod.detect_kvm(c), memory=mem_mod.detect_memory(),
            topology=cpu_mod.cpu_topology(), os_release=osr,
            package_manager=sys_mod.detect_package_manager(osr),
            session=sys_mod.detect_session(),
            qemu_binaries=qemu_mod.installed_system_binaries(),
        )
        return _cached


# --- Policy -----------------------------------------------------------------

KVM_COMPATIBLE = {
    # host arch -> guest arches KVM can run natively
    "x86_64": {"x86_64", "i386", "i686"},
    "aarch64": {"aarch64"},
    "ppc64": {"ppc64"},
    "s390x": {"s390x"},
    "riscv64": {"riscv64"},
    "loongarch64": {"loongarch64"},
}


@dataclass
class AccelChoice:
    accel: str               # kvm / tcg
    reason: str
    warning: str = ""


def choose_accel(host: HostInfo, guest_arch: str, prefer: str = "auto",
                 prefer_reason: str = "") -> AccelChoice:
    guest_arch = cpu_mod.canonical_arch(guest_arch)
    native = guest_arch in KVM_COMPATIBLE.get(host.arch, set())
    caps = host.caps(guest_arch)
    kvm_in_qemu = caps is None or not caps.accels or "kvm" in caps.accels
    if prefer == "tcg":
        return AccelChoice("tcg", prefer_reason or "This guest is configured to use emulation.",
                           "" if prefer_reason else
                           "Emulation (TCG) is much slower than KVM.")
    if native and host.kvm_usable and kvm_in_qemu:
        return AccelChoice("kvm", "Hardware virtualization (KVM) is available.")
    if not native:
        return AccelChoice(
            "tcg", f"A {guest_arch} guest cannot use KVM on this {host.arch} host.",
            "This VM will run with software emulation (TCG), which is much slower — "
            "expect it to feel 5–20× slower than a native VM.")
    why = "KVM is not available on this machine"
    if not host.kvm.device_exists:
        why = "/dev/kvm does not exist (virtualization is off or the KVM module is not loaded)"
    elif not host.kvm.accessible:
        why = "you do not have permission to use /dev/kvm"
    elif not kvm_in_qemu:
        why = "this QEMU build does not support KVM"
    return AccelChoice(
        "tcg", f"Falling back to emulation because {why}.",
        "Without KVM the VM runs with software emulation (TCG), which is much slower. "
        "Run `rehome doctor` to see how to enable KVM.")


def choose_machine(host: HostInfo, guest_arch: str, family: str = "q35") -> str:
    """Return a machine alias the installed QEMU supports (q35 / pc / virt)."""
    guest_arch = cpu_mod.canonical_arch(guest_arch)
    if guest_arch in ("aarch64", "arm", "riscv64", "loongarch64"):
        return "virt"
    caps = host.caps(guest_arch)
    if caps and caps.machines and not caps.has_machine(family):
        return "pc" if caps.has_machine("pc") else next(iter(caps.machines))
    return family


def choose_firmware(host: HostInfo, guest_arch: str, machine: str, uefi: bool,
                    secure_boot: bool = False, enrolled_keys: bool = False
                    ) -> fw_mod.Firmware | None:
    if not uefi:
        return None
    caps = host.caps(guest_arch)
    versioned = caps.resolve_machine(machine) if caps else machine
    if versioned == machine:
        # Without a resolvable alias, fall back to a glob-compatible guess.
        versioned = {"q35": "pc-q35-99.0", "pc": "pc-i440fx-99.0", "virt": "virt-99.0"}.get(machine, machine)
    arch = "x86_64" if guest_arch in ("i386", "i686") else guest_arch
    req = fw_mod.FirmwareRequest(arch=arch, machine=versioned, uefi=True,
                                 secure_boot=secure_boot, enrolled_keys=enrolled_keys)
    return fw_mod.select(req, host.firmware(arch))


def default_cpus(host: HostInfo, recommended: int | None = None) -> int:
    """Half the host threads (at least 1, at most the recommendation or 8)."""
    cap = max(1, host.threads // 2)
    want = recommended or min(4, cap)
    return max(1, min(want, cap, 8 if recommended is None else 64))
