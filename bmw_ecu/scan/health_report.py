"""Aggregate a full-system scan into one customer-facing health report.

The orchestrator collects a `ModuleScanResult` per module (reachable +
decoded faults, or an "unreachable" marker). This module rolls those up
into a `HealthReport` with:

  • per-severity totals across the whole car,
  • a traffic-light overall status (GREEN / YELLOW / RED),
  • a "missing expected modules" list (a safety-critical module that
    didn't answer is a red flag, not a non-event),
  • a stable `to_dict()` the API + the printable PDF + the chatbot all
    render from.

Overall status policy
---------------------
  RED    — any SAFETY-severity fault anywhere, OR an expected
           safety-critical module didn't answer, OR any HARD fault.
  YELLOW — no RED conditions but at least one SOFT fault (pending /
           comfort / body / network).
  GREEN  — no faults above INFO and every expected module answered.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .dtc_decoder import DecodedDtc, DtcSeverity
from .module_map import EcuModule, describe_module, expected_module_codes


class OverallStatus(str, enum.Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass
class ModuleScanResult:
    module: EcuModule
    reachable: bool
    dtcs: list[DecodedDtc] = field(default_factory=list)
    cleared: bool = False
    note: str = ""

    @property
    def worst_severity(self) -> DtcSeverity:
        if not self.dtcs:
            return DtcSeverity.INFO
        return max(d.severity for d in self.dtcs)

    @property
    def fault_count(self) -> int:
        return len(self.dtcs)

    def to_dict(self) -> dict:
        return {
            "module": self.module.to_dict(),
            "reachable": self.reachable,
            "fault_count": self.fault_count,
            "worst_severity": self.worst_severity.name.lower(),
            "cleared": self.cleared,
            "note": self.note,
            "dtcs": [d.to_dict() for d in self.dtcs],
        }


@dataclass
class HealthReport:
    vin: str
    chassis_family: str
    results: list[ModuleScanResult] = field(default_factory=list)
    missing_modules: list[EcuModule] = field(default_factory=list)

    # ── severity tallies (filled by build_report) ────────────────────
    counts: dict[str, int] = field(default_factory=dict)
    overall: OverallStatus = OverallStatus.GREEN

    @property
    def total_faults(self) -> int:
        return sum(r.fault_count for r in self.results)

    @property
    def modules_scanned(self) -> int:
        return len(self.results)

    @property
    def modules_with_faults(self) -> int:
        return sum(1 for r in self.results if r.fault_count)

    def headline_ar(self) -> str:
        if self.overall is OverallStatus.GREEN:
            return "العربية سليمة ✅ — مفيش أعطال محفوظة."
        if self.overall is OverallStatus.YELLOW:
            return (f"في {self.total_faults} عطل بسيط/متقطّع محتاج متابعة "
                    f"⚠️ — مفيش خطر مباشر.")
        return (f"في أعطال خطيرة 🔴 — {self.total_faults} عطل، "
                f"بعضها في أنظمة أمان. لازم كشف فوري.")

    def to_dict(self) -> dict:
        return {
            "vin": self.vin,
            "chassis_family": self.chassis_family,
            "overall": self.overall.value,
            "headline_ar": self.headline_ar(),
            "total_faults": self.total_faults,
            "modules_scanned": self.modules_scanned,
            "modules_with_faults": self.modules_with_faults,
            "counts": dict(self.counts),
            "missing_modules": [m.to_dict() for m in self.missing_modules],
            "results": [r.to_dict() for r in self.results],
        }


def build_report(*, vin: str, chassis_family: str,
                 results: list[ModuleScanResult]) -> HealthReport:
    """Roll per-module results up into a HealthReport with totals +
    traffic-light status + missing-module detection."""
    report = HealthReport(vin=vin, chassis_family=chassis_family,
                          results=list(results))

    # Per-severity tally across every decoded fault.
    counts = {s.name.lower(): 0 for s in DtcSeverity}
    for r in results:
        for d in r.dtcs:
            counts[d.severity.name.lower()] += 1
    report.counts = counts

    # Which expected modules never produced a result row at all.
    seen = {r.module.code for r in results}
    try:
        expected = expected_module_codes(chassis_family)
    except (ValueError, KeyError):
        expected = ()
    report.missing_modules = [
        describe_module(code) for code in expected if code not in seen
    ]

    # A safety-critical module that was EXPECTED but unreachable (either
    # missing, or present-but-not-answered) is a RED condition.
    unreachable_safety = any(
        r.module.is_safety_critical and not r.reachable for r in results
    )
    missing_safety = any(m.is_safety_critical for m in report.missing_modules)

    if (counts["safety"] > 0 or counts["hard"] > 0
            or unreachable_safety or missing_safety):
        report.overall = OverallStatus.RED
    elif counts["soft"] > 0:
        report.overall = OverallStatus.YELLOW
    else:
        report.overall = OverallStatus.GREEN

    return report
