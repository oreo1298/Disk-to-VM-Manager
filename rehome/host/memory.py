"""Host RAM detection and VM sizing policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class MemoryInfo:
    total_mib: int
    available_mib: int
    hugepages_total: int = 0

    # Keep this much for the host no matter what.
    @property
    def host_reserve_mib(self) -> int:
        # 2 GiB, or 25 % on small machines, whichever is larger (max 8 GiB).
        return min(8192, max(2048, self.total_mib // 4))

    @property
    def max_vm_mib(self) -> int:
        """The largest single VM we allow without an explicit override."""
        return max(256, self.total_mib - self.host_reserve_mib)

    def default_vm_mib(self, recommended: int | None = None,
                       minimum: int | None = None) -> int:
        """A sensible default: the OS recommendation, capped to a share of the host."""
        cap = max(512, min(self.max_vm_mib, self.total_mib // 2))
        want = recommended or 4096
        if minimum:
            want = max(want, minimum)
        return int(min(want, cap))


def parse_meminfo(text: str) -> MemoryInfo:
    vals = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            vals[k.strip()] = int(parts[0])
    total = vals.get("MemTotal", 0) // 1024
    avail = vals.get("MemAvailable", vals.get("MemFree", 0)) // 1024
    return MemoryInfo(total_mib=total, available_mib=avail,
                      hugepages_total=vals.get("HugePages_Total", 0))


def detect_memory(proc_root: str = "/proc") -> MemoryInfo:
    try:
        return parse_meminfo(Path(proc_root, "meminfo").read_text())
    except OSError:
        return MemoryInfo(total_mib=0, available_mib=0)
