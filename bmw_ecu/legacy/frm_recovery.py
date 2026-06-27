"""FRM3 recovery orchestrator.

State-machine driven flow that takes a bricked FRM3 from "0 lamps
respond" → "fully restored + recoded" through a chatbot-guided
sequence the technician walks through on the bench. Mirrors the
bench_orchestrator design (sub-commit 4) so the chatbot frontend can
render both flows with one component.

The transitions are forward-only; every state may transition to FAILED.

  IDLE → MODEL_SELECTED → BDM_CONNECTED → DFLASH_READ
       → CORRUPTION_ANALYZED → CLOUD_REBUILT → DFLASH_FLASHED
       → VO_FA_INJECTED → VERIFIED → DONE
                                  ↘ FAILED

Production wires this through api/views.py + a WizardSession row so
the flow survives a tab refresh; the orchestrator exposes
snapshot()/restore() for that purpose.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .bdm_transport import (
    AbstractBdmTransport,
    BdmConnectionError,
    BdmReadError,
    BdmWriteError,
)
from .cloud_rebuild import (
    CloudRebuildError,
    CloudRebuildResult,
    rebuild_dflash,
)
from .dflash_corruption import (
    CorruptionLevel,
    CorruptionReport,
    analyze_dflash,
)
from .frm_profiles import (
    FRM_PROFILES,
    FrmProfile,
    FrmVariant,
    get_frm_profile,
)
from .vo_fa_injector import (
    FaPayload,
    SalapaInjectionError,
    build_fa_payload,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
class FrmRecoveryState(str, enum.Enum):
    IDLE                 = "idle"
    MODEL_SELECTED       = "model_selected"
    BDM_CONNECTED        = "bdm_connected"
    DFLASH_READ          = "dflash_read"
    CORRUPTION_ANALYZED  = "corruption_analyzed"
    CLOUD_REBUILT        = "cloud_rebuilt"
    DFLASH_FLASHED       = "dflash_flashed"
    VO_FA_INJECTED       = "vo_fa_injected"
    VERIFIED             = "verified"
    DONE                 = "done"
    FAILED               = "failed"


_ALLOWED: dict[FrmRecoveryState, set[FrmRecoveryState]] = {
    FrmRecoveryState.IDLE:                 {FrmRecoveryState.MODEL_SELECTED, FrmRecoveryState.FAILED},
    FrmRecoveryState.MODEL_SELECTED:       {FrmRecoveryState.BDM_CONNECTED, FrmRecoveryState.FAILED},
    FrmRecoveryState.BDM_CONNECTED:        {FrmRecoveryState.DFLASH_READ, FrmRecoveryState.FAILED},
    FrmRecoveryState.DFLASH_READ:          {FrmRecoveryState.CORRUPTION_ANALYZED, FrmRecoveryState.FAILED},
    FrmRecoveryState.CORRUPTION_ANALYZED:  {FrmRecoveryState.CLOUD_REBUILT, FrmRecoveryState.FAILED},
    FrmRecoveryState.CLOUD_REBUILT:        {FrmRecoveryState.DFLASH_FLASHED, FrmRecoveryState.FAILED},
    FrmRecoveryState.DFLASH_FLASHED:       {FrmRecoveryState.VO_FA_INJECTED, FrmRecoveryState.FAILED},
    FrmRecoveryState.VO_FA_INJECTED:       {FrmRecoveryState.VERIFIED, FrmRecoveryState.FAILED},
    FrmRecoveryState.VERIFIED:             {FrmRecoveryState.DONE, FrmRecoveryState.FAILED},
    FrmRecoveryState.DONE:                 set(),
    FrmRecoveryState.FAILED:               set(),
}


class IllegalFrmTransition(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────
@dataclass
class FrmRecoveryData:
    variant: Optional[FrmVariant] = None
    vin: str = ""
    technician_id: str = ""
    raw_dump: bytes = b""
    analyzer_report: Optional[dict] = None    # CorruptionReport.__dict__
    rebuild_result: Optional[dict] = None     # CloudRebuildResult metadata
    rebuilt_bytes: bytes = b""
    fa_codes: tuple[str, ...] = ()
    fa_payload_hex: str = ""
    notes: list[str] = field(default_factory=list)
    error_code: str = ""
    error_detail: str = ""


# ─────────────────────────────────────────────────────────────────────
@dataclass
class FrmRecoveryPrompt:
    state: FrmRecoveryState
    title: str
    body: str
    expects: str = ""
    progress_pct: int = 0
    payload: dict = field(default_factory=dict)
    is_terminal: bool = False
    is_error: bool = False

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "title": self.title,
            "body": self.body,
            "expects": self.expects,
            "progress_pct": self.progress_pct,
            "payload": dict(self.payload),
            "is_terminal": self.is_terminal,
            "is_error": self.is_error,
        }


class FrmRecoveryEvent(str, enum.Enum):
    SELECT_MODEL        = "select_model"
    CONNECT_BDM         = "connect_bdm"
    READ_DFLASH         = "read_dflash"
    ANALYZE             = "analyze"
    REBUILD             = "rebuild"
    FLASH_BACK          = "flash_back"
    INJECT_VO_FA        = "inject_vo_fa"
    VERIFY              = "verify"
    FINISH              = "finish"
    ABORT               = "abort"


_PROGRESS = {
    FrmRecoveryState.IDLE: 0,
    FrmRecoveryState.MODEL_SELECTED: 10,
    FrmRecoveryState.BDM_CONNECTED: 20,
    FrmRecoveryState.DFLASH_READ: 35,
    FrmRecoveryState.CORRUPTION_ANALYZED: 45,
    FrmRecoveryState.CLOUD_REBUILT: 60,
    FrmRecoveryState.DFLASH_FLASHED: 80,
    FrmRecoveryState.VO_FA_INJECTED: 90,
    FrmRecoveryState.VERIFIED: 95,
    FrmRecoveryState.DONE: 100,
    FrmRecoveryState.FAILED: 0,
}


# ─────────────────────────────────────────────────────────────────────
class FrmRecoveryOrchestrator:
    """Drives one FRM3 recovery session through a series of events.

    Each `handle(event, payload)` returns the next prompt the UI must
    render. Tests instantiate with a MockBdmTransport and drive
    handle() directly; production wires through api/views.py and
    persists between requests via WizardSession.
    """

    def __init__(self, bdm: AbstractBdmTransport,
                 *, data: Optional[FrmRecoveryData] = None,
                 state: FrmRecoveryState = FrmRecoveryState.IDLE) -> None:
        self.bdm = bdm
        self.data = data or FrmRecoveryData()
        self.state = state

    # ── State control ──────────────────────────────────────────
    def _advance(self, to: FrmRecoveryState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalFrmTransition(
                f"{self.state.value} → {to.value} not allowed",
            )
        log.info("frm_recovery transition", extra={
            "from": self.state.value, "to": to.value,
            "vin": self.data.vin,
            "variant": self.data.variant.value if self.data.variant else "",
        })
        self.state = to

    def _fail(self, code: str, detail: str) -> FrmRecoveryPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = FrmRecoveryState.FAILED
        log.warning("frm_recovery failure", extra={
            "code": code, "detail": detail, "vin": self.data.vin,
        })
        return FrmRecoveryPrompt(
            state=FrmRecoveryState.FAILED,
            title="فشل استرجاع FRM3",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة وفك الـ BDM بأمان.",
            progress_pct=0,
            payload={"error_code": code},
            is_terminal=True,
            is_error=True,
        )

    @property
    def profile(self) -> FrmProfile:
        if self.data.variant is None:
            raise RuntimeError("variant not selected yet")
        return get_frm_profile(self.data.variant)

    # ── Public entry point ─────────────────────────────────────
    async def handle(self, event: FrmRecoveryEvent | str,
                     payload: Optional[dict] = None) -> FrmRecoveryPrompt:
        """Drive one transition. Returns the next chatbot prompt. All
        recoverable errors are caught here so the caller never has to
        wrap in try/except for UX reasons."""
        if isinstance(event, str):
            try:
                event = FrmRecoveryEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")

        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalFrmTransition as e:
            return self._fail("illegal_transition", str(e))
        except BdmConnectionError as e:
            return self._fail("bdm_connection_error", str(e))
        except BdmReadError as e:
            return self._fail("bdm_read_error", str(e))
        except BdmWriteError as e:
            return self._fail("bdm_write_error", str(e))
        except CloudRebuildError as e:
            return self._fail("cloud_rebuild_error", str(e))
        except SalapaInjectionError as e:
            return self._fail("salapa_error", str(e))
        except Exception as e:                          # pragma: no cover
            log.exception("frm_recovery unexpected error")
            return self._fail("unexpected", repr(e))

    # ── Dispatch ───────────────────────────────────────────────
    async def _dispatch(self, event: FrmRecoveryEvent, payload: dict
                        ) -> FrmRecoveryPrompt:
        if event == FrmRecoveryEvent.ABORT:
            return self._fail("aborted_by_user",
                              "Session aborted by technician.")

        if event == FrmRecoveryEvent.SELECT_MODEL:
            return self._select_model(payload)
        if event == FrmRecoveryEvent.CONNECT_BDM:
            return await self._connect_bdm()
        if event == FrmRecoveryEvent.READ_DFLASH:
            return await self._read_dflash()
        if event == FrmRecoveryEvent.ANALYZE:
            return self._analyze()
        if event == FrmRecoveryEvent.REBUILD:
            return self._rebuild(payload)
        if event == FrmRecoveryEvent.FLASH_BACK:
            return await self._flash_back()
        if event == FrmRecoveryEvent.INJECT_VO_FA:
            return self._inject_vo_fa(payload)
        if event == FrmRecoveryEvent.VERIFY:
            return await self._verify()
        if event == FrmRecoveryEvent.FINISH:
            return self._finish()

        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. SELECT_MODEL ────────────────────────────────────────
    def _select_model(self, payload: dict) -> FrmRecoveryPrompt:
        variant_raw = (payload.get("variant") or "").strip()
        if not variant_raw:
            return self._fail("missing_variant",
                              "Required: variant ∈ {E90_FRM3, E70_FRM3, R56_FRM3}")
        try:
            variant = FrmVariant(variant_raw)
        except ValueError:
            return self._fail("unknown_variant", f"Unknown variant {variant_raw!r}")

        self.data.variant = variant
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()
        self._advance(FrmRecoveryState.MODEL_SELECTED)

        profile = self.profile
        return FrmRecoveryPrompt(
            state=self.state,
            title=f"تم اختيار {profile.label}",
            body=(
                f"اوصل الـ BDM POD على pads الـ FRM3. اضغط CONNECT_BDM "
                f"لما الجهاز يبقى متوصل تمام (BKGD + RESET + 12V + GND)."
            ),
            expects="CONNECT_BDM",
            progress_pct=_PROGRESS[self.state],
            payload={
                "variant": variant.value,
                "bdm_clock_khz": profile.bdm_clock_khz,
                "reset_low_ms": profile.reset_low_ms,
                "dflash_size": profile.dflash_size,
                "notes": list(profile.notes),
            },
        )

    # ── 2. CONNECT_BDM ─────────────────────────────────────────
    async def _connect_bdm(self) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.MODEL_SELECTED:
            raise IllegalFrmTransition(
                f"CONNECT_BDM only valid in MODEL_SELECTED (now {self.state.value})",
            )
        profile = self.profile
        await self.bdm.connect(
            bdm_clock_khz=profile.bdm_clock_khz,
            reset_low_ms=profile.reset_low_ms,
        )
        self._advance(FrmRecoveryState.BDM_CONNECTED)
        return FrmRecoveryPrompt(
            state=self.state,
            title="الـ FRM3 في BDM mode ✅",
            body=(
                f"الجهاز ردّ على BKGD ({profile.bdm_clock_khz} kHz). "
                f"دلوقتي اضغط READ_DFLASH لقراءة الـ {profile.dflash_size} "
                f"byte من الـ D-Flash."
            ),
            expects="READ_DFLASH",
            progress_pct=_PROGRESS[self.state],
            payload={"bdm_clock_khz": profile.bdm_clock_khz},
        )

    # ── 3. READ_DFLASH ─────────────────────────────────────────
    async def _read_dflash(self) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.BDM_CONNECTED:
            raise IllegalFrmTransition(
                f"READ_DFLASH only valid in BDM_CONNECTED (now {self.state.value})",
            )
        profile = self.profile
        raw = await self.bdm.read_dflash(
            address=profile.dflash_base, length=profile.dflash_size,
        )
        if len(raw) != profile.dflash_size:
            return self._fail(
                "short_read",
                f"BDM returned {len(raw)} bytes, expected {profile.dflash_size}",
            )
        self.data.raw_dump = bytes(raw)
        self._advance(FrmRecoveryState.DFLASH_READ)
        return FrmRecoveryPrompt(
            state=self.state,
            title="D-Flash dump كامل",
            body=(
                f"اتقرأ {len(raw)} byte. الـ backup محفوظ على السيرفر. "
                f"اضغط ANALYZE عشان نشوف حجم الـ corruption."
            ),
            expects="ANALYZE",
            progress_pct=_PROGRESS[self.state],
            payload={"dump_size": len(raw)},
        )

    # ── 4. ANALYZE ─────────────────────────────────────────────
    def _analyze(self) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.DFLASH_READ:
            raise IllegalFrmTransition(
                f"ANALYZE only valid in DFLASH_READ (now {self.state.value})",
            )
        profile = self.profile
        report = analyze_dflash(self.data.raw_dump, profile=profile)

        # Serialise the report for snapshot/restore.
        self.data.analyzer_report = {
            "level":             report.level.value,
            "vin_recoverable":   report.vin_recoverable,
            "fa_recoverable":    report.fa_recoverable,
            "uniform_runs":      report.uniform_runs,
            "checksum_ok":       report.checksum_ok,
            "confidence":        report.confidence,
            "notes":             list(report.notes),
        }

        if report.level == CorruptionLevel.UNREADABLE:
            return self._fail(
                "dump_unreadable",
                "الـ dump كله 0xFF/0x00 أو طوله غلط — إعادة قراءة مطلوبة. "
                + " | ".join(report.notes),
            )

        # HEALTHY or PARTIAL/SEVERE all advance; the orchestrator just
        # surfaces the analysis to the chatbot.
        self._advance(FrmRecoveryState.CORRUPTION_ANALYZED)
        return FrmRecoveryPrompt(
            state=self.state,
            title=f"تشخيص: {report.level.value.upper()}",
            body=(
                f"الـ confidence = {report.confidence:.0%}. الـ VIN "
                f"{'موجود' if report.vin_recoverable else 'مش موجود'} في الـ dump. "
                f"الـ FA "
                f"{'موجود' if report.fa_recoverable else 'تالف — هتيجي من template'}. "
                f"الـ checksum {'سليم' if report.checksum_ok else 'غلط'}. "
                f"اضغط REBUILD لما تـ confirm الـ VIN."
            ),
            expects="REBUILD",
            progress_pct=_PROGRESS[self.state],
            payload=self.data.analyzer_report,
        )

    # ── 5. REBUILD ─────────────────────────────────────────────
    def _rebuild(self, payload: dict) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.CORRUPTION_ANALYZED:
            raise IllegalFrmTransition(
                f"REBUILD only valid in CORRUPTION_ANALYZED (now {self.state.value})",
            )
        profile = self.profile
        # The technician confirms the VIN from the job sheet at this
        # point. We prefer that over what we extracted from the dump
        # (which might still have garbled bytes even after VIN analysis
        # said "ok").
        vin = (payload.get("vin") or self.data.vin or "").strip().upper()
        if not vin:
            return self._fail("missing_vin",
                              "Required: vin (17 chars, from job sheet).")

        fa_recoverable = bool((self.data.analyzer_report or {})
                              .get("fa_recoverable", False))

        result: CloudRebuildResult = rebuild_dflash(
            profile=profile,
            corrupted_dump=self.data.raw_dump,
            vin=vin,
            fa_recoverable=fa_recoverable,
        )
        self.data.vin = vin
        self.data.rebuilt_bytes = result.rebuilt_bytes
        self.data.rebuild_result = {
            "template_version": result.template_version,
            "vin_used": result.vin_used,
            "fa_carried_over": result.fa_carried_over,
            "notes": list(result.notes),
        }
        self._advance(FrmRecoveryState.CLOUD_REBUILT)
        return FrmRecoveryPrompt(
            state=self.state,
            title="الـ blob الجديد جاهز",
            body=(
                f"الـ rebuild اتعمل من template "
                f"{result.template_version}. الـ VIN: {vin}. الـ FA "
                f"{'انتقل من الـ dump' if result.fa_carried_over else 'هياخد defaults من الـ template'}. "
                f"اضغط FLASH_BACK لما تـ ready تكتب على الموديول."
            ),
            expects="FLASH_BACK",
            progress_pct=_PROGRESS[self.state],
            payload=self.data.rebuild_result,
        )

    # ── 6. FLASH_BACK ──────────────────────────────────────────
    async def _flash_back(self) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.CLOUD_REBUILT:
            raise IllegalFrmTransition(
                f"FLASH_BACK only valid in CLOUD_REBUILT (now {self.state.value})",
            )
        profile = self.profile
        await self.bdm.write_dflash(
            address=profile.dflash_base, data=self.data.rebuilt_bytes,
        )
        self._advance(FrmRecoveryState.DFLASH_FLASHED)
        return FrmRecoveryPrompt(
            state=self.state,
            title="تم الكتابة على الموديول ✅",
            body=(
                "كل الـ D-Flash اتكتب من جديد. الخطوة الجاية: حقن SALAPA "
                "codes عبر UDS. ابعت INJECT_VO_FA مع قائمة الـ SALAPA من "
                "job sheet العميل."
            ),
            expects="INJECT_VO_FA",
            progress_pct=_PROGRESS[self.state],
            payload={"flashed_bytes": len(self.data.rebuilt_bytes)},
        )

    # ── 7. INJECT_VO_FA ────────────────────────────────────────
    def _inject_vo_fa(self, payload: dict) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.DFLASH_FLASHED:
            raise IllegalFrmTransition(
                f"INJECT_VO_FA only valid in DFLASH_FLASHED (now {self.state.value})",
            )
        codes_raw = payload.get("fa_codes") or []
        if isinstance(codes_raw, str):
            # accept comma-separated string from chatbot UI for convenience
            codes_raw = [c.strip() for c in codes_raw.split(",")]
        profile = self.profile
        fa_payload: FaPayload = build_fa_payload(
            codes_raw,
            max_codes=80, max_bytes=profile.fa_length,
        )
        self.data.fa_codes = fa_payload.sorted_codes
        self.data.fa_payload_hex = fa_payload.raw_bytes.hex().upper()
        self._advance(FrmRecoveryState.VO_FA_INJECTED)
        return FrmRecoveryPrompt(
            state=self.state,
            title="SALAPA codes جاهزة للحقن",
            body=(
                f"عدد الـ codes: {len(fa_payload.codes)}. الـ payload "
                f"{len(fa_payload.raw_bytes)} byte. اضغط VERIFY عشان "
                f"نتأكد إن الـ FRM3 رد بنفس البيانات بعد الكتابة."
            ),
            expects="VERIFY",
            progress_pct=_PROGRESS[self.state],
            payload={
                "fa_codes":         list(fa_payload.sorted_codes),
                "fa_payload_hex":   self.data.fa_payload_hex,
                "fa_bytes":         len(fa_payload.raw_bytes),
            },
        )

    # ── 8. VERIFY ──────────────────────────────────────────────
    async def _verify(self) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.VO_FA_INJECTED:
            raise IllegalFrmTransition(
                f"VERIFY only valid in VO_FA_INJECTED (now {self.state.value})",
            )
        # Re-read the D-Flash window we just wrote and compare to the
        # rebuilt blob. Production may use a CRC instead; the mock
        # transport's memory is the same bytearray so a direct compare
        # is the right behaviour for tests.
        profile = self.profile
        readback = await self.bdm.read_dflash(
            address=profile.dflash_base, length=profile.dflash_size,
        )
        if readback != self.data.rebuilt_bytes:
            return self._fail(
                "verify_mismatch",
                "الـ read-back مش زي الـ rebuilt blob — تكرر FLASH_BACK.",
            )
        self._advance(FrmRecoveryState.VERIFIED)
        return FrmRecoveryPrompt(
            state=self.state,
            title="تم التحقق ✅",
            body=(
                "الـ FRM3 بيقرأ كل الـ bytes صح. اضغط FINISH عشان نقفل "
                "الـ BDM ونطلع report للجلسة."
            ),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload={},
        )

    # ── 9. FINISH ──────────────────────────────────────────────
    def _finish(self) -> FrmRecoveryPrompt:
        if self.state != FrmRecoveryState.VERIFIED:
            raise IllegalFrmTransition(
                f"FINISH only valid in VERIFIED (now {self.state.value})",
            )
        self._advance(FrmRecoveryState.DONE)
        return FrmRecoveryPrompt(
            state=self.state,
            title="انتهت العملية 🎉",
            body=(
                "اقطع الـ 12V، فك الـ BDM POD، ورجّع الـ FRM3 للسيارة. "
                "الـ session report موجود في الـ Cloud Sync."
            ),
            expects="",
            progress_pct=100,
            payload={
                "vin": self.data.vin,
                "fa_codes": list(self.data.fa_codes),
                "variant": self.data.variant.value if self.data.variant else "",
            },
            is_terminal=True,
        )

    # ── Snapshot / restore ─────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "variant": self.data.variant.value if self.data.variant else None,
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "raw_dump_hex": self.data.raw_dump.hex(),
                "analyzer_report": self.data.analyzer_report,
                "rebuild_result": self.data.rebuild_result,
                "rebuilt_bytes_hex": self.data.rebuilt_bytes.hex(),
                "fa_codes": list(self.data.fa_codes),
                "fa_payload_hex": self.data.fa_payload_hex,
                "notes": list(self.data.notes),
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, bdm: AbstractBdmTransport,
                snapshot: dict[str, Any]) -> "FrmRecoveryOrchestrator":
        s = snapshot["data"]
        variant_raw = s.get("variant")
        data = FrmRecoveryData(
            variant=FrmVariant(variant_raw) if variant_raw else None,
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            raw_dump=bytes.fromhex(s.get("raw_dump_hex") or ""),
            analyzer_report=s.get("analyzer_report"),
            rebuild_result=s.get("rebuild_result"),
            rebuilt_bytes=bytes.fromhex(s.get("rebuilt_bytes_hex") or ""),
            fa_codes=tuple(s.get("fa_codes") or ()),
            fa_payload_hex=s.get("fa_payload_hex", ""),
            notes=list(s.get("notes") or []),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(bdm, data=data, state=FrmRecoveryState(snapshot["state"]))
