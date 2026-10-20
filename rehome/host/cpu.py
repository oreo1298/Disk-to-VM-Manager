"""CPU, KVM and nested virtualization detection."""

from __future__ import annotations

import errno
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

# Map from `uname -m` to the canonical QEMU architecture name.
ARCH_ALIASES = {
    "x86_64": "x86_64", "amd64": "x86_64",
    "i386": "i386", "i486": "i386", "i586": "i386", "i686": "i386",
    "aarch64": "aarch64", "arm64": "aarch64",
    "armv7l": "arm", "armv6l": "arm",
    "ppc64le": "ppc64", "ppc64": "ppc64",
    "riscv64": "riscv64", "s390x": "s390x", "loongarch64": "loongarch64",
}


def canonical_arch(name: str) -> str:
    return ARCH_ALIASES.get(name.lower(), name.lower())


@dataclass
class CpuInfo:
    arch: str
    vendor: str = "unknown"
    model_name: str = ""
    flags: set[str] = field(default_factory=set)
    # "vmx", "svm", "arm-el2" or "" when the CPU (as visible) lacks it.
    virt_extension: str = ""
    # True when /proc/cpuinfo shows a "vmx flags" line (kernel >= 5.8 only
    # prints it when VMX is actually usable).
    vmx_flags_line: bool = False
    running_under_hypervisor: bool = False
    hypervisor_vendor: str = ""

    @property
    def vendor_label(self) -> str:
        return {"GenuineIntel": "Intel", "AuthenticAMD": "AMD",
                "HygonGenuine": "Hygon", "CentaurHauls": "Centaur",
                "  Shanghai  ": "Zhaoxin"}.get(self.vendor, self.vendor)

    @property
    def virt_name(self) -> str:
        if self.virt_extension == "vmx":
            return "Intel VT-x"
        if self.virt_extension == "svm":
            return "AMD-V (SVM)"
        if self.virt_extension == "arm-el2":
            return "ARM virtualization extensions"
        return "none"


def parse_cpuinfo(text: str, arch: str) -> CpuInfo:
    info = CpuInfo(arch=arch)
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "vendor_id" and info.vendor == "unknown":
            info.vendor = value
        elif key in ("model name", "cpu model", "hardware") and not info.model_name:
            info.model_name = value
        elif key in ("flags", "features") and not info.flags:
            info.flags = set(value.split())
        elif key == "vmx flags":
            info.vmx_flags_line = True
        elif key == "cpu implementer" and info.vendor == "unknown":
            info.vendor = ARM_IMPLEMENTERS.get(value.lower(), f"ARM implementer {value}")
    if "vmx" in info.flags:
        info.virt_extension = "vmx"
    elif "svm" in info.flags:
        info.virt_extension = "svm"
    info.running_under_hypervisor = "hypervisor" in info.flags
    return info


ARM_IMPLEMENTERS = {
    "0x41": "ARM", "0x42": "Broadcom", "0x43": "Cavium", "0x48": "HiSilicon",
    "0x4e": "NVIDIA", "0x50": "Ampere (APM)", "0x51": "Qualcomm",
    "0x61": "Apple", "0x69": "Intel", "0xc0": "Ampere",
}


def detect_cpu(proc_root: str = "/proc", sys_root: str = "/sys") -> CpuInfo:
    arch = canonical_arch(platform.machine())
    try:
        text = Path(proc_root, "cpuinfo").read_text(errors="replace")
    except OSError:
        text = ""
    info = parse_cpuinfo(text, arch)
    if arch == "aarch64":
        # KVM on arm64 needs the kernel to have booted at EL2. The kernel
        # exposes that indirectly: /sys/module/kvm exists only if KVM
        # initialised, and dmesg reports "kvm [1]: Hyp mode initialized".
        if Path(sys_root, "module/kvm").exists() or Path("/dev/kvm").exists():
            info.virt_extension = "arm-el2"
    hv = Path(sys_root, "hypervisor/type")
    if hv.exists():
        try:
            info.hypervisor_vendor = hv.read_text().strip()
            info.running_under_hypervisor = True
        except OSError:
            pass
    dmi_vendor = Path(sys_root, "class/dmi/id/sys_vendor")
    if info.running_under_hypervisor and not info.hypervisor_vendor and dmi_vendor.exists():
        try:
            info.hypervisor_vendor = dmi_vendor.read_text().strip()
        except OSError:
            pass
    return info


@dataclass
class KvmStatus:
    device_exists: bool
    accessible: bool
    error: str = ""          # errno name when open failed
    module: str = ""         # kvm_intel / kvm_amd / kvm (arm)
    module_loaded: bool = False
    nested: bool | None = None   # None = unknown / not applicable
    nested_param_path: str = ""
    device_group: str = ""
    user_in_group: bool = False


def detect_kvm(cpu: CpuInfo, dev: str = "/dev/kvm", sys_root: str = "/sys") -> KvmStatus:
    st = KvmStatus(device_exists=os.path.exists(dev), accessible=False)
    if st.device_exists:
        try:
            fd = os.open(dev, os.O_RDWR | os.O_CLOEXEC)
            os.close(fd)
            st.accessible = True
        except OSError as e:
            st.error = errno.errorcode.get(e.errno, str(e.errno))
        try:
            import grp
            gid = os.stat(dev).st_gid
            st.device_group = grp.getgrgid(gid).gr_name
            st.user_in_group = gid in os.getgroups() or gid == os.getgid()
        except (KeyError, OSError):
            pass
    if cpu.virt_extension == "vmx":
        st.module = "kvm_intel"
    elif cpu.virt_extension == "svm":
        st.module = "kvm_amd"
    elif cpu.arch == "aarch64":
        st.module = "kvm"
    else:
        # Flags hidden (e.g. disabled in firmware): guess from vendor.
        st.module = {"GenuineIntel": "kvm_intel", "AuthenticAMD": "kvm_amd",
                     "HygonGenuine": "kvm_amd"}.get(cpu.vendor, "kvm")
    mod_dir = Path(sys_root, "module", st.module)
    st.module_loaded = mod_dir.exists()
    nested = mod_dir / "parameters" / "nested"
    if nested.exists():
        st.nested_param_path = str(nested)
        try:
            v = nested.read_text().strip().upper()
            st.nested = v in ("Y", "1")
        except OSError:
            st.nested = None
    return st


def cpu_topology(sys_root: str = "/sys") -> tuple[int, int, int]:
    """Return (sockets, physical cores, logical threads) for online CPUs."""
    threads = os.cpu_count() or 1
    cores = set()
    sockets = set()
    base = Path(sys_root, "devices/system/cpu")
    for cpu in base.glob("cpu[0-9]*"):
        topo = cpu / "topology"
        try:
            pkg = (topo / "physical_package_id").read_text().strip()
            core = (topo / "core_id").read_text().strip()
        except OSError:
            continue
        sockets.add(pkg)
        cores.add((pkg, core))
    return (len(sockets) or 1, len(cores) or threads, threads)
