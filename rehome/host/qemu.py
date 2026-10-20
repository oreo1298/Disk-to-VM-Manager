"""QEMU binary discovery and capability probing.

Capabilities are probed once per QEMU build (binary + installed modules) and
cached under ``~/.cache/rehome``. Probing starts QEMU with ``-machine none``
and asks over QMP for machines, device types and CPU models, and parses the
``-display help`` / ``-audiodev help`` / ``-accel help`` listings.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rehome import paths
from rehome.host.cpu import canonical_arch
from rehome.util.proc import output, which

# Guest architecture -> qemu-system binary suffix.
SYSTEM_BINARY = {
    "x86_64": "x86_64", "i386": "x86_64", "i686": "x86_64",
    "aarch64": "aarch64", "arm": "arm", "ppc64": "ppc64", "ppc": "ppc",
    "riscv64": "riscv64", "s390x": "s390x", "loongarch64": "loongarch64",
    "mips": "mips", "sparc64": "sparc64",
}

MODULE_DIRS = ("/usr/lib/qemu", "/usr/lib64/qemu", "/usr/lib/x86_64-linux-gnu/qemu",
               "/usr/lib/aarch64-linux-gnu/qemu", "/usr/local/lib/qemu")

CACHE_VERSION = 3


def system_binary(arch: str) -> str | None:
    suffix = SYSTEM_BINARY.get(canonical_arch(arch), canonical_arch(arch))
    return which(f"qemu-system-{suffix}")


def installed_system_binaries() -> dict[str, str]:
    found = {}
    for d in os.environ.get("PATH", "/usr/bin").split(os.pathsep):
        for p in glob.glob(os.path.join(d, "qemu-system-*")):
            name = os.path.basename(p)[len("qemu-system-"):]
            if name not in found and os.access(p, os.X_OK):
                found[name] = p
    return found


def distinct_arches(binaries: dict[str, str]) -> set[str]:
    """Architectures of the installed emulators, without aliases and variants."""
    seen: dict[str, str] = {}
    for name, path in sorted(binaries.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if "-" in name or name == "amd64":
            continue  # x86_64-microvm, x86_64-spice wrappers, Debian amd64 alias
        real = os.path.realpath(path)
        seen.setdefault(real, name)
    return set(seen.values())


@dataclass
class Machine:
    name: str
    alias: str = ""
    is_default: bool = False
    deprecated: bool = False


@dataclass
class QemuCaps:
    binary: str
    version: tuple[int, int, int] = (0, 0, 0)
    package: str = ""
    machines: dict[str, Machine] = field(default_factory=dict)
    devices: set[str] = field(default_factory=set)
    cpu_models: set[str] = field(default_factory=set)
    accels: set[str] = field(default_factory=set)
    displays: set[str] = field(default_factory=set)
    audiodevs: set[str] = field(default_factory=set)
    virtio_gpu_props: set[str] = field(default_factory=set)
    probe_error: str = ""

    @property
    def version_str(self) -> str:
        return ".".join(map(str, self.version))

    def at_least(self, major: int, minor: int = 0) -> bool:
        return self.version >= (major, minor, 0)

    def resolve_machine(self, name: str) -> str:
        """Map an alias such as ``q35`` to the versioned type ``pc-q35-9.1``."""
        for m in self.machines.values():
            if m.alias == name:
                return m.name
        return name

    def has_machine(self, name: str) -> bool:
        return name in self.machines or any(m.alias == name for m in self.machines.values())

    def has_device(self, name: str) -> bool:
        return name in self.devices

    @property
    def dbus_display(self) -> bool:
        return "dbus" in self.displays

    @property
    def gl_capable(self) -> bool:
        return "egl-headless" in self.displays

    @property
    def virgl(self) -> bool:
        return self.gl_capable and ("virtio-vga-gl" in self.devices or
                                    "virtio-gpu-gl-pci" in self.devices)

    @property
    def venus(self) -> bool:
        return self.virgl and "venus" in self.virtio_gpu_props

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("devices", "cpu_models", "accels", "displays", "audiodevs", "virtio_gpu_props"):
            d[k] = sorted(d[k])
        d["machines"] = {k: asdict(v) for k, v in self.machines.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "QemuCaps":
        caps = cls(binary=d["binary"])
        caps.version = tuple(d.get("version", (0, 0, 0)))  # type: ignore[assignment]
        caps.package = d.get("package", "")
        caps.machines = {k: Machine(**v) for k, v in d.get("machines", {}).items()}
        for k in ("devices", "cpu_models", "accels", "displays", "audiodevs", "virtio_gpu_props"):
            setattr(caps, k, set(d.get(k, [])))
        caps.probe_error = d.get("probe_error", "")
        return caps


def parse_help_list(text: str) -> set[str]:
    """Parse ``-display help`` style output (one name per line after a header)."""
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.endswith(":") or " " in line:
            continue
        names.add(line)
    return names


def parse_version(text: str) -> tuple[tuple[int, int, int], str]:
    m = re.search(r"version (\d+)\.(\d+)\.(\d+)(?:\s*\(([^)]*)\))?", text)
    if not m:
        return (0, 0, 0), ""
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), (m.group(4) or "")


def _fingerprint(binary: str) -> str:
    h = hashlib.sha256()
    st = os.stat(binary)
    h.update(f"{CACHE_VERSION}:{binary}:{st.st_size}:{int(st.st_mtime)}".encode())
    for d in MODULE_DIRS:
        try:
            for n in sorted(os.listdir(d)):
                h.update(n.encode())
        except OSError:
            pass
    return h.hexdigest()[:20]


class _StdioQmp:
    """Tiny QMP client over a child's stdin/stdout, for probing only."""

    def __init__(self, binary: str):
        self.proc = subprocess.Popen(
            [binary, "-machine", "none", "-nodefaults", "-nographic", "-S",
             "-qmp", "stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, "LC_ALL": "C"})
        greeting = self._read()
        if "QMP" not in greeting:
            raise RuntimeError(f"unexpected QMP greeting: {greeting}")
        self.cmd("qmp_capabilities")

    def _read(self) -> dict:
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"QEMU exited during probe: {err.strip()}")
            msg = json.loads(line)
            if "event" not in msg:
                return msg

    def cmd(self, name: str, **args):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        reply = self._read()
        if "error" in reply:
            raise RuntimeError(reply["error"].get("desc", str(reply)))
        return reply.get("return")

    def close(self):
        try:
            self.cmd("quit")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def probe(binary: str) -> QemuCaps:
    caps = QemuCaps(binary=binary)
    env = {"LC_ALL": "C"}
    caps.version, caps.package = parse_version(output([binary, "--version"], env=env))
    caps.displays = parse_help_list(output([binary, "-display", "help"], env=env))
    caps.audiodevs = parse_help_list(output([binary, "-audiodev", "help"], env=env))
    caps.accels = parse_help_list(output([binary, "-accel", "help"], env=env))
    try:
        q = _StdioQmp(binary)
        try:
            for m in q.cmd("query-machines"):
                caps.machines[m["name"]] = Machine(
                    name=m["name"], alias=m.get("alias", ""),
                    is_default=m.get("is-default", False),
                    deprecated=m.get("deprecated", False))
            for t in q.cmd("qom-list-types", implements="device", abstract=False):
                caps.devices.add(t["name"])
            try:
                for c in q.cmd("query-cpu-definitions"):
                    caps.cpu_models.add(c["name"])
            except RuntimeError:
                pass
            for dev in ("virtio-gpu-gl-pci", "virtio-vga-gl"):
                if dev in caps.devices:
                    try:
                        props = q.cmd("device-list-properties", typename=dev)
                        caps.virtio_gpu_props = {p["name"] for p in props}
                    except RuntimeError:
                        pass
                    break
        finally:
            q.close()
    except (RuntimeError, OSError, ValueError) as e:
        caps.probe_error = str(e)
    return caps


def capabilities(binary: str, use_cache: bool = True) -> QemuCaps:
    cache = paths.cache_dir() / f"qemu-caps-{_fingerprint(binary)}.json"
    if use_cache and cache.exists():
        try:
            return QemuCaps.from_dict(json.loads(cache.read_text()))
        except (ValueError, KeyError, TypeError):
            pass
    caps = probe(binary)
    if not caps.probe_error:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(caps.to_dict()))
        except OSError:
            pass
    return caps


def qemu_img() -> str | None:
    return which("qemu-img")


def bridge_helper() -> str | None:
    return which("qemu-bridge-helper")


def bridge_conf_allows(path: str = "/etc/qemu/bridge.conf") -> list[str]:
    """Return bridge names allowed in bridge.conf (``all`` for wildcard)."""
    allowed = []
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "allow":
                allowed.append(parts[1])
    except OSError:
        pass
    return allowed
