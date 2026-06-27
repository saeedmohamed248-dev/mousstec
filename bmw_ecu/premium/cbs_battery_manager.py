"""CBS Battery Manager — register a new battery + reset CBS counters.

After a battery swap on a BMW (E60+ and every F/G chassis), the
Intelligent Battery Sensor (IBS) + the Power Management module
remember the OLD battery's capacity and charge-cycle history. Until
you register the new one:
  • the alternator under-charges (it thinks the battery is older
    than it is),
  • CBS reports "battery: replace soon" forever,
  • Start/Stop disables itself after 24 hours.

This orchestrator runs the right UDS sequence to:
  1. Validate the technician's claim about the new battery (TYPE + Ah +
     serial). Type mismatch (e.g. lead-acid in an AGM-capable chassis)
     is REFUSED — the alternator profile would over-fry the cells.
  2. Snapshot the OLD registration to the Cloud Sync so a botched
     register can be backed out.
  3. Write the new battery descriptor via UDS-0x2E.
  4. Reset the CBS counters (Mode 0x31 RoutineControl with the
     "battery_swap" routine ID).
  5. Verify by reading back the descriptor + the CBS index.

Pre-conditions
--------------
  • Voltage 12.4–13.5 V (KOEO, battery rested). If the engine is
    running (KOER) the alternator overlays its own voltage.
  • Gear in P.
  • No active charging-system DTCs (would mask a real alternator
    issue we'd be papering over).
"""
from __future__ import annotations

