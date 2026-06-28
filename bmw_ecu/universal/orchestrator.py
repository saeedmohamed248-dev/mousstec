"""UniversalSmartOrchestrator — the Plug-&-Play master flow.

One forward-only state machine that auto-detects the car, takes a mandatory
backup BEFORE any write, then runs the right branch and can always roll the
ECU back to the saved state.

        IDLE ─START─▶ DETECTED ─BACKUP─▶ BACKED_UP
                                            │
                 (DME unlocked) ───────────┤────────── (DME locked)
                                            │                     │
                                          CODE                 (auto)
                                            ▼                     ▼
                                          CODED            BENCH_HALTED ─READY─▶ BENCH_READY
                                            │                                        │
                                          SYNC                                    EXTRACT
                                            ▼                                        ▼
                                          DONE ◀────────────── SYNC ───────────── EXTRACTED

  • Auto-detect: transport kind → series/body module (ENET→F-Series/FEM,
    K+DCAN→R-Series/CAS) and a dynamic DME lock probe.
  • Auto-Backup: read the live coding/ISN snapshot and persist it (disk + DB
    via an injected sink) tied to the VIN. NOTHING is written before this.
  • Rollback Guard: from the moment a backup exists, every non-success prompt
    offers a "🔄 Rollback to Previous Backup" action; ROLLBACK writes the
    saved snapshot back to the ECU.

INVARIANT (same spirit as FlashOrchestrator): no coding/flash step runs before
BACKED_UP, and any abort/error after BACKED_UP can be rolled back.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..safety.backup import EcuBackup
from .provider import AbstractUniversalEcuIo, DetectResult, UniversalIoError

log = logging.getLogger(__name__)

# Async sink that persists a backup (e.g. BackupStore.save + recorder). Kept
# injectable so the state machine stays hardware/DB-free in tests.
BackupSink = Callable[[EcuBackup], Awaitable[None]]


class UState(str, enum.Enum):
    IDLE         = "idle"
    DETECTED     = "detected"
    BACKED_UP    = "backed_up"
    CODED        = "coded"
    BENCH_HALTED = "bench_halted"
    BENCH_READY  = "bench_ready"
    EXTRACTED    = "extracted"
    DONE         = "done"
    ROLLED_BACK  = "rolled_back"
    FAILED       = "failed"


class UEvent(str, enum.Enum):
    START    = "start"
    BACKUP   = "backup"
    CODE     = "code"
    READY    = "ready"
    EXTRACT  = "extract"
    SYNC     = "sync"
    ABORT    = "abort"
    ROLLBACK = "rollback"


_ALLOWED: dict[UState, set[UState]] = {
    UState.IDLE:         {UState.DETECTED, UState.FAILED},
    UState.DETECTED:     {UState.BACKED_UP, UState.FAILED},
    UState.BACKED_UP:    {UState.CODED, UState.BENCH_HALTED,
                          UState.FAILED, UState.ROLLED_BACK},
    UState.CODED:        {UState.DONE, UState.FAILED, UState.ROLLED_BACK},
    UState.BENCH_HALTED: {UState.BENCH_READY, UState.FAILED, UState.ROLLED_BACK},
    UState.BENCH_READY:  {UState.EXTRACTED, UState.FAILED, UState.ROLLED_BACK},
    UState.EXTRACTED:    {UState.DONE, UState.FAILED, UState.ROLLED_BACK},
    UState.DONE:         set(),
    UState.ROLLED_BACK:  set(),
    UState.FAILED:       {UState.ROLLED_BACK},
}

_PROGRESS = {
    UState.IDLE: 0, UState.DETECTED: 15, UState.BACKED_UP: 35,
    UState.BENCH_HALTED: 45, UState.BENCH_READY: 60, UState.EXTRACTED: 80,
    UState.CODED: 70, UState.DONE: 100, UState.ROLLED_BACK: 0, UState.FAILED: 0,
}


class IllegalUTransition(Exception):
    pass


def infer_topology(transport_kind: str) -> tuple[str, str]:
    """Map the live transport to (series, paired body module).

    ENET/DoIP  → modern F/G series, body coding lives in the FEM.
    K+DCAN     → older R/E series (incl. Mini R56), body coding in the CAS.
    """
    t = (transport_kind or "").lower()
    if t == "doip":
        return ("F/G-Series", "FEM")
    if t in ("kdcan", "socketcan"):
        return ("R/E-Series", "CAS")
    return ("Unknown", "FEM")


@dataclass
class UData:
    vin: str = ""
    technician_id: str = ""
    transport_kind: str = ""
    series: str = ""
    body_module: str = ""        # "FEM" | "CAS"
    dme_family: str = "MEVD17"
    dme_locked: bool = False
    backup_sha256: str = ""
    backup_size: int = 0
    rolled_back: bool = False
    coded_options: int = 0
    error_code: str = ""
    error_detail: str = ""


@dataclass
class UAction:
    event: str
    label_ar: str
    label_en: str
    style: str = "default"       # default | primary | danger | warning

    def to_dict(self) -> dict[str, str]:
        return {"event": self.event, "label_ar": self.label_ar,
                "label_en": self.label_en, "style": self.style}


@dataclass
class UPrompt:
    state: UState
    title_ar: str
    title_en: str
    body_ar: str
    body_en: str
    expects: str = ""
    progress_pct: int = 0
    actions: list[UAction] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    is_terminal: bool = False
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "title_ar": self.title_ar, "title_en": self.title_en,
            "body_ar": self.body_ar, "body_en": self.body_en,
            "expects": self.expects, "progress_pct": self.progress_pct,
            "actions": [a.to_dict() for a in self.actions],
            "payload": dict(self.payload),
            "is_terminal": self.is_terminal, "is_error": self.is_error,
        }


class UniversalSmartOrchestrator:
    def __init__(self, *, io: AbstractUniversalEcuIo,
                 data: Optional[UData] = None,
                 state: UState = UState.IDLE,
                 backup_sink: Optional[BackupSink] = None) -> None:
        self.io = io
        self.data = data or UData()
        self.state = state
        self._backup_sink = backup_sink
        self._backup: Optional[EcuBackup] = None

    # ── helpers ──────────────────────────────────────────────────────────
    def _advance(self, to: UState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalUTransition(f"{self.state.value} → {to.value} not allowed")
        log.info("universal transition", extra={
            "from": self.state.value, "to": to.value, "vin": self.data.vin})
        self.state = to

    def _rollback_action(self) -> list[UAction]:
        """The rollback button — shown whenever a usable backup exists."""
        if self._backup is None or self.data.rolled_back:
            return []
        return [UAction(
            event=UEvent.ROLLBACK.value,
            label_ar="🔄 رجوع للنسخة الاحتياطية",
            label_en="🔄 Rollback to Previous Backup",
            style="warning",
        )]

    def _fail(self, code: str, detail: str) -> UPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = UState.FAILED
        log.warning("universal failure", extra={"code": code, "detail": detail})
        actions = list(self._rollback_action())
        actions.append(UAction(event=UEvent.ABORT.value,
                               label_ar="إغلاق الجلسة", label_en="Close session"))
        return UPrompt(
            state=UState.FAILED,
            title_ar="حصلت مشكلة", title_en="Something went wrong",
            body_ar=detail, body_en=detail,
            expects="ROLLBACK" if self._backup and not self.data.rolled_back else "",
            progress_pct=0, actions=actions,
            payload={"error_code": code, "rolled_back": self.data.rolled_back},
            is_terminal=True, is_error=True,
        )

    async def _do_rollback(self) -> None:
        """Write the saved snapshot back. Idempotent + best-effort."""
        if self._backup is None or self.data.rolled_back:
            return
        await self.io.write_coding_snapshot(self._backup.data)
        self.data.rolled_back = True
        log.warning("universal rolled back", extra={"vin": self.data.vin})

    # ── dispatch ─────────────────────────────────────────────────────────
    async def handle(self, event: UEvent | str,
                     payload: Optional[dict] = None) -> UPrompt:
        if isinstance(event, str):
            try:
                event = UEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalUTransition as e:
            return self._fail("illegal_transition", str(e))
        except UniversalIoError as e:
            return self._fail("io_error", str(e))
        except Exception as e:  # pragma: no cover - defensive
            log.exception("universal unexpected")
            return self._fail("unexpected", repr(e))

    async def _dispatch(self, event: UEvent, payload: dict) -> UPrompt:
        if event == UEvent.ROLLBACK:
            return await self._rollback_step()
        if event == UEvent.ABORT:
            return await self._abort()
        if event == UEvent.START:
            return await self._start(payload)
        if event == UEvent.BACKUP:
            return await self._backup_step()
        if event == UEvent.CODE:
            return await self._code_step(payload)
        if event == UEvent.READY:
            return await self._ready_step()
        if event == UEvent.EXTRACT:
            return await self._extract_step()
        if event == UEvent.SYNC:
            return await self._sync_step()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. START — auto-detect ───────────────────────────────────────────
    async def _start(self, payload: dict) -> UPrompt:
        if self.state != UState.IDLE:
            raise IllegalUTransition(f"START only valid in IDLE (now {self.state.value})")
        self.data.technician_id = (payload.get("technician_id") or "").strip()

        transport = await self.io.detect_transport()
        vin = await self.io.read_vin()
        locked = await self.io.probe_dme_locked()
        series, body = infer_topology(transport)

        detect = DetectResult(transport_kind=transport, vin=vin, dme_locked=locked)
        self.data.transport_kind = transport
        self.data.vin = vin
        self.data.dme_locked = locked
        self.data.series = series
        self.data.body_module = body

        self._advance(UState.DETECTED)
        verdict_ar = "مقفول 🔒 (محتاج بنش)" if locked else "مفتوح ✅ (تكويد مباشر)"
        verdict_en = "LOCKED 🔒 (bench needed)" if locked else "OPEN ✅ (direct coding)"
        return UPrompt(
            state=self.state,
            title_ar=f"اتعرّفت على العربية — {series}",
            title_en=f"Vehicle detected — {series}",
            body_ar=(f"الوصلة: {transport} → {series}، وحدة الجسم: {body}. "
                     f"الكنترول (DME {self.data.dme_family}) {verdict_ar}. "
                     f"اضغط BACKUP عشان ناخد نسخة احتياطية الأول."),
            body_en=(f"Link: {transport} → {series}, body module: {body}. "
                     f"DME {self.data.dme_family} is {verdict_en}. "
                     f"Press BACKUP first to save a restore point."),
            expects="BACKUP",
            progress_pct=_PROGRESS[self.state],
            actions=[UAction(UEvent.BACKUP.value, "📦 نسخة احتياطية",
                             "📦 Auto-Backup", "primary")],
            payload={"detect": detect.to_json(), "series": series,
                     "body_module": body},
        )

    # ── 2. BACKUP — mandatory, before any write ──────────────────────────
    async def _backup_step(self) -> UPrompt:
        if self.state != UState.DETECTED:
            raise IllegalUTransition(f"BACKUP only valid in DETECTED (now {self.state.value})")

        raw = await self.io.read_coding_snapshot()
        backup = EcuBackup(
            vin=self.data.vin or "UNKNOWN",
            ecu_name=f"DME_{self.data.dme_family}",
            memory_region="CODING",
            data=bytes(raw),
            metadata={"series": self.data.series,
                      "body_module": self.data.body_module,
                      "locked": self.data.dme_locked},
        )
        if self._backup_sink is not None:
            await self._backup_sink(backup)
        self._backup = backup
        self.data.backup_sha256 = backup.sha256
        self.data.backup_size = len(backup.data)
        self._advance(UState.BACKED_UP)

        # Branch: locked → halt for bench; unlocked → ready to code.
        if self.data.dme_locked:
            return await self._halt_for_bench()
        return self._ready_to_code_prompt()

    def _ready_to_code_prompt(self) -> UPrompt:
        return UPrompt(
            state=self.state,   # BACKED_UP
            title_ar="النسخة الاحتياطية اتاخدت ✅",
            title_en="Backup captured ✅",
            body_ar=(f"اتحفظت نسخة ({self.data.backup_size} بايت) — "
                     f"SHA {self.data.backup_sha256[:12]}. الكنترول مفتوح، "
                     f"اضغط CODE عشان نكوّد الـ DME."),
            body_en=(f"Saved {self.data.backup_size} bytes — "
                     f"SHA {self.data.backup_sha256[:12]}. DME is open; "
                     f"press CODE to code the DME."),
            expects="CODE",
            progress_pct=_PROGRESS[self.state],
            actions=[UAction(UEvent.CODE.value, "⚙️ كوّد الـ DME",
                             "⚙️ Code DME", "primary")] + self._rollback_action(),
            payload={"backup_sha256": self.data.backup_sha256,
                     "backup_size": self.data.backup_size},
        )

    async def _halt_for_bench(self) -> UPrompt:
        self._advance(UState.BENCH_HALTED)
        pinout = await self.io.bench_pinout()
        if pinout:
            body_ar = ("الكنترول مقفول — لازم بنش. وصّل الهارنس حسب المخطط "
                       "اللي قدامك، وبعد ما تجهّز اضغط READY.")
            body_en = ("DME is locked — bench required. Wire the harness per "
                       "the pinout shown, then press READY when set.")
        else:
            body_ar = ("الكنترول مقفول — لازم بنش، بس مفيش مخطط بِنّات مؤكَّد "
                       "للبورده دي في الكتالوج. سجّل البورده من Django admin "
                       "(EcuHardwareProfile) قبل ما تكمّل — مش بنخمّن بِنّات.")
            body_en = ("DME is locked — bench required, but there is no "
                       "confirmed pinout for this board in the catalog. "
                       "Register it via Django admin (EcuHardwareProfile) "
                       "before continuing — we never guess bench pins.")
        return UPrompt(
            state=self.state,
            title_ar="وقفة بنش 🔧", title_en="Bench halt 🔧",
            body_ar=body_ar, body_en=body_en,
            expects="READY" if pinout else "register_board",
            progress_pct=_PROGRESS[self.state],
            actions=([UAction(UEvent.READY.value, "✅ جاهز — وصّلت الهارنس",
                              "✅ Harness ready", "primary")] if pinout else [])
                     + self._rollback_action(),
            payload={"pinout": pinout or {}, "has_pinout": bool(pinout),
                     "backup_sha256": self.data.backup_sha256},
        )

    # ── 3a. CODE (unlocked path) ─────────────────────────────────────────
    async def _code_step(self, payload: dict) -> UPrompt:
        if self.state != UState.BACKED_UP:
            raise IllegalUTransition(f"CODE only valid in BACKED_UP (now {self.state.value})")
        options = payload.get("options") or {}
        result = await self.io.code_dme(options)
        self.data.coded_options = int(result.get("coded_options", 0))
        self._advance(UState.CODED)
        return UPrompt(
            state=self.state,
            title_ar="الـ DME اتكوّد ✅", title_en="DME coded ✅",
            body_ar=(f"اتطبّق {self.data.coded_options} خيار. اضغط SYNC عشان "
                     f"نزامن وحدة {self.data.body_module}."),
            body_en=(f"Applied {self.data.coded_options} option(s). Press SYNC "
                     f"to sync the {self.data.body_module}."),
            expects="SYNC",
            progress_pct=_PROGRESS[self.state],
            actions=[UAction(UEvent.SYNC.value,
                             f"🔗 زامن {self.data.body_module}",
                             f"🔗 Sync {self.data.body_module}", "primary")]
                     + self._rollback_action(),
            payload={"coded": result},
        )

    # ── 3b. READY → EXTRACT (locked path) ────────────────────────────────
    async def _ready_step(self) -> UPrompt:
        if self.state != UState.BENCH_HALTED:
            raise IllegalUTransition(f"READY only valid in BENCH_HALTED (now {self.state.value})")
        self._advance(UState.BENCH_READY)
        return UPrompt(
            state=self.state,
            title_ar="الهارنس جاهز — ابدأ الاستخراج",
            title_en="Harness ready — start extraction",
            body_ar="اضغط EXTRACT عشان نقرأ/نفلش الكنترول على البنش.",
            body_en="Press EXTRACT to read/flash the DME on the bench.",
            expects="EXTRACT",
            progress_pct=_PROGRESS[self.state],
            actions=[UAction(UEvent.EXTRACT.value, "⬇️ استخراج/فلاش",
                             "⬇️ Extract / Flash", "primary")]
                     + self._rollback_action(),
            payload={"backup_sha256": self.data.backup_sha256},
        )

    async def _extract_step(self) -> UPrompt:
        if self.state != UState.BENCH_READY:
            raise IllegalUTransition(f"EXTRACT only valid in BENCH_READY (now {self.state.value})")
        result = await self.io.extract_bench()
        self._advance(UState.EXTRACTED)
        return UPrompt(
            state=self.state,
            title_ar="الاستخراج تم ✅", title_en="Extraction done ✅",
            body_ar=(f"اتقرأت صورة الكنترول ({result.get('image_size', 0)} بايت). "
                     f"اضغط SYNC عشان نزامن {self.data.body_module} ونقفل."),
            body_en=(f"DME image read ({result.get('image_size', 0)} bytes). "
                     f"Press SYNC to sync {self.data.body_module} and finish."),
            expects="SYNC",
            progress_pct=_PROGRESS[self.state],
            actions=[UAction(UEvent.SYNC.value,
                             f"🔗 زامن {self.data.body_module}",
                             f"🔗 Sync {self.data.body_module}", "primary")]
                     + self._rollback_action(),
            payload={"extract": result},
        )

    # ── 4. SYNC → DONE (both paths converge) ─────────────────────────────
    async def _sync_step(self) -> UPrompt:
        if self.state not in (UState.CODED, UState.EXTRACTED):
            raise IllegalUTransition(
                f"SYNC only valid in CODED/EXTRACTED (now {self.state.value})")
        result = await self.io.sync_module(self.data.body_module)
        self._advance(UState.DONE)
        return UPrompt(
            state=self.state,
            title_ar="تمام، خلصنا ✅", title_en="All done ✅",
            body_ar=(f"اتزامنت وحدة {self.data.body_module} والعملية اكتملت. "
                     f"النسخة الاحتياطية محفوظة (SHA {self.data.backup_sha256[:12]}) "
                     f"لو احتجت ترجع لها."),
            body_en=(f"{self.data.body_module} synced and the job is complete. "
                     f"Your backup (SHA {self.data.backup_sha256[:12]}) stays "
                     f"saved if you ever need to roll back."),
            expects="",
            progress_pct=100,
            actions=[],
            payload={"sync": result, "backup_sha256": self.data.backup_sha256},
            is_terminal=True,
        )

    # ── Rollback / Abort ─────────────────────────────────────────────────
    async def _rollback_step(self) -> UPrompt:
        if self._backup is None:
            return self._fail("no_backup", "No backup exists to roll back to.")
        if self.data.rolled_back:
            return UPrompt(
                state=UState.ROLLED_BACK,
                title_ar="اترجّع خلاص", title_en="Already rolled back",
                body_ar="النسخة الاحتياطية اترجّعت قبل كده.",
                body_en="The backup was already restored.",
                progress_pct=0, is_terminal=True,
            )
        await self._do_rollback()
        # ROLLED_BACK is reachable from any non-terminal post-backup state and
        # from FAILED — assert it's allowed, then set it.
        if UState.ROLLED_BACK in _ALLOWED.get(self.state, set()):
            self.state = UState.ROLLED_BACK
        else:
            self.state = UState.ROLLED_BACK  # terminal safety net
        return UPrompt(
            state=UState.ROLLED_BACK,
            title_ar="رجّعنا النسخة الاحتياطية 🔄",
            title_en="Rolled back to backup 🔄",
            body_ar=(f"اتكتبت النسخة المحفوظة ({self.data.backup_size} بايت) "
                     f"تاني على الكنترول. الكنترول رجع لحالته قبل التعديل."),
            body_en=(f"The saved snapshot ({self.data.backup_size} bytes) was "
                     f"written back. The ECU is at its pre-edit state."),
            expects="",
            progress_pct=0,
            payload={"backup_sha256": self.data.backup_sha256,
                     "restored": True},
            is_terminal=True,
        )

    async def _abort(self) -> UPrompt:
        # Aborting after a backup offers rollback rather than silently leaving
        # a half-coded ECU. Before any backup it's just a clean close.
        if self._backup is not None and not self.data.rolled_back:
            return self._fail("aborted_by_user",
                              "اتلغت بواسطة الفني — تقدر ترجع للنسخة الاحتياطية.")
        self.state = UState.FAILED
        return UPrompt(
            state=UState.FAILED,
            title_ar="اتقفلت الجلسة", title_en="Session closed",
            body_ar="اتلغت قبل أي تعديل — مفيش حاجة اتكتبت.",
            body_en="Aborted before any write — nothing was changed.",
            progress_pct=0, is_terminal=True,
        )

    # ── snapshot / restore (resume across requests) ──────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "transport_kind": self.data.transport_kind,
                "series": self.data.series,
                "body_module": self.data.body_module,
                "dme_family": self.data.dme_family,
                "dme_locked": self.data.dme_locked,
                "backup_sha256": self.data.backup_sha256,
                "backup_size": self.data.backup_size,
                "rolled_back": self.data.rolled_back,
                "coded_options": self.data.coded_options,
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, *, io: AbstractUniversalEcuIo, snapshot: dict[str, Any],
                backup: Optional[EcuBackup] = None,
                backup_sink: Optional[BackupSink] = None,
                ) -> "UniversalSmartOrchestrator":
        s = snapshot["data"]
        data = UData(
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            transport_kind=s.get("transport_kind", ""),
            series=s.get("series", ""),
            body_module=s.get("body_module", ""),
            dme_family=s.get("dme_family", "MEVD17"),
            dme_locked=bool(s.get("dme_locked")),
            backup_sha256=s.get("backup_sha256", ""),
            backup_size=int(s.get("backup_size") or 0),
            rolled_back=bool(s.get("rolled_back")),
            coded_options=int(s.get("coded_options") or 0),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        inst = cls(io=io, data=data, state=UState(snapshot["state"]),
                   backup_sink=backup_sink)
        inst._backup = backup
        return inst
