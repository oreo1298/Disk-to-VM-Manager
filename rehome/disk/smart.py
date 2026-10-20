"""Disk health (SMART) assessment before touching a physical disk."""

from __future__ import annotations

from dataclasses import dataclass, field

from rehome.errors import RehomeError

# ATA attributes whose raw value > 0 indicates media problems.
CRITICAL_ATA = {
    5: "reallocated sectors",
    187: "uncorrectable errors reported",
    188: "command timeouts",
    197: "sectors waiting to be reallocated",
    198: "sectors that could not be read",
}

HEALTHY, WARNING, FAILING, UNKNOWN = "healthy", "warning", "failing", "unknown"


@dataclass
class Health:
    verdict: str
    summary: str
    details: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def blocks_import(self) -> bool:
        return self.verdict == FAILING


def assess(smart_json: dict) -> Health:
    """Interpret ``smartctl --json`` output."""
    if not smart_json:
        return Health(UNKNOWN, "The disk did not report any health data.")
    details: list[str] = []
    passed = (smart_json.get("smart_status") or {}).get("passed")
    verdict = HEALTHY if passed is not False else FAILING
    if passed is None:
        verdict = UNKNOWN
    for attr in (smart_json.get("ata_smart_attributes") or {}).get("table", []):
        aid = attr.get("id")
        raw = (attr.get("raw") or {}).get("value", 0) or 0
        if aid in CRITICAL_ATA and raw > 0:
            details.append(f"{raw} {CRITICAL_ATA[aid]}")
            if verdict == HEALTHY:
                verdict = WARNING
            if aid in (197, 198) and raw > 0:
                verdict = FAILING
        if attr.get("when_failed") not in (None, "", "-"):
            details.append(f"attribute {attr.get('name')} failed ({attr.get('when_failed')})")
            verdict = FAILING
    nvme = smart_json.get("nvme_smart_health_information_log") or {}
    if nvme:
        if nvme.get("critical_warning", 0):
            details.append(f"NVMe critical warning flags 0x{nvme['critical_warning']:x}")
            verdict = FAILING
        if nvme.get("media_errors", 0):
            details.append(f"{nvme['media_errors']} media errors")
            verdict = FAILING if verdict != FAILING else verdict
        used = nvme.get("percentage_used", 0)
        if used >= 100:
            details.append(f"rated endurance used up ({used}%)")
            if verdict == HEALTHY:
                verdict = WARNING
    errs = (smart_json.get("ata_smart_error_log") or {}).get("summary", {}).get("count", 0)
    if errs:
        details.append(f"{errs} errors in the drive's error log")
        if verdict == HEALTHY:
            verdict = WARNING
    summary = {
        HEALTHY: "The disk reports good health.",
        WARNING: "The disk shows signs of wear or past errors. Importing is possible, but consider "
                 "making an image of it first.",
        FAILING: "The disk is failing. Reading it repeatedly could make it worse.",
        UNKNOWN: "The disk's health could not be determined (USB adapters often hide it).",
    }[verdict]
    return Health(verdict, summary, details, smart_json)


def check(device: str) -> Health:
    from rehome.disk import privileged
    try:
        reply = privileged.smart(device)
    except RehomeError as e:
        if e.code in ("no-smartctl", "no-helper", "no-pkexec"):
            return Health(UNKNOWN, f"Disk health was not checked: {e.message}")
        raise
    return assess(reply.get("smart") or {})


def ddrescue_advice(device: str, name: str = "rescued-disk") -> list[str]:
    """Exact commands for imaging a failing disk safely (shown to the user)."""
    return [
        f"sudo ddrescue -d -n {device} ~/{name}.img ~/{name}.map",
        f"sudo ddrescue -d -r3 {device} ~/{name}.img ~/{name}.map",
        f"sudo chown $USER: ~/{name}.img ~/{name}.map",
        f"rehome import ~/{name}.img",
    ]
