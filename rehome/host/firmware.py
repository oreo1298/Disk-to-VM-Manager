"""Firmware discovery from QEMU firmware descriptor files.

Implements the lookup rules of QEMU's ``docs/interop/firmware.json``:
descriptors live in ``/usr/share/qemu/firmware``, ``/etc/qemu/firmware`` and
``$XDG_CONFIG_HOME/qemu/firmware``. Files are ordered by file name across all
directories; a file in a later directory (``/etc``, then the user's config)
replaces a same-named file from an earlier one, and an empty file masks it.

Distros put the actual firmware in different places (``/usr/share/OVMF``,
``/usr/share/edk2/ovmf``, ``/usr/share/edk2/x64``, ...) and some ship qcow2
images, so we never hard-code paths unless no descriptors exist at all.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SYSTEM_DIRS = ("/usr/share/qemu/firmware", "/etc/qemu/firmware")


@dataclass
class Firmware:
    path: str                       # descriptor file
    description: str
    interface_types: list[str]      # bios / uefi / openfirmware / slof / uboot
    device: str                     # flash / memory / kernel
    executable: str
    executable_format: str = "raw"
    nvram_template: str = ""
    nvram_format: str = "raw"
    mode: str = "split"             # split / combined / stateless
    targets: list[tuple[str, list[str]]] = field(default_factory=list)
    features: set[str] = field(default_factory=set)

    @property
    def is_uefi(self) -> bool:
        return "uefi" in self.interface_types

    @property
    def secure_boot(self) -> bool:
        return "secure-boot" in self.features

    @property
    def enrolled_keys(self) -> bool:
        return "enrolled-keys" in self.features

    @property
    def requires_smm(self) -> bool:
        return "requires-smm" in self.features

    def supports(self, arch: str, machine: str) -> bool:
        for t_arch, machines in self.targets:
            if t_arch != arch:
                continue
            if any(fnmatch.fnmatch(machine, m) for m in machines):
                return True
        return False

    def to_dict(self) -> dict:
        return {
            "descriptor": self.path, "description": self.description,
            "interface_types": self.interface_types, "device": self.device,
            "executable": self.executable, "executable_format": self.executable_format,
            "nvram_template": self.nvram_template, "nvram_format": self.nvram_format,
            "features": sorted(self.features),
        }


def descriptor_dirs() -> list[str]:
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return list(SYSTEM_DIRS) + [os.path.join(xdg, "qemu", "firmware")]


def _collect_files(dirs: list[str]) -> list[str]:
    by_name: dict[str, str] = {}
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.endswith(".json"):
                by_name[n] = os.path.join(d, n)
    result = []
    for n in sorted(by_name):
        p = by_name[n]
        try:
            if os.path.getsize(p) == 0:
                continue  # masked
        except OSError:
            continue
        result.append(p)
    return result


def parse_descriptor(path: str, data: dict) -> Firmware | None:
    mapping = data.get("mapping") or {}
    device = mapping.get("device", "")
    if device == "flash":
        exe = mapping.get("executable") or {}
        nv = mapping.get("nvram-template") or {}
        executable = exe.get("filename", "")
        exe_fmt = exe.get("format", "raw")
        nvram, nv_fmt = nv.get("filename", ""), nv.get("format", "raw")
        mode = mapping.get("mode", "split")
    elif device == "memory":
        executable, exe_fmt = mapping.get("filename", ""), "raw"
        nvram, nv_fmt, mode = "", "raw", "stateless"
    elif device == "kernel":
        executable, exe_fmt = mapping.get("filename", ""), "raw"
        nvram, nv_fmt, mode = "", "raw", "stateless"
    else:
        return None
    if not executable:
        return None
    targets = []
    for t in data.get("targets") or []:
        targets.append((t.get("architecture", ""), list(t.get("machines") or [])))
    return Firmware(
        path=path, description=data.get("description", ""),
        interface_types=list(data.get("interface-types") or []),
        device=device, executable=executable, executable_format=exe_fmt,
        nvram_template=nvram, nvram_format=nv_fmt, mode=mode,
        targets=targets, features=set(data.get("features") or []),
    )


def load_descriptors(dirs: list[str] | None = None) -> list[Firmware]:
    out = []
    for p in _collect_files(dirs or descriptor_dirs()):
        try:
            with open(p) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        fw = parse_descriptor(p, data)
        if fw and os.path.exists(fw.executable):
            out.append(fw)
    return out


# Known locations used only when a distro ships no descriptors at all.
FALLBACK_UEFI = {
    "x86_64": [
        # (code, vars, secure_boot, enrolled_keys)
        ("/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd", True, False),
        ("/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/edk2/x64/OVMF_VARS.4m.fd", False, False),
        ("/usr/share/OVMF/OVMF_CODE_4M.ms.fd", "/usr/share/OVMF/OVMF_VARS_4M.ms.fd", True, True),
        ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd", False, False),
        ("/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd", "/usr/share/edk2/ovmf/OVMF_VARS.secboot.fd", True, True),
        ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd", False, False),
        ("/usr/share/qemu/ovmf-x86_64-ms-code.bin", "/usr/share/qemu/ovmf-x86_64-ms-vars.bin", True, True),
        ("/usr/share/qemu/ovmf-x86_64-code.bin", "/usr/share/qemu/ovmf-x86_64-vars.bin", False, False),
    ],
    "aarch64": [
        ("/usr/share/AAVMF/AAVMF_CODE.fd", "/usr/share/AAVMF/AAVMF_VARS.fd", False, False),
        ("/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw", "/usr/share/edk2/aarch64/vars-template-pflash.raw", False, False),
        ("/usr/share/edk2/aarch64/QEMU_EFI-silent-pflash.raw", "/usr/share/edk2/aarch64/vars-template-pflash.raw", False, False),
    ],
}


def fallback_firmware(arch: str) -> list[Firmware]:
    out = []
    for code, nvram, sb, keys in FALLBACK_UEFI.get(arch, []):
        if os.path.exists(code) and os.path.exists(nvram):
            feats = set()
            if sb:
                feats |= {"secure-boot", "requires-smm"}
            if keys:
                feats.add("enrolled-keys")
            machines = ["pc-q35-*"] if sb else ["pc-q35-*", "pc-i440fx-*"]
            if arch == "aarch64":
                machines = ["virt-*"]
            out.append(Firmware(path="(built-in fallback)", description=f"UEFI firmware {code}",
                                interface_types=["uefi"], device="flash", executable=code,
                                nvram_template=nvram, targets=[(arch, machines)], features=feats))
    return out


@dataclass
class FirmwareRequest:
    arch: str
    machine: str            # versioned machine name, e.g. pc-q35-8.2
    uefi: bool = True
    secure_boot: bool = False
    enrolled_keys: bool = False   # Microsoft keys pre-enrolled (Windows 11)


def select(req: FirmwareRequest, available: list[Firmware]) -> Firmware | None:
    """Pick the best descriptor for ``req`` (descriptors are priority-ordered)."""
    candidates = []
    for fw in available:
        if req.uefi != fw.is_uefi:
            continue
        if fw.device != "flash" or fw.mode == "stateless":
            # Memory-mapped (e.g. AMD SEV) builds cannot keep variables.
            continue
        if not fw.supports(req.arch, req.machine):
            continue
        if any(f in fw.features for f in ("amd-sev-snp", "intel-tdx")) and \
                "secure-boot" not in fw.features:
            continue
        if req.secure_boot != fw.secure_boot:
            continue
        if req.secure_boot and req.enrolled_keys and not fw.enrolled_keys:
            continue
        if not req.enrolled_keys and fw.enrolled_keys and req.secure_boot:
            # Prefer, but don't require, an empty key store when not asked.
            candidates.append((1, fw))
            continue
        if not fw.nvram_template and fw.mode == "split":
            continue
        candidates.append((0, fw))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])  # stable: keeps descriptor priority
    return candidates[0][1]


def _for_arch(fw: Firmware, arch: str) -> bool:
    return any(t[0] == arch for t in fw.targets)


def enrolled_dir():
    from rehome import paths
    return paths.data_dir() / "firmware"


def self_enrolled(available: list[Firmware], arch: str) -> list[Firmware]:
    """Secure Boot firmware with Microsoft's keys enrolled by Rehome itself.

    Some distributions (Arch) ship Secure Boot capable OVMF but only an empty
    variable store. Windows 11 needs Microsoft's keys, so Rehome enrolls them
    into a copy of the store with ``virt-fw-vars`` (package virt-firmware),
    the tool Fedora uses to build its pre-enrolled store. The copy is made
    once per template (keyed by its content) under the user's data dir.
    """
    if arch != "x86_64" or any(f.secure_boot and f.enrolled_keys and _for_arch(f, arch)
                               for f in available):
        return []
    from rehome.util.proc import run, which
    tool = which("virt-fw-vars")
    out, seen = [], set()
    for fw in available:
        if not (fw.is_uefi and fw.secure_boot and not fw.enrolled_keys and _for_arch(fw, arch)
                and fw.device == "flash" and fw.mode == "split" and fw.nvram_template
                and fw.nvram_format == "raw" and os.path.isfile(fw.nvram_template)):
            continue
        try:
            with open(fw.nvram_template, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            continue
        stem = os.path.basename(fw.nvram_template).split(".")[0]
        target = enrolled_dir() / f"{stem}-{digest}.ms.fd"
        if not target.exists():
            if not tool or digest in seen:
                continue
            seen.add(digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            try:
                run([tool, "--input", fw.nvram_template, "--output", str(tmp),
                     "--enroll-redhat", "--secure-boot"], timeout=120)
                os.replace(tmp, target)
            except Exception as e:  # noqa: BLE001 - reported by `rehome doctor`
                log.warning("could not enroll Secure Boot keys: %s", e)
                tmp.unlink(missing_ok=True)
                continue
        out.append(dataclasses.replace(
            fw, path=f"{fw.path} (Microsoft keys enrolled by Rehome)",
            description=fw.description + ", Microsoft keys enrolled by Rehome",
            nvram_template=str(target), features=fw.features | {"enrolled-keys"}))
    return out


def discover(arch: str) -> list[Firmware]:
    descs = load_descriptors()
    if not any(f.is_uefi and _for_arch(f, arch) for f in descs):
        descs += fallback_firmware(arch)
    return descs + self_enrolled(descs, arch)
