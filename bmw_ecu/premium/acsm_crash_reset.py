"""ACSM Crash Reset — Airbag / SRS module.

⚠️  THIS IS A SAFETY-CRITICAL MODULE.

After a collision the ACSM (Airbag Control Sensor Module) latches a
"crash record" — a binary tuple of (timestamp, severity, deployed
slots). Even a minor parking-lot bump can set the latch. The airbag
indicator stays lit and the system goes into a refusing-to-deploy
posture until the record is cleared. Dealer fix is a 1,200 EUR ACSM
swap; this module clears the record over OBD/Bench instead.

The huge caveat: clearing the record on a module whose squibs are
DAMAGED puts the customer in mortal danger on the next collision. The
orchestrator therefore has TWO terminal states:

  • DONE             — clean clear, system re-armed.
  • BLOCKED_FOR_SAFETY — physical inspection required before any
                        software action. This is NOT a failure — it's
                        the orchestrator refusing to enable a
                        dangerous operation.

Hard gates BEFORE the routine can run
-------------------------------------
  1. NO airbag module reports state DEPLOYED, DISCONNECTED, or SHORTED.
     (Even one bad squib → BLOCKED_FOR_SAFETY.)
  2. Recent DTC list contains no "active deployment" code
     (B0001-B0005, B0010, B0020 BMW family).
  3. Voltage stable 11.8–14.6 V.
  4. Ignition KOEO (engine MUST be off — engine vibration jams
     SecurityAccess on some ACSM revisions).

These checks run BOTH in ASSESS_DAMAGE (entry) and again under
read-modify-write right before the actual clear so a wire that came
loose mid-session can't slip through.
"""
from __future__ import annotations

