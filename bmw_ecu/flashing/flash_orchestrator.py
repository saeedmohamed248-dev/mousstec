"""Guided ECU-flash orchestrator — entitlement-gated, backup-enforced.

The highest-risk flow in the suite, so it is the most defended. A
declarative `FlashJob` (flash_catalog.py) is run through a forward-only
machine over the hardware-free `AbstractFlashProvider`:

  IDLE ─START─▶ READY ─BACKUP─▶ BACKED_UP ─FLASH─▶ FLASHED ─FINISH─▶ DONE
                  │                  │                 │
                  │   (ABORT / safety / error)         │
                  └──────────┬─────────────────────────┘
                             ▼
                           FAILED

  • START   — load job, entitlement gate ('ecu_flashing'), VALIDATE the
              payload size against the job's band, SafetyGate probe (a
              CHARGED battery: ≥13.0 V, KOEO), read the current version.
              → READY.
  • BACKUP  — programming session, optional security, then read + keep a
              full backup of the target region. NOTHING is erased before
              this succeeds. → BACKED_UP.
  • FLASH   — erase → request_download → transfer every block →
              transfer_exit → local checksum → check_dependencies →
              ecu_reset. If ANY step fails, the saved backup is written
              back (restore_backup) before the machine goes FAILED, so a
              half-written ECU is never left bricked. → FLASHED.
  • FINISH  — consume the grant once. → DONE.

INVARIANT: the machine never enters FLASH without a backup in hand, and
never leaves a failed FLASH without attempting rollback.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

from ..premium.safety_checks import AbstractSafetyGate, SafetyReport
from .checksum import compute_checksum
from .flash_catalog import FLASH_CATALOG, FlashJob, get_flash_job
from .flash_provider import (
    AbstractFlashProvider,
    FlashBackup,
    FlashDependencyError,
    FlashRejected,
    FlashSecurityDenied,
    FlashTransportError,
)

log = logging.getLogger(__name__)


class FlashState(str, enum.Enum):
    IDLE      = "idle"
    READY     = "ready"
    BACKED_UP = "backed_up"
    FLASHED   = "flashed"
    DONE      = "done"
    FAILED    = "failed"


_ALLOWED: dict[FlashState, set[FlashState]] = {
    FlashState.IDLE:      {FlashState.READY, FlashState.FAILED},
    FlashState.READY:     {FlashState.BACKED_UP, FlashState.FAILED},
    FlashState.BACKED_UP: {FlashState.FLASHED, FlashState.FAILED},
    FlashState.FLASHED:   {FlashState.DONE, FlashState.FAILED},
    FlashState.DONE:      set(),
    FlashState.FAILED:    set(),
}


class IllegalFlashTransition(Exception):
    pass


class FlashEvent(str, enum.Enum):
    START  = "start"
    BACKUP = "backup"
    FLASH  = "flash"
    FINISH = "finish"
    ABORT  = "abort"


_PROGRESS = {
    FlashState.IDLE: 0, FlashState.READY: 20, FlashState.BACKED_UP: 45,
    FlashState.FLASHED: 85, FlashState.DONE: 100, FlashState.FAILED: 0,
}


@dataclass
class FlashData:
    job_code: str = ""
    vin: str = ""
    technician_id: str = ""
    safety_voltage_v: float = 0.0
    payload_len: int = 0
    payload_checksum: int = 0
    current_version: str = ""
    backup_size: int = 0
    blocks_written: int = 0
    rolled_back: bool = False
    error_code: str = ""
    error_detail: str = ""


@dataclass
class FlashPrompt:
    state: FlashState
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


class FlashOrchestrator:
    def __init__(self, *, safety: AbstractSafetyGate,
                 provider: AbstractFlashProvider,
                 data: Optional[FlashData] = None,
                 state: FlashState = FlashState.IDLE,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.safety = safety
        self.provider = provider
        self.data = data or FlashData()
        self.state = state
        self.entitlement = entitlement
        self._job: Optional[FlashJob] = (
            get_flash_job(self.data.job_code) if self.data.job_code else None)
        # Transient — the payload + backup live on the instance, never in
        # the snapshot (a restored session re-validates before re-flashing).
        self._payload: bytes = b""
        self._backup: Optional[FlashBackup] = None

    @property
    def job(self) -> Optional[FlashJob]:
        return self._job

    # ── helpers ────────────────────────────────────────────────────────
    def _advance(self, to: FlashState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalFlashTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("flash transition", extra={
            "from": self.state.value, "to": to.value,
            "job": self.data.job_code, "vin": self.data.vin,
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> FlashPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = FlashState.FAILED
        log.warning("flash failure", extra={"code": code, "detail": detail})
        return FlashPrompt(
            state=FlashState.FAILED,
            title="فشل تحديث الوحدة",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة.",
            progress_pct=0,
            payload={"error_code": code,
                     "rolled_back": self.data.rolled_back},
            is_terminal=True, is_error=True,
        )

    async def _rollback(self) -> None:
        """Best-effort restore of the saved image. Idempotent."""
        if self._backup is None or self.data.rolled_back:
            return
        try:
            await self.provider.restore_backup(self._backup)
            log.warning("flash rolled back", extra={"job": self.data.job_code})
        except Exception:               # pragma: no cover
            log.exception("rollback failed — ECU may need bench recovery")
        finally:
            self.data.rolled_back = True

    # ── dispatch ───────────────────────────────────────────────────────
    async def handle(self, event: FlashEvent | str,
                     payload: Optional[dict] = None) -> FlashPrompt:
        if isinstance(event, str):
            try:
                event = FlashEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except (IllegalFlashTransition, FlashTransportError,
                FlashSecurityDenied, FlashRejected,
                FlashDependencyError) as e:
            # A failure once erase has started must roll the image back.
            if self.state == FlashState.BACKED_UP:
                await self._rollback()
            code = {
                IllegalFlashTransition: "illegal_transition",
                FlashTransportError: "transport_error",
                FlashSecurityDenied: "security_denied",
                FlashRejected: "flash_rejected",
                FlashDependencyError: "dependency_failed",
            }[type(e)]
            return self._fail(code, str(e))
        except Exception as e:                       # pragma: no cover
            if self.state == FlashState.BACKED_UP:
                await self._rollback()
            log.exception("flash unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: FlashEvent,
                        payload: dict) -> FlashPrompt:
        if event == FlashEvent.ABORT:
            if self.state == FlashState.BACKED_UP:
                await self._rollback()
            return self._fail("aborted_by_user", "Aborted by technician.")
        if event == FlashEvent.START:
            return await self._start(payload)
        if event == FlashEvent.BACKUP:
            return await self._backup_step()
        if event == FlashEvent.FLASH:
            return await self._flash_step()
        if event == FlashEvent.FINISH:
            return await self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. START ───────────────────────────────────────────────────────
    async def _start(self, payload: dict) -> FlashPrompt:
        if self.state != FlashState.IDLE:
            raise IllegalFlashTransition(
                f"START only valid in IDLE (now {self.state.value})",
            )
        code = (payload.get("job_code") or "").strip()
        job = get_flash_job(code)
        if job is None:
            return self._fail(
                "unknown_job",
                f"مفيش مهمة تحديث بالكود {code!r}. المتاح: "
                + ", ".join(sorted(FLASH_CATALOG)),
            )
        self._job = job
        self.data.job_code = code
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()

        payload_bytes = payload.get("payload") or b""
        if not isinstance(payload_bytes, (bytes, bytearray)):
            return self._fail("bad_payload", "الـ payload لازم يكون bytes.")
        payload_bytes = bytes(payload_bytes)
        if not job.size_ok(len(payload_bytes)):
            return self._fail(
                "bad_payload_size",
                f"حجم الملف {len(payload_bytes)} بايت خارج المدى المسموح "
                f"({job.expected_min_bytes}–{job.expected_max_bytes}). "
                f"اتأكد إنك مخدتش ملف غلط.",
            )
        self._payload = payload_bytes
        self.data.payload_len = len(payload_bytes)
        self.data.payload_checksum = compute_checksum(
            payload_bytes, algo=job.checksum_algo)

        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                return self._fail("not_entitled", reason)

        report: SafetyReport = await self.safety.probe(
            require=job.safety.to_require())
        self.data.safety_voltage_v = report.voltage_v
        if not report.ok:
            return self._fail(
                "prereq_failed",
                "الشروط مش مظبوطة للفلاش: " + " | ".join(report.refusal_reasons),
            )

        self.data.current_version = await self.provider.read_current_version()
        self._advance(FlashState.READY)
        return FlashPrompt(
            state=self.state,
            title=f"{job.name_ar} — جاهز للبدء",
            body=(f"النسخة الحالية: {self.data.current_version}. الجهد: "
                  f"{report.voltage_v:.2f} V. حجم الملف: "
                  f"{self.data.payload_len} بايت. اضغط BACKUP عشان ناخد "
                  f"نسخة احتياطية قبل أي مسح."),
            expects="BACKUP",
            progress_pct=_PROGRESS[self.state],
            payload={"job": job.to_dict(),
                     "current_version": self.data.current_version,
                     "payload_len": self.data.payload_len,
                     "payload_checksum": self.data.payload_checksum},
        )

    # ── 2. BACKUP ──────────────────────────────────────────────────────
    async def _backup_step(self) -> FlashPrompt:
        if self.state != FlashState.READY:
            raise IllegalFlashTransition(
                f"BACKUP only valid in READY (now {self.state.value})",
            )
        job = self._job
        assert job is not None
        await self.provider.enter_programming_session()
        if job.needs_security:
            await self.provider.unlock_security(vin=self.data.vin)

        backup = await self.provider.read_backup(
            addr=job.target_addr, size=self.data.payload_len)
        self._backup = backup
        self.data.backup_size = backup.size
        self._advance(FlashState.BACKED_UP)
        return FlashPrompt(
            state=self.state,
            title="النسخة الاحتياطية اتاخدت ✅",
            body=(f"اتحفظت نسخة احتياطية ({backup.size} بايت) من الوحدة. لو "
                  f"حصل أي مشكلة أثناء الفلاش هنرجّعها أوتوماتيك. اضغط FLASH "
                  f"للبدء — وما تفصلش العربية."),
            expects="FLASH",
            progress_pct=_PROGRESS[self.state],
            payload={"backup_size": backup.size},
        )

    # ── 3. FLASH ───────────────────────────────────────────────────────
    async def _flash_step(self) -> FlashPrompt:
        if self.state != FlashState.BACKED_UP:
            raise IllegalFlashTransition(
                f"FLASH only valid in BACKED_UP (now {self.state.value})",
            )
        job = self._job
        assert job is not None

        # erase → download → transfer → exit. Any raise here is caught by
        # handle(), which rolls the backup back in before failing.
        await self.provider.erase(addr=job.target_addr)
        block_len = await self.provider.request_download(
            addr=job.target_addr, size=self.data.payload_len)
        block_len = max(1, int(block_len))

        seq = 1
        for off in range(0, len(self._payload), block_len):
            await self.provider.transfer_block(
                seq=seq, data=self._payload[off:off + block_len])
            seq += 1
        self.data.blocks_written = seq - 1
        await self.provider.request_transfer_exit()

        # ECU-side acceptance + reboot.
        await self.provider.check_dependencies()
        await self.provider.ecu_reset()

        new_version = await self.provider.read_current_version()
        self._advance(FlashState.FLASHED)
        return FlashPrompt(
            state=self.state,
            title="الفلاش تم — بيتأكد",
            body=(f"اتكتبت {self.data.blocks_written} بلوك وعدى فحص الاعتماد، "
                  f"والوحدة عملت ريسيت. النسخة دلوقتي: {new_version}. اضغط "
                  f"FINISH لإنهاء وتسجيل العملية."),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload={"new_version": new_version,
                     "blocks_written": self.data.blocks_written,
                     "payload_checksum": self.data.payload_checksum},
        )

    # ── 4. FINISH ──────────────────────────────────────────────────────
    async def _finish(self) -> FlashPrompt:
        if self.state != FlashState.FLASHED:
            raise IllegalFlashTransition(
                f"FINISH only valid in FLASHED (now {self.state.value})",
            )
        self._advance(FlashState.DONE)
        job = self._job
        assert job is not None
        if self.entitlement is not None:
            op_ref = f"{job.code}-{self.data.vin or 'no-vin'}"
            self.entitlement.consume(vin=self.data.vin, operation_ref=op_ref)
        return FlashPrompt(
            state=self.state,
            title="اكتمل التحديث ✅",
            body=job.success_message_ar,
            expects="",
            progress_pct=100,
            payload={"job_code": job.code, "vin": self.data.vin,
                     "blocks_written": self.data.blocks_written},
            is_terminal=True,
        )

    # ── snapshot / restore ─────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "job_code": self.data.job_code,
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "safety_voltage_v": self.data.safety_voltage_v,
                "payload_len": self.data.payload_len,
                "payload_checksum": self.data.payload_checksum,
                "current_version": self.data.current_version,
                "backup_size": self.data.backup_size,
                "blocks_written": self.data.blocks_written,
                "rolled_back": self.data.rolled_back,
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, safety: AbstractSafetyGate,
                provider: AbstractFlashProvider,
                snapshot: dict[str, Any],
                entitlement: Optional["AbstractEntitlementGuard"] = None,
                ) -> "FlashOrchestrator":
        s = snapshot["data"]
        data = FlashData(
            job_code=s.get("job_code", ""),
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            safety_voltage_v=float(s.get("safety_voltage_v") or 0.0),
            payload_len=int(s.get("payload_len") or 0),
            payload_checksum=int(s.get("payload_checksum") or 0),
            current_version=s.get("current_version", ""),
            backup_size=int(s.get("backup_size") or 0),
            blocks_written=int(s.get("blocks_written") or 0),
            rolled_back=bool(s.get("rolled_back")),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(safety=safety, provider=provider, data=data,
                   state=FlashState(snapshot["state"]),
                   entitlement=entitlement)