import abc
import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .safety_checks import (
    AbstractSafetyGate,
    GearPosition,
    IgnitionState,
    SafetyReport,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
class BatteryType(str, enum.Enum):
    LEAD_ACID = "lead_acid"
    AGM = "agm"
    EFB = "efb"


@dataclass(frozen=True)
class BatterySpec:
    """Technician-supplied claim about the new battery."""
    type: BatteryType
    capacity_ah: int
    serial: str = ""

    def __post_init__(self) -> None:
        if self.capacity_ah <= 0 or self.capacity_ah > 200:
            raise ValueError(
                f"capacity_ah {self.capacity_ah} out of plausible range (1..200)",
            )


# Chassis-compatible battery types — refuse to register a non-compatible
# pairing (the alternator profile would overcharge / undercharge).
_CHASSIS_TYPES: dict[str, frozenset[BatteryType]] = {
    "E60":  frozenset({BatteryType.LEAD_ACID, BatteryType.AGM}),
    "E90":  frozenset({BatteryType.LEAD_ACID, BatteryType.AGM}),
    "F30":  frozenset({BatteryType.AGM, BatteryType.EFB}),
    "F10":  frozenset({BatteryType.AGM, BatteryType.EFB}),
    "F20":  frozenset({BatteryType.AGM, BatteryType.EFB}),
    "G20":  frozenset({BatteryType.AGM, BatteryType.EFB}),
    "G30":  frozenset({BatteryType.AGM, BatteryType.EFB}),
    "G05":  frozenset({BatteryType.AGM, BatteryType.EFB}),
}


# Charging-system DTCs that would mask a real alternator problem.
CBS_BLOCKING_DTCS = ("A53C", "A53D", "A52A", "P0562", "P0563")


# ─────────────────────────────────────────────────────────────────────
class CbsBatteryState(str, enum.Enum):
    IDLE              = "idle"
    BATTERY_INFO_OK   = "battery_info_ok"
    VEHICLE_STATE_OK  = "vehicle_state_ok"
    OLD_REG_READ      = "old_reg_read"
    NEW_REG_WRITTEN   = "new_reg_written"
    CBS_RESET         = "cbs_reset"
    VERIFIED          = "verified"
    DONE              = "done"
    FAILED            = "failed"


_ALLOWED: dict[CbsBatteryState, set[CbsBatteryState]] = {
    CbsBatteryState.IDLE:             {CbsBatteryState.BATTERY_INFO_OK, CbsBatteryState.FAILED},
    CbsBatteryState.BATTERY_INFO_OK:  {CbsBatteryState.VEHICLE_STATE_OK, CbsBatteryState.FAILED},
    CbsBatteryState.VEHICLE_STATE_OK: {CbsBatteryState.OLD_REG_READ, CbsBatteryState.FAILED},
    CbsBatteryState.OLD_REG_READ:     {CbsBatteryState.NEW_REG_WRITTEN, CbsBatteryState.FAILED},
    CbsBatteryState.NEW_REG_WRITTEN:  {CbsBatteryState.CBS_RESET, CbsBatteryState.FAILED},
    CbsBatteryState.CBS_RESET:        {CbsBatteryState.VERIFIED, CbsBatteryState.FAILED},
    CbsBatteryState.VERIFIED:         {CbsBatteryState.DONE, CbsBatteryState.FAILED},
    CbsBatteryState.DONE:             set(),
    CbsBatteryState.FAILED:           set(),
}


class IllegalCbsTransition(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────
class AbstractCbsServiceProvider(abc.ABC):
    @abc.abstractmethod
    async def read_current_registration(self) -> dict:
        """Return the IBS-stored battery descriptor as a dict
        {type, capacity_ah, serial, install_timestamp_unix}."""

    @abc.abstractmethod
    async def write_registration(self, *, spec: BatterySpec) -> None: ...

    @abc.abstractmethod
    async def reset_cbs_counters(self) -> None: ...

    @abc.abstractmethod
    async def read_cbs_index(self) -> int:
        """Return the CBS battery index 0..100 (lower = closer to swap
        prompt). A fresh register typically lands ≥ 95."""


@dataclass
class MockCbsServiceProvider(AbstractCbsServiceProvider):
    initial_registration: dict = field(default_factory=lambda: {
        "type": "agm", "capacity_ah": 80, "serial": "OLDBAT-001",
        "install_timestamp_unix": 1_600_000_000,
    })
    current: dict = field(default_factory=dict)
    cbs_index: int = 14    # "replace soon" range
    reads: int = 0
    writes: list[BatterySpec] = field(default_factory=list)
    reset_calls: int = 0
    refuse_write: bool = False

    def __post_init__(self) -> None:
        if not self.current:
            self.current = dict(self.initial_registration)

    async def read_current_registration(self) -> dict:
        self.reads += 1
        return dict(self.current)

    async def write_registration(self, *, spec: BatterySpec) -> None:
        if self.refuse_write:
            raise IOError("Mock: simulated write_registration UDS NACK")
        self.writes.append(spec)
        self.current = {
            "type": spec.type.value,
            "capacity_ah": spec.capacity_ah,
            "serial": spec.serial,
            "install_timestamp_unix": 1_700_000_000,
        }

    async def reset_cbs_counters(self) -> None:
        self.reset_calls += 1
        self.cbs_index = 100

    async def read_cbs_index(self) -> int:
        return self.cbs_index


# ─────────────────────────────────────────────────────────────────────
@dataclass
class CbsBatteryData:
    vin: str = ""
    technician_id: str = ""
    chassis: str = ""
    new_spec: Optional[dict] = None    # serialised BatterySpec
    old_registration: dict = field(default_factory=dict)
    new_registration: dict = field(default_factory=dict)
    cbs_index_after: Optional[int] = None
    safety_voltage_v: float = 0.0
    notes: list[str] = field(default_factory=list)
    error_code: str = ""
    error_detail: str = ""


@dataclass
class CbsBatteryPrompt:
    state: CbsBatteryState
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


class CbsBatteryEvent(str, enum.Enum):
    ENTER_BATTERY_INFO  = "enter_battery_info"
    CHECK_VEHICLE       = "check_vehicle"
    READ_OLD            = "read_old"
    WRITE_NEW           = "write_new"
    RESET_COUNTERS      = "reset_counters"
    VERIFY              = "verify"
    FINISH              = "finish"
    ABORT               = "abort"


_PROGRESS = {
    CbsBatteryState.IDLE: 0, CbsBatteryState.BATTERY_INFO_OK: 15,
    CbsBatteryState.VEHICLE_STATE_OK: 30, CbsBatteryState.OLD_REG_READ: 45,
    CbsBatteryState.NEW_REG_WRITTEN: 70, CbsBatteryState.CBS_RESET: 85,
    CbsBatteryState.VERIFIED: 95, CbsBatteryState.DONE: 100,
    CbsBatteryState.FAILED: 0,
}


# ─────────────────────────────────────────────────────────────────────
class CbsBatteryOrchestrator:
    def __init__(self, *, safety: AbstractSafetyGate,
                 provider: AbstractCbsServiceProvider,
                 data: Optional[CbsBatteryData] = None,
                 state: CbsBatteryState = CbsBatteryState.IDLE) -> None:
        self.safety = safety
        self.provider = provider
        self.data = data or CbsBatteryData()
        self.state = state

    def _advance(self, to: CbsBatteryState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalCbsTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("cbs transition", extra={
            "from": self.state.value, "to": to.value, "vin": self.data.vin,
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> CbsBatteryPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = CbsBatteryState.FAILED
        log.warning("cbs failure", extra={"code": code})
        return CbsBatteryPrompt(
            state=CbsBatteryState.FAILED,
            title="فشل تسجيل البطارية",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True, is_error=True,
        )

    async def handle(self, event: CbsBatteryEvent | str,
                     payload: Optional[dict] = None) -> CbsBatteryPrompt:
        if isinstance(event, str):
            try:
                event = CbsBatteryEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalCbsTransition as e:
            return self._fail("illegal_transition", str(e))
        except ValueError as e:
            return self._fail("validation_error", str(e))
        except Exception as e:                          # pragma: no cover
            log.exception("cbs unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: CbsBatteryEvent, payload: dict
                        ) -> CbsBatteryPrompt:
        if event == CbsBatteryEvent.ABORT:
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == CbsBatteryEvent.ENTER_BATTERY_INFO:
            return self._enter_battery_info(payload)
        if event == CbsBatteryEvent.CHECK_VEHICLE:
            return await self._check_vehicle()
        if event == CbsBatteryEvent.READ_OLD:
            return await self._read_old()
        if event == CbsBatteryEvent.WRITE_NEW:
            return await self._write_new()
        if event == CbsBatteryEvent.RESET_COUNTERS:
            return await self._reset_counters()
        if event == CbsBatteryEvent.VERIFY:
            return await self._verify()
        if event == CbsBatteryEvent.FINISH:
            return self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. ENTER_BATTERY_INFO ─────────────────────────────────
    def _enter_battery_info(self, payload: dict) -> CbsBatteryPrompt:
        if self.state != CbsBatteryState.IDLE:
            raise IllegalCbsTransition(
                f"ENTER_BATTERY_INFO only valid in IDLE (now {self.state.value})",
            )
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()
        chassis = (payload.get("chassis") or "").strip().upper()
        if not chassis:
            return self._fail("missing_chassis",
                              "chassis is required (e.g. E90, F30, G20)")
        self.data.chassis = chassis

        try:
            spec = BatterySpec(
                type=BatteryType((payload.get("type") or "").strip().lower()),
                capacity_ah=int(payload.get("capacity_ah") or 0),
                serial=(payload.get("serial") or "").strip().upper(),
            )
        except ValueError as e:
            return self._fail("invalid_battery_spec", str(e))

        # Chassis compatibility — refuse if the type isn't supported.
        compat = _CHASSIS_TYPES.get(chassis)
        if compat is not None and spec.type not in compat:
            allowed = "/".join(t.value for t in compat)
            return self._fail(
                "incompatible_battery_type",
                f"البطارية type={spec.type.value} مش متوافقة مع chassis "
                f"{chassis}. النوع المسموح: [{allowed}]. تركيب نوع مش "
                f"صحيح بيـ overcharge أو undercharge بمرور الوقت.",
            )

        self.data.new_spec = {
            "type": spec.type.value,
            "capacity_ah": spec.capacity_ah,
            "serial": spec.serial,
        }
        self._advance(CbsBatteryState.BATTERY_INFO_OK)
        return CbsBatteryPrompt(
            state=self.state,
            title="بيانات البطارية مظبوطة ✅",
            body=(
                f"{chassis} — type={spec.type.value}، {spec.capacity_ah} Ah، "
                f"serial={spec.serial or '—'}. اضغط CHECK_VEHICLE لقراءة "
                f"حالة العربية."
            ),
            expects="CHECK_VEHICLE",
            progress_pct=_PROGRESS[self.state],
            payload=dict(self.data.new_spec),
        )

    # ── 2. CHECK_VEHICLE ──────────────────────────────────────
    async def _check_vehicle(self) -> CbsBatteryPrompt:
        if self.state != CbsBatteryState.BATTERY_INFO_OK:
            raise IllegalCbsTransition(
                f"CHECK_VEHICLE only valid in BATTERY_INFO_OK (now {self.state.value})",
            )
        report: SafetyReport = await self.safety.probe(require={
            "voltage_min_v": 12.4, "voltage_max_v": 13.5,
            "gear_in": [GearPosition.P],
            "ignition_in": [IgnitionState.KOEO],
            "forbidden_dtcs": CBS_BLOCKING_DTCS,
        })
        self.data.safety_voltage_v = report.voltage_v
        if not report.ok:
            return self._fail(
                "vehicle_state_failed",
                "حالة العربية مش مظبوطة: " + " | ".join(report.refusal_reasons),
            )
        self._advance(CbsBatteryState.VEHICLE_STATE_OK)
        return CbsBatteryPrompt(
            state=self.state,
            title="العربية جاهزة ✅",
            body=(
                f"الجهد: {report.voltage_v:.2f} V، gear=P، ignition=KOEO، "
                f"مفيش DTC في الـ charging system. اضغط READ_OLD."
            ),
            expects="READ_OLD",
            progress_pct=_PROGRESS[self.state],
            payload={"voltage_v": report.voltage_v},
        )

    # ── 3. READ_OLD ───────────────────────────────────────────
    async def _read_old(self) -> CbsBatteryPrompt:
        if self.state != CbsBatteryState.VEHICLE_STATE_OK:
            raise IllegalCbsTransition(
                f"READ_OLD only valid in VEHICLE_STATE_OK (now {self.state.value})",
            )
        reg = await self.provider.read_current_registration()
        self.data.old_registration = dict(reg)
        self._advance(CbsBatteryState.OLD_REG_READ)
        return CbsBatteryPrompt(
            state=self.state,
            title="قراءة التسجيل الحالي",
            body=(
                f"البطارية المسجّلة حالياً: "
                f"{reg.get('type', '?')} {reg.get('capacity_ah', '?')}Ah، "
                f"serial={reg.get('serial', '—')}. "
                f"اضغط WRITE_NEW لكتابة البيانات الجديدة."
            ),
            expects="WRITE_NEW",
            progress_pct=_PROGRESS[self.state],
            payload=dict(self.data.old_registration),
        )

    # ── 4. WRITE_NEW ──────────────────────────────────────────
    async def _write_new(self) -> CbsBatteryPrompt:
        if self.state != CbsBatteryState.OLD_REG_READ:
            raise IllegalCbsTransition(
                f"WRITE_NEW only valid in OLD_REG_READ (now {self.state.value})",
            )
        if self.data.new_spec is None:
            return self._fail("missing_spec",
                              "internal: new_spec missing — re-enter from IDLE")
        spec = BatterySpec(
            type=BatteryType(self.data.new_spec["type"]),
            capacity_ah=int(self.data.new_spec["capacity_ah"]),
            serial=self.data.new_spec.get("serial", ""),
        )
        await self.provider.write_registration(spec=spec)
        self._advance(CbsBatteryState.NEW_REG_WRITTEN)
        return CbsBatteryPrompt(
            state=self.state,
            title="تم كتابة التسجيل الجديد ✅",
            body=(
                "الـ IBS قبل البيانات الجديدة. اضغط RESET_COUNTERS عشان "
                "نمسح الـ CBS history."
            ),
            expects="RESET_COUNTERS",
            progress_pct=_PROGRESS[self.state],
            payload=dict(self.data.new_spec),
        )

    # ── 5. RESET_COUNTERS ─────────────────────────────────────
    async def _reset_counters(self) -> CbsBatteryPrompt:
        if self.state != CbsBatteryState.NEW_REG_WRITTEN:
            raise IllegalCbsTransition(
                f"RESET_COUNTERS only valid in NEW_REG_WRITTEN (now {self.state.value})",
            )
        await self.provider.reset_cbs_counters()
        self._advance(CbsBatteryState.CBS_RESET)
        return CbsBatteryPrompt(
            state=self.state,
            title="تم reset لـ CBS counters",
            body="اضغط VERIFY عشان نقرأ التسجيل والـ index ونتأكد.",
            expects="VERIFY",
            progress_pct=_PROGRESS[self.state],
        )

    # ── 6. VERIFY ─────────────────────────────────────────────
    async def _verify(self) -> CbsBatteryPrompt:
        if self.state != CbsBatteryState.CBS_RESET:
            raise IllegalCbsTransition(
                f"VERIFY only valid in CBS_RESET (now {self.state.value})",
            )
        new_reg = await self.provider.read_current_registration()
        self.data.new_registration = dict(new_reg)
        cbs_idx = await self.provider.read_cbs_index()
        self.data.cbs_index_after = cbs_idx

        # Confirm the readback matches what we wrote.
        expected = self.data.new_spec or {}
        for key in ("type", "capacity_ah", "serial"):
            if str(new_reg.get(key)) != str(expected.get(key)):
                return self._fail(
                    "verify_mismatch",
                    f"الـ {key}: المكتوب={expected.get(key)}، "
                    f"المقروء={new_reg.get(key)}",
                )
        if cbs_idx < 90:
            return self._fail(
                "cbs_index_low_after_reset",
                f"الـ CBS index بعد الـ reset = {cbs_idx} (المتوقع ≥ 90). "
                f"الـ reset routine ما اشتغلتش بالكامل.",
            )

        self._advance(CbsBatteryState.VERIFIED)
        return CbsBatteryPrompt(
            state=self.state,
            title="التحقق ناجح ✅",
            body=(
                f"التسجيل الجديد متخزن، الـ CBS index = {cbs_idx}/100. "
                f"اضغط FINISH."
            ),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload={"cbs_index": cbs_idx},
        )

    # ── 7. FINISH ─────────────────────────────────────────────
    def _finish(self) -> CbsBatteryPrompt:
        if self.state != CbsBatteryState.VERIFIED:
            raise IllegalCbsTransition(
                f"FINISH only valid in VERIFIED (now {self.state.value})",
            )
        self._advance(CbsBatteryState.DONE)
        return CbsBatteryPrompt(
            state=self.state,
            title="انتهت العملية 🎉",
            body=(
                "البطارية مسجّلة، الـ Start/Stop هيرجع يشتغل بعد دورة "
                "ignition. لو الـ MIL لسه شغّال، شغّل ignition cycle "
                "واحد ثم اقفل قراءة كاملة."
            ),
            expects="",
            progress_pct=100,
            payload={
                "vin": self.data.vin,
                "chassis": self.data.chassis,
                "new_spec": self.data.new_spec,
                "cbs_index": self.data.cbs_index_after,
            },
            is_terminal=True,
        )

    # ── Snapshot / restore ─────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "chassis": self.data.chassis,
                "new_spec": self.data.new_spec,
                "old_registration": dict(self.data.old_registration),
                "new_registration": dict(self.data.new_registration),
                "cbs_index_after": self.data.cbs_index_after,
                "safety_voltage_v": self.data.safety_voltage_v,
                "notes": list(self.data.notes),
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, safety: AbstractSafetyGate,
                provider: AbstractCbsServiceProvider,
                snapshot: dict[str, Any]) -> "CbsBatteryOrchestrator":
        s = snapshot["data"]
        data = CbsBatteryData(
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            chassis=s.get("chassis", ""),
            new_spec=s.get("new_spec"),
            old_registration=dict(s.get("old_registration") or {}),
            new_registration=dict(s.get("new_registration") or {}),
            cbs_index_after=s.get("cbs_index_after"),
            safety_voltage_v=float(s.get("safety_voltage_v") or 0.0),
            notes=list(s.get("notes") or []),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(safety=safety, provider=provider,
                   data=data, state=CbsBatteryState(snapshot["state"]))