import abc
import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from .safety_checks import (
    AbstractSafetyGate,
    AirbagModuleState,
    IgnitionState,
    SafetyReport,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
class AcsmCrashState(str, enum.Enum):
    IDLE                = "idle"
    DAMAGE_ASSESSED     = "damage_assessed"
    CRASH_RECORD_READ   = "crash_record_read"
    BACKUP_SAVED        = "backup_saved"
    CLEAR_REQUESTED     = "clear_requested"
    VERIFIED            = "verified"
    DONE                = "done"
    FAILED              = "failed"
    # Terminal but NOT a failure — a refusal to perform a dangerous op.
    BLOCKED_FOR_SAFETY  = "blocked_for_safety"


_ALLOWED: dict[AcsmCrashState, set[AcsmCrashState]] = {
    AcsmCrashState.IDLE: {
        AcsmCrashState.DAMAGE_ASSESSED,
        AcsmCrashState.FAILED,
        AcsmCrashState.BLOCKED_FOR_SAFETY,
    },
    AcsmCrashState.DAMAGE_ASSESSED: {
        AcsmCrashState.CRASH_RECORD_READ, AcsmCrashState.FAILED,
    },
    AcsmCrashState.CRASH_RECORD_READ: {
        AcsmCrashState.BACKUP_SAVED, AcsmCrashState.FAILED,
    },
    AcsmCrashState.BACKUP_SAVED: {
        AcsmCrashState.CLEAR_REQUESTED,
        AcsmCrashState.FAILED,
        AcsmCrashState.BLOCKED_FOR_SAFETY,   # second-gate trip
    },
    AcsmCrashState.CLEAR_REQUESTED: {
        AcsmCrashState.VERIFIED, AcsmCrashState.FAILED,
    },
    AcsmCrashState.VERIFIED: {AcsmCrashState.DONE, AcsmCrashState.FAILED},
    AcsmCrashState.DONE: set(),
    AcsmCrashState.FAILED: set(),
    AcsmCrashState.BLOCKED_FOR_SAFETY: set(),
}


class IllegalAcsmTransition(Exception):
    pass


class AcsmSafetyBlocked(Exception):
    """Raised when a hard safety gate refuses the operation. Caller
    converts this into a BLOCKED_FOR_SAFETY prompt — NOT a FAILED one."""


# Known BMW SRS deployment / fault DTCs that must NOT be present.
ACSM_BLOCKING_DTCS = (
    "B0001", "B0002", "B0003", "B0004", "B0005",     # driver / passenger bags
    "B0010", "B0011", "B0012", "B0013",              # side bags
    "B0020", "B0021",                                 # curtain bags
    "B0028", "B0029",                                 # pretensioners
    "9CA0", "9CA1",                                   # BMW manuf. variants
)


# ─────────────────────────────────────────────────────────────────────
class AbstractAcsmServiceProvider(abc.ABC):
    @abc.abstractmethod
    async def enter_extended_session(self) -> None: ...

    @abc.abstractmethod
    async def unlock_security(self, *, vin: str) -> None: ...

    @abc.abstractmethod
    async def read_crash_record(self) -> dict:
        """Return the (opaque) crash record as a dict. Empty dict = no
        active record. Real shape: {"timestamp": int, "severity": int,
        "deployed_slots": tuple, "raw_hex": str}."""

    @abc.abstractmethod
    async def save_backup_to_cloud(self, *, vin: str, record: dict,
                                   technician_id: str) -> str:
        """Persist the crash record to the cloud-sync table so the
        record is recoverable if accounting needs to disprove a
        clear-without-inspection accusation. Returns a backup reference."""

    @abc.abstractmethod
    async def request_clear(self) -> None: ...

    @abc.abstractmethod
    async def read_post_clear_record(self) -> dict:
        """Read after the clear — empty dict means cleared."""


@dataclass
class MockAcsmServiceProvider(AbstractAcsmServiceProvider):
    initial_record: dict = field(default_factory=dict)
    refuse_clear: bool = False
    cleared: bool = False
    extended_calls: int = 0
    security_calls: list[str] = field(default_factory=list)
    backup_calls: list[dict] = field(default_factory=list)
    clear_calls: int = 0

    async def enter_extended_session(self) -> None:
        self.extended_calls += 1

    async def unlock_security(self, *, vin: str) -> None:
        self.security_calls.append(vin)

    async def read_crash_record(self) -> dict:
        if self.cleared:
            return {}
        return dict(self.initial_record)

    async def save_backup_to_cloud(self, *, vin: str, record: dict,
                                   technician_id: str) -> str:
        self.backup_calls.append({
            "vin": vin, "record": dict(record),
            "technician_id": technician_id,
        })
        return f"BAK-{vin}-{len(self.backup_calls)}"

    async def request_clear(self) -> None:
        self.clear_calls += 1
        if self.refuse_clear:
            raise AcsmSafetyBlocked(
                "MockAcsmServiceProvider: ACSM rejected the clear routine. "
                "Real hardware would do this if a squib resistance check fails.",
            )
        self.cleared = True

    async def read_post_clear_record(self) -> dict:
        return {} if self.cleared else dict(self.initial_record)


# ─────────────────────────────────────────────────────────────────────
@dataclass
class AcsmCrashData:
    vin: str = ""
    technician_id: str = ""
    safety_voltage_v: float = 0.0
    crash_record: dict = field(default_factory=dict)
    backup_ref: str = ""
    blocked_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error_code: str = ""
    error_detail: str = ""


@dataclass
class AcsmCrashPrompt:
    state: AcsmCrashState
    title: str
    body: str
    expects: str = ""
    progress_pct: int = 0
    payload: dict = field(default_factory=dict)
    is_terminal: bool = False
    is_error: bool = False
    is_safety_block: bool = False

    def to_dict(self) -> dict:
        return {
            "state": self.state.value, "title": self.title, "body": self.body,
            "expects": self.expects, "progress_pct": self.progress_pct,
            "payload": dict(self.payload),
            "is_terminal": self.is_terminal, "is_error": self.is_error,
            "is_safety_block": self.is_safety_block,
        }


class AcsmCrashEvent(str, enum.Enum):
    ASSESS_DAMAGE   = "assess_damage"
    READ_RECORD     = "read_record"
    BACKUP          = "backup"
    REQUEST_CLEAR   = "request_clear"
    VERIFY          = "verify"
    FINISH          = "finish"
    ABORT           = "abort"


_PROGRESS = {
    AcsmCrashState.IDLE: 0,
    AcsmCrashState.DAMAGE_ASSESSED: 25,
    AcsmCrashState.CRASH_RECORD_READ: 40,
    AcsmCrashState.BACKUP_SAVED: 55,
    AcsmCrashState.CLEAR_REQUESTED: 80,
    AcsmCrashState.VERIFIED: 95,
    AcsmCrashState.DONE: 100,
    AcsmCrashState.FAILED: 0,
    AcsmCrashState.BLOCKED_FOR_SAFETY: 0,
}


# ─────────────────────────────────────────────────────────────────────
class AcsmCrashOrchestrator:
    def __init__(self, *, safety: AbstractSafetyGate,
                 provider: AbstractAcsmServiceProvider,
                 data: Optional[AcsmCrashData] = None,
                 state: AcsmCrashState = AcsmCrashState.IDLE,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.safety = safety
        self.provider = provider
        self.data = data or AcsmCrashData()
        self.state = state
        # Entitlement gate — check() at ASSESS_DAMAGE, consume() on FINISH.
        # NOTE: consume() runs ONLY when the orchestrator hits DONE.
        # BLOCKED_FOR_SAFETY terminal states do NOT consume the grant —
        # the technician didn't actually use the service, just got
        # refused by safety logic.
        self.entitlement = entitlement

    def _advance(self, to: AcsmCrashState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalAcsmTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("acsm transition", extra={
            "from": self.state.value, "to": to.value, "vin": self.data.vin,
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> AcsmCrashPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = AcsmCrashState.FAILED
        log.warning("acsm failure", extra={"code": code})
        return AcsmCrashPrompt(
            state=AcsmCrashState.FAILED,
            title="فشل مسح بيانات الـ Airbag",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True, is_error=True,
        )

    def _block_for_safety(self, reasons: list[str]) -> AcsmCrashPrompt:
        """Distinct from `_fail` — this is the orchestrator REFUSING to
        run a dangerous operation. The technician must do a physical
        inspection (or replace the ACSM) before retrying.
        """
        self.data.blocked_reasons = list(reasons)
        self.state = AcsmCrashState.BLOCKED_FOR_SAFETY
        log.warning("acsm blocked for safety", extra={
            "vin": self.data.vin, "reasons": reasons,
        })
        return AcsmCrashPrompt(
            state=AcsmCrashState.BLOCKED_FOR_SAFETY,
            title="⚠️ السلامة أولاً — العملية متوقفة",
            body=(
                "الـ orchestrator رفض الـ clear لأن واحد أو أكتر من "
                "شروط السلامة مش متوفر:\n\n"
                + "\n".join(f"  • {r}" for r in reasons) +
                "\n\nلازم فحص فيزيائي للـ harness والـ squibs والـ "
                "modules قبل أي تكرار. لو الـ ACSM مش سليم، لازم "
                "تستبدل بـ module جديد — مينفعش تعدّى من software."
            ),
            expects="ABORT",
            progress_pct=0,
            payload={"blocked_reasons": list(reasons)},
            is_terminal=True, is_safety_block=True,
        )

    async def handle(self, event: AcsmCrashEvent | str,
                     payload: Optional[dict] = None) -> AcsmCrashPrompt:
        if isinstance(event, str):
            try:
                event = AcsmCrashEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalAcsmTransition as e:
            return self._fail("illegal_transition", str(e))
        except AcsmSafetyBlocked as e:
            # Treated as a safety refusal, NOT a failure.
            return self._block_for_safety([str(e)])
        except Exception as e:                          # pragma: no cover
            log.exception("acsm unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: AcsmCrashEvent, payload: dict
                        ) -> AcsmCrashPrompt:
        if event == AcsmCrashEvent.ABORT:
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == AcsmCrashEvent.ASSESS_DAMAGE:
            return await self._assess_damage(payload)
        if event == AcsmCrashEvent.READ_RECORD:
            return await self._read_record()
        if event == AcsmCrashEvent.BACKUP:
            return await self._backup()
        if event == AcsmCrashEvent.REQUEST_CLEAR:
            return await self._request_clear()
        if event == AcsmCrashEvent.VERIFY:
            return await self._verify()
        if event == AcsmCrashEvent.FINISH:
            return self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. ASSESS_DAMAGE — the first safety gate ──────────────
    async def _assess_damage(self, payload: dict) -> AcsmCrashPrompt:
        if self.state != AcsmCrashState.IDLE:
            raise IllegalAcsmTransition(
                f"ASSESS_DAMAGE only valid in IDLE (now {self.state.value})",
            )
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()

        # Entitlement gate — block unentitled sessions BEFORE we
        # ever probe the SRS bus. ACSM-bus access is itself sensitive,
        # so refusing early is preferable to refusing mid-session.
        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        report: SafetyReport = await self.safety.probe(require={
            "voltage_min_v": 11.8, "voltage_max_v": 14.6,
            "ignition_in": [IgnitionState.KOEO],
            "forbidden_dtcs": ACSM_BLOCKING_DTCS,
            "forbid_deployed_bag": True,
        })
        self.data.safety_voltage_v = report.voltage_v
        if not report.ok:
            return self._block_for_safety(list(report.refusal_reasons))

        self._advance(AcsmCrashState.DAMAGE_ASSESSED)
        modules_ok = [mid for (mid, st) in report.airbag_modules
                      if st == AirbagModuleState.OK]
        return AcsmCrashPrompt(
            state=self.state,
            title="فحص السلامة الأولي ✅",
            body=(
                f"الجهد: {report.voltage_v:.2f} V، الـ ignition: KOEO. "
                f"عدد الموديولات المعروفة سليمة: {len(modules_ok)}. "
                f"اضغط READ_RECORD لقراءة سجل الـ crash."
            ),
            expects="READ_RECORD",
            progress_pct=_PROGRESS[self.state],
            payload={
                "voltage_v": report.voltage_v,
                "ok_module_count": len(modules_ok),
            },
        )

    # ── 2. READ_RECORD ────────────────────────────────────────
    async def _read_record(self) -> AcsmCrashPrompt:
        if self.state != AcsmCrashState.DAMAGE_ASSESSED:
            raise IllegalAcsmTransition(
                f"READ_RECORD only valid in DAMAGE_ASSESSED (now {self.state.value})",
            )
        await self.provider.enter_extended_session()
        await self.provider.unlock_security(vin=self.data.vin)
        record = await self.provider.read_crash_record()
        self.data.crash_record = dict(record)
        self._advance(AcsmCrashState.CRASH_RECORD_READ)
        if not record:
            body = (
                "الـ ACSM مفيهوش crash record — مفيش حاجة نمسحها. "
                "ده يحصل لو حد سبق ومسح قبل كده. اضغط BACKUP عشان "
                "نسجل الحالة ونـ proceed."
            )
        else:
            body = (
                f"الـ ACSM فيه crash record. الـ severity: "
                f"{record.get('severity', '?')}. اضغط BACKUP عشان "
                f"نحفظ نسخة في الـ Cloud Sync قبل المسح."
            )
        return AcsmCrashPrompt(
            state=self.state,
            title="قراءة سجل الـ crash",
            body=body,
            expects="BACKUP",
            progress_pct=_PROGRESS[self.state],
            payload={"has_record": bool(record),
                     "severity": record.get("severity") if record else None},
        )

    # ── 3. BACKUP ─────────────────────────────────────────────
    async def _backup(self) -> AcsmCrashPrompt:
        if self.state != AcsmCrashState.CRASH_RECORD_READ:
            raise IllegalAcsmTransition(
                f"BACKUP only valid in CRASH_RECORD_READ (now {self.state.value})",
            )
        backup_ref = await self.provider.save_backup_to_cloud(
            vin=self.data.vin,
            record=self.data.crash_record,
            technician_id=self.data.technician_id,
        )
        self.data.backup_ref = backup_ref
        self._advance(AcsmCrashState.BACKUP_SAVED)
        return AcsmCrashPrompt(
            state=self.state,
            title="تم حفظ نسخة احتياطية",
            body=(
                f"الـ record اتسجل على السيرفر برقم {backup_ref}. "
                "اضغط REQUEST_CLEAR لتنفيذ الـ routine."
            ),
            expects="REQUEST_CLEAR",
            progress_pct=_PROGRESS[self.state],
            payload={"backup_ref": backup_ref},
        )

    # ── 4. REQUEST_CLEAR — the second safety gate ─────────────
    async def _request_clear(self) -> AcsmCrashPrompt:
        if self.state != AcsmCrashState.BACKUP_SAVED:
            raise IllegalAcsmTransition(
                f"REQUEST_CLEAR only valid in BACKUP_SAVED (now {self.state.value})",
            )
        # RE-PROBE before the irreversible step. A wire that came loose
        # between ASSESS_DAMAGE and now would otherwise slip through.
        report: SafetyReport = await self.safety.probe(require={
            "voltage_min_v": 11.8, "voltage_max_v": 14.6,
            "ignition_in": [IgnitionState.KOEO],
            "forbidden_dtcs": ACSM_BLOCKING_DTCS,
            "forbid_deployed_bag": True,
        })
        if not report.ok:
            return self._block_for_safety(list(report.refusal_reasons))

        await self.provider.request_clear()
        self._advance(AcsmCrashState.CLEAR_REQUESTED)
        return AcsmCrashPrompt(
            state=self.state,
            title="تم إرسال الـ clear routine",
            body=(
                "الـ ACSM قبل الـ routine. اضغط VERIFY عشان نقرأ تاني "
                "ونتأكد إن الـ record اتمسح."
            ),
            expects="VERIFY",
            progress_pct=_PROGRESS[self.state],
            payload={},
        )

    # ── 5. VERIFY ─────────────────────────────────────────────
    async def _verify(self) -> AcsmCrashPrompt:
        if self.state != AcsmCrashState.CLEAR_REQUESTED:
            raise IllegalAcsmTransition(
                f"VERIFY only valid in CLEAR_REQUESTED (now {self.state.value})",
            )
        post = await self.provider.read_post_clear_record()
        if post:
            return self._fail(
                "verify_record_remains",
                "بعد الـ clear الـ ACSM لسه فيه record — الـ routine ما اشتغلتش.",
            )
        self._advance(AcsmCrashState.VERIFIED)
        return AcsmCrashPrompt(
            state=self.state,
            title="تم المسح والتحقق ✅",
            body=(
                "الـ ACSM رد بـ record فارغ. الـ MIL لازم يطفي بعد "
                "ignition cycle. اضغط FINISH."
            ),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
        )

    # ── 6. FINISH ─────────────────────────────────────────────
    def _finish(self) -> AcsmCrashPrompt:
        if self.state != AcsmCrashState.VERIFIED:
            raise IllegalAcsmTransition(
                f"FINISH only valid in VERIFIED (now {self.state.value})",
            )
        self._advance(AcsmCrashState.DONE)

        # Entitlement consume — ACSM clear + verify both succeeded.
        # The grant counts the use NOW (not on BLOCKED_FOR_SAFETY,
        # which represents a refusal to perform the service).
        if self.entitlement is not None:
            op_ref = f"acsm-{self.data.vin or 'no-vin'}-{self.data.backup_ref or 'no-bak'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)

        return AcsmCrashPrompt(
            state=self.state,
            title="انتهت العملية 🎉",
            body=(
                "اقفل الـ ignition ثم شغّل تاني. الـ Airbag indicator "
                "هيتشغّل لثوانٍ ثم يطفي — دي السلوك السليم. الـ session "
                "كاملة محفوظة (record + backup_ref) في الـ Cloud Sync."
            ),
            expects="",
            progress_pct=100,
            payload={
                "vin": self.data.vin,
                "backup_ref": self.data.backup_ref,
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
                "safety_voltage_v": self.data.safety_voltage_v,
                "crash_record": dict(self.data.crash_record),
                "backup_ref": self.data.backup_ref,
                "blocked_reasons": list(self.data.blocked_reasons),
                "notes": list(self.data.notes),
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, safety: AbstractSafetyGate,
                provider: AbstractAcsmServiceProvider,
                snapshot: dict[str, Any]) -> "AcsmCrashOrchestrator":
        s = snapshot["data"]
        data = AcsmCrashData(
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            safety_voltage_v=float(s.get("safety_voltage_v") or 0.0),
            crash_record=dict(s.get("crash_record") or {}),
            backup_ref=s.get("backup_ref", ""),
            blocked_reasons=list(s.get("blocked_reasons") or []),
            notes=list(s.get("notes") or []),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(safety=safety, provider=provider,
                   data=data, state=AcsmCrashState(snapshot["state"]))
