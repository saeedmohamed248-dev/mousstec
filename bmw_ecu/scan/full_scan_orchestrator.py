"""Full-System Auto-Scan orchestrator.

Drives the workshop's "scan the whole car" flow as a forward-only state
machine, chatbot-friendly the same way the premium orchestrators are:

  IDLE ──CONNECT──▶ CONNECTED ──SCAN_ALL──▶ REPORT_READY ──FINISH──▶ DONE
                        │                         │
                        └──────────┬──────────────┘
                                   ▼
                                 FAILED

  • CONNECT      — entitlement gate (feature 'full_system_scan'), read
                   VIN, list reachable modules. An unentitled session
                   never gets past IDLE; a dead gateway → FAILED.
  • SCAN_ALL     — pull + decode fault memory from every expected module
                   (reachable or not), build the HealthReport.
  • FINISH       — consume the grant (one scan = one use) and emit the
                   final report payload.

`clear_codes` is intentionally NOT part of the auto-scan grant — clearing
is a separate, more dangerous operation (you don't wipe an airbag's crash
memory by accident). It lives behind its own future feature; this
orchestrator only READS.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from .dtc_decoder import decode_dtc
from .health_report import HealthReport, ModuleScanResult, build_report
from .module_map import describe_module, expected_module_codes
from .scan_provider import AbstractScanProvider, ScanTransportError

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
class ScanState(str, enum.Enum):
    IDLE         = "idle"
    CONNECTED    = "connected"
    REPORT_READY = "report_ready"
    DONE         = "done"
    FAILED       = "failed"


_ALLOWED: dict[ScanState, set[ScanState]] = {
    ScanState.IDLE:         {ScanState.CONNECTED, ScanState.FAILED},
    ScanState.CONNECTED:    {ScanState.REPORT_READY, ScanState.FAILED},
    ScanState.REPORT_READY: {ScanState.DONE, ScanState.FAILED},
    ScanState.DONE:         set(),
    ScanState.FAILED:       set(),
}


class IllegalScanTransition(Exception):
    pass


class ScanEvent(str, enum.Enum):
    CONNECT  = "connect"
    SCAN_ALL = "scan_all"
    FINISH   = "finish"
    ABORT    = "abort"


_PROGRESS = {
    ScanState.IDLE: 0, ScanState.CONNECTED: 20,
    ScanState.REPORT_READY: 90, ScanState.DONE: 100, ScanState.FAILED: 0,
}


@dataclass
class ScanData:
    vin: str = ""
    chassis_family: str = ""
    technician_id: str = ""
    reachable_modules: list[str] = field(default_factory=list)
    report: Optional[HealthReport] = None
    error_code: str = ""
    error_detail: str = ""


@dataclass
class ScanPrompt:
    state: ScanState
    title: str
    body: str
    expects: str = ""
    progress_pct: int = 0
    payload: dict = field(default_factory=dict)
    is_terminal: bool = False
    is_error: bool = False

    def to_dict(self) -> dict:
        return {
            "state": self.state.value, "title": self.title, "body": self.body,
            "expects": self.expects, "progress_pct": self.progress_pct,
            "payload": dict(self.payload),
            "is_terminal": self.is_terminal, "is_error": self.is_error,
        }


class FullScanOrchestrator:
    def __init__(self, *, provider: AbstractScanProvider,
                 data: Optional[ScanData] = None,
                 state: ScanState = ScanState.IDLE,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.provider = provider
        self.data = data or ScanData()
        self.state = state
        self.entitlement = entitlement

    # ── transition helpers ────────────────────────────────────────────
    def _advance(self, to: ScanState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalScanTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("scan transition", extra={
            "from": self.state.value, "to": to.value, "vin": self.data.vin,
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> ScanPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = ScanState.FAILED
        log.warning("scan failure", extra={"code": code, "detail": detail})
        return ScanPrompt(
            state=ScanState.FAILED,
            title="فشل الفحص الشامل",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True, is_error=True,
        )

    # ── dispatch ──────────────────────────────────────────────────────
    async def handle(self, event: ScanEvent | str,
                     payload: Optional[dict] = None) -> ScanPrompt:
        if isinstance(event, str):
            try:
                event = ScanEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalScanTransition as e:
            return self._fail("illegal_transition", str(e))
        except ScanTransportError as e:
            return self._fail("transport_error", str(e))
        except Exception as e:                       # pragma: no cover
            log.exception("scan unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: ScanEvent, payload: dict) -> ScanPrompt:
        if event == ScanEvent.ABORT:
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == ScanEvent.CONNECT:
            return await self._connect(payload)
        if event == ScanEvent.SCAN_ALL:
            return await self._scan_all()
        if event == ScanEvent.FINISH:
            return self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. CONNECT ────────────────────────────────────────────────────
    async def _connect(self, payload: dict) -> ScanPrompt:
        if self.state != ScanState.IDLE:
            raise IllegalScanTransition(
                f"CONNECT only valid in IDLE (now {self.state.value})",
            )
        self.data.chassis_family = (
            payload.get("chassis_family") or "").strip().lower()
        self.data.technician_id = (payload.get("technician_id") or "").strip()

        # Entitlement gate BEFORE any bus chatter.
        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        vin = await self.provider.read_vin()
        self.data.vin = (vin or "").strip().upper()
        self.data.reachable_modules = await self.provider.list_reachable_modules()

        self._advance(ScanState.CONNECTED)
        return ScanPrompt(
            state=self.state,
            title="الاتصال تم ✅",
            body=(
                f"الـ VIN: {self.data.vin or 'غير متاح'}. "
                f"عدد الوحدات اللي ردّت: {len(self.data.reachable_modules)}. "
                f"اضغط SCAN_ALL لبدء فحص كل وحدات السيارة."
            ),
            expects="SCAN_ALL",
            progress_pct=_PROGRESS[self.state],
            payload={
                "vin": self.data.vin,
                "reachable_count": len(self.data.reachable_modules),
                "reachable_modules": list(self.data.reachable_modules),
            },
        )

    # ── 2. SCAN_ALL ───────────────────────────────────────────────────
    async def _scan_all(self) -> ScanPrompt:
        if self.state != ScanState.CONNECTED:
            raise IllegalScanTransition(
                f"SCAN_ALL only valid in CONNECTED (now {self.state.value})",
            )

        # Scan the union of EXPECTED modules (so a non-answering one is a
        # finding) and any extra reachable module the chassis map didn't
        # list (so nothing real is dropped).
        try:
            expected = list(expected_module_codes(self.data.chassis_family))
        except (ValueError, KeyError):
            expected = []
        codes: list[str] = list(dict.fromkeys(
            expected + list(self.data.reachable_modules)))
        reachable = set(self.data.reachable_modules)

        results: list[ModuleScanResult] = []
        for code in codes:
            module = describe_module(code)
            if code not in reachable:
                note = ("الوحدة دي مفروض موجودة بس مردتش — افحص الكابلات/الباور."
                        if module.is_safety_critical
                        else "الوحدة مردتش على الفحص.")
                results.append(ModuleScanResult(
                    module=module, reachable=False, note=note))
                continue
            raw = await self.provider.read_module_dtcs(code)
            decoded = [
                decode_dtc(c, sb,
                           module_is_safety_critical=module.is_safety_critical)
                for (c, sb) in raw
            ]
            results.append(ModuleScanResult(
                module=module, reachable=True, dtcs=decoded))

        report = build_report(
            vin=self.data.vin,
            chassis_family=self.data.chassis_family,
            results=results,
        )
        self.data.report = report
        self._advance(ScanState.REPORT_READY)
        return ScanPrompt(
            state=self.state,
            title=f"التقرير جاهز — {report.overall.value.upper()}",
            body=report.headline_ar(),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload=report.to_dict(),
        )

    # ── 3. FINISH ─────────────────────────────────────────────────────
    def _finish(self) -> ScanPrompt:
        if self.state != ScanState.REPORT_READY:
            raise IllegalScanTransition(
                f"FINISH only valid in REPORT_READY (now {self.state.value})",
            )
        self._advance(ScanState.DONE)

        # One completed scan = one grant use.
        if self.entitlement is not None:
            op_ref = f"scan-{self.data.vin or 'no-vin'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)

        report = self.data.report
        return ScanPrompt(
            state=self.state,
            title="انتهى الفحص 🎉",
            body=(
                "التقرير اتسجّل في الـ Cloud Sync وينفع يتطبع PDF بلوجو "
                "الورشة. تقدر تبعت العميل نسخة من النتيجة."
            ),
            expects="",
            progress_pct=100,
            payload=report.to_dict() if report else {},
            is_terminal=True,
        )

    # ── snapshot / restore ────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "vin": self.data.vin,
                "chassis_family": self.data.chassis_family,
                "technician_id": self.data.technician_id,
                "reachable_modules": list(self.data.reachable_modules),
                "report": self.data.report.to_dict() if self.data.report else None,
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, provider: AbstractScanProvider,
                snapshot: dict[str, Any],
                entitlement: Optional["AbstractEntitlementGuard"] = None,
                ) -> "FullScanOrchestrator":
        s = snapshot["data"]
        data = ScanData(
            vin=s.get("vin", ""),
            chassis_family=s.get("chassis_family", ""),
            technician_id=s.get("technician_id", ""),
            reachable_modules=list(s.get("reachable_modules") or []),
            report=None,   # report is a derived artefact; re-scan to rebuild
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(provider=provider, data=data,
                   state=ScanState(snapshot["state"]),
                   entitlement=entitlement)
