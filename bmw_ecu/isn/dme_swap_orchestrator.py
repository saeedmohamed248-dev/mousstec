"""Used-DME swap orchestrator — CAS↔DME ISN alignment.

The exact workshop case this models
-----------------------------------
A technician fits a **second-hand DME** (e.g. MINI R56 N18 / Bosch MEVD17.2)
into a car. The donor DME still carries the *donor car's* ISN, so the engine
won't crank — the immobilizer (CAS3 / CAS3+) and key won't pair with it.

To make the car start you must align the ISN so the used DME accepts THIS
car's CAS + key:

    1. READ_CAS_ISN  → read the genuine ISN from the car's CAS (over OBD).
    2. BACKUP_DME     → full backup of the used DME BEFORE any write.
    3. WRITE_DME_ISN  → write the car's ISN into the used DME.
    4. VERIFY         → read the ISN back from the DME; must match.
    5. ALIGN          → EWS align DME↔CAS over OBD; engine may now crank.

Honesty rules baked in (project policy: never fake, never guess)
----------------------------------------------------------------
• This orchestrator NEVER fabricates an ISN. It only ever carries the bytes a
  provider returns from real hardware (or, in tests, the Mock provider).
• A virgin ISN (all 0x00 / all 0xFF) is refused — it means the read failed.
• WRITE is blocked until a backup exists (state order enforces this).
• On MEVD17 the ISN is NOT writable over UDS (see isn_map: over_uds=False), so
  `requires_bench=True` profiles surface a bench instruction and route the
  write through a bench provider. We do not pretend an OBD write succeeded.
• The actual ISN crypto / bench read-write happens inside the provider, which
  in production composes IsnExtractor / IsnInjector / EwsSync (and ultimately
  the licensed tool) — this state machine is transport/tool-agnostic.

The prompt + event shape mirrors key_learning.bench_orchestrator so the same
chatbot UI renders it with no frontend changes.
"""
from __future__ import annotations

import abc
import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

ISN_LENGTH = 32


# ─────────────────────────────────────────────────────────────────────
# Provider abstraction — the only place that touches real hardware/tools.
# ─────────────────────────────────────────────────────────────────────
class AbstractDmeSwapProvider(abc.ABC):
    """Hardware/tool seam. Production composes the real UDS + bench + EWS
    primitives behind this; tests use MockDmeSwapProvider.
    """

    @abc.abstractmethod
    async def read_cas_isn(self, *, vin: str, cas_family: str) -> bytes:
        """Return THIS car's 32-byte ISN read from its CAS (over OBD)."""

    @abc.abstractmethod
    async def backup_dme(self, *, vin: str, dme_name: str) -> str:
        """Full-read the used DME and persist it. Return a backup reference
        (e.g. sha256) so a bad write can be rolled back."""

    @abc.abstractmethod
    async def write_dme_isn(self, *, vin: str, dme_name: str, isn: bytes,
                            requires_bench: bool) -> None:
        """Write the car's ISN into the used DME (UDS or bench/BDM)."""

    @abc.abstractmethod
    async def verify_dme_isn(self, *, vin: str, dme_name: str,
                             isn: bytes) -> bool:
        """Read the ISN back from the DME and compare to `isn`."""

    @abc.abstractmethod
    async def align_ews(self, *, vin: str) -> None:
        """EWS align DME↔CAS over OBD (RoutineControl)."""

    async def bsl_write_dme_isn(self, *, vin: str, dme_name: str, dme_family: str,
                                isn: bytes) -> None:
        """Phase 2: write the ISN over the chip's Bootstrap Loader (no external
        programmer). Default providers don't support BSL; override to enable.

        Raises (orchestrator-handled, never a hard crash):
          • BslHandshakeFailed  — physical boot setup wrong; tech fixes + retries
          • BslNotConfigured    — boot link OK but confirmed flash profile missing
        """
        from .tricore_bsl import BslNotConfigured
        raise BslNotConfigured(
            f"{type(self).__name__} has no BSL fallback wired for {dme_family}.")


class MockDmeSwapProvider(AbstractDmeSwapProvider):
    """Deterministic in-memory provider for tests and the clickable demo.

    NOT a real ECU. It returns a fixed, clearly-synthetic ISN and records
    every call so tests can assert ordering. Failure flags let tests drive
    the FAILED branch without real hardware.
    """

    # A clearly-synthetic but valid (non-virgin) 32-byte ISN.
    DEMO_ISN = bytes(range(1, ISN_LENGTH + 1))

    def __init__(self, *, isn: Optional[bytes] = None,
                 fail_read: bool = False, fail_backup: bool = False,
                 fail_write: bool = False, corrupt_verify: bool = False,
                 fail_align: bool = False,
                 uds_reject_nrc: Optional[int] = None,
                 bsl_handshake_fail: bool = False,
                 bsl_not_configured: bool = False) -> None:
        self._isn = isn or self.DEMO_ISN
        self.fail_read = fail_read
        self.fail_backup = fail_backup
        self.fail_write = fail_write
        self.corrupt_verify = corrupt_verify
        self.fail_align = fail_align
        # Fallback simulation: make the Phase-1 UDS write report an NRC so the
        # orchestrator diverts to the BSL wizard; then drive the Phase-2 result.
        self.uds_reject_nrc = uds_reject_nrc
        self.bsl_handshake_fail = bsl_handshake_fail
        self.bsl_not_configured = bsl_not_configured
        self.calls: list[str] = []
        self._written: Optional[bytes] = None

    async def read_cas_isn(self, *, vin: str, cas_family: str) -> bytes:
        self.calls.append("read_cas_isn")
        if self.fail_read:
            return bytes(ISN_LENGTH)  # virgin → orchestrator must refuse
        return self._isn

    async def backup_dme(self, *, vin: str, dme_name: str) -> str:
        self.calls.append("backup_dme")
        if self.fail_backup:
            raise RuntimeError("DME full-read failed (no backup)")
        return "sha256:" + self._isn.hex()[:16]

    async def write_dme_isn(self, *, vin: str, dme_name: str, isn: bytes,
                            requires_bench: bool) -> None:
        self.calls.append("write_dme_isn")
        if self.uds_reject_nrc is not None:
            # Phase-1 UDS attempt rejected → orchestrator diverts to BSL.
            raise DmeUdsWriteRejected(nrc=self.uds_reject_nrc)
        if self.fail_write:
            raise RuntimeError("ISN write rejected by DME")
        self._written = bytes(isn)

    async def bsl_write_dme_isn(self, *, vin: str, dme_name: str,
                                dme_family: str, isn: bytes) -> None:
        self.calls.append("bsl_write_dme_isn")
        from .tricore_bsl import BslHandshakeFailed, BslNotConfigured
        if self.bsl_handshake_fail:
            raise BslHandshakeFailed("mock: no boot handshake")
        if self.bsl_not_configured:
            raise BslNotConfigured("mock: no confirmed flash profile")
        self._written = bytes(isn)

    async def verify_dme_isn(self, *, vin: str, dme_name: str,
                             isn: bytes) -> bool:
        self.calls.append("verify_dme_isn")
        # Deterministic simulator: a fresh provider is built on every stateless
        # HTTP request, so we must NOT depend on in-process `_written` (that
        # would spuriously fail read-back across requests). The orchestrator
        # only ever verifies the ISN it just wrote; the real provider does the
        # actual read-back compare.
        return not self.corrupt_verify

    async def align_ews(self, *, vin: str) -> None:
        self.calls.append("align_ews")
        if self.fail_align:
            raise RuntimeError("EWS align timed out")


# ─────────────────────────────────────────────────────────────────────
# Profile — minimal, declarative. No unverified DIDs baked here; the
# provider/isn_map own the per-family access spec.
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DmeSwapProfile:
    chassis: str               # e.g. "R56"
    dme_name: str              # e.g. "MEVD17_2_2"
    dme_family: str            # e.g. "MEVD17"
    cas_family: str            # "CAS3" | "CAS3+"
    requires_bench: bool       # MEVD17 used-DME ISN write is bench-only
    label_ar: str
    label_en: str


# A small starter set. R56 N18 is the user's case. These describe the
# WORKFLOW only (which tool path) — never guessed pins or DIDs.
DME_SWAP_PROFILES: dict[str, DmeSwapProfile] = {
    "R56_N18_MEVD17": DmeSwapProfile(
        chassis="R56", dme_name="MEVD17_2_2", dme_family="MEVD17",
        cas_family="CAS3+", requires_bench=True,
        label_ar="ميني R56 N18 — DME MEVD17.2 (كتابة ISN على البنش)",
        label_en="MINI R56 N18 — DME MEVD17.2 (bench ISN write)",
    ),
}


def get_dme_swap_profile(key: str) -> Optional[DmeSwapProfile]:
    return DME_SWAP_PROFILES.get(key)


# ─────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────
class SwapState(str, enum.Enum):
    IDLE             = "idle"
    PROFILE_SELECTED = "profile_selected"
    CAS_ISN_READ     = "cas_isn_read"
    DME_BACKED_UP    = "dme_backed_up"
    DME_BSL_FALLBACK = "dme_bsl_fallback"   # UDS write rejected → guided BSL setup
    DME_ISN_WRITTEN  = "dme_isn_written"
    VERIFIED         = "verified"
    ALIGNED          = "aligned"
    DONE             = "done"
    FAILED           = "failed"


_ALLOWED: dict[SwapState, set[SwapState]] = {
    SwapState.IDLE:             {SwapState.PROFILE_SELECTED, SwapState.FAILED},
    SwapState.PROFILE_SELECTED: {SwapState.CAS_ISN_READ,    SwapState.FAILED},
    SwapState.CAS_ISN_READ:     {SwapState.DME_BACKED_UP,   SwapState.FAILED},
    # After backup the write is attempted over UDS first; an NRC rejection
    # diverts to the BSL fallback wizard instead of failing the whole job.
    SwapState.DME_BACKED_UP:    {SwapState.DME_ISN_WRITTEN,
                                 SwapState.DME_BSL_FALLBACK, SwapState.FAILED},
    # The BSL wizard is a paused, re-enterable state: the tech can fire BSL_START
    # repeatedly (fix wiring / register the confirmed profile) until the write
    # lands, without ever leaving the job in a crashed state.
    SwapState.DME_BSL_FALLBACK: {SwapState.DME_BSL_FALLBACK,
                                 SwapState.DME_ISN_WRITTEN, SwapState.FAILED},
    SwapState.DME_ISN_WRITTEN:  {SwapState.VERIFIED,        SwapState.FAILED},
    SwapState.VERIFIED:         {SwapState.ALIGNED,         SwapState.FAILED},
    SwapState.ALIGNED:          {SwapState.DONE,            SwapState.FAILED},
    SwapState.DONE:             set(),
    SwapState.FAILED:           set(),
}


class IllegalSwapTransition(Exception):
    pass


class DmeUdsWriteRejected(Exception):
    """Phase-1 UDS ISN write was refused by the DME. Carries the NRC (if the
    DME answered one) so the orchestrator can divert to the BSL fallback
    instead of treating it as a hard failure. `nrc` is None when there simply
    is no usable UDS write path (so BSL is the only option)."""

    def __init__(self, nrc: Optional[int] = None, reason: str = "") -> None:
        super().__init__(reason or (f"UDS ISN write rejected (NRC 0x{nrc:02X})"
                                    if nrc is not None else "No UDS ISN write path"))
        self.nrc = nrc
        self.reason = reason


class SwapEvent(str, enum.Enum):
    SELECT_PROFILE = "select_profile"
    READ_CAS_ISN   = "read_cas_isn"
    BACKUP_DME     = "backup_dme"
    WRITE_DME_ISN  = "write_dme_isn"
    BSL_START      = "bsl_start"      # "Start BSL Extraction" button (Phase 2)
    VERIFY         = "verify"
    ALIGN          = "align"
    FINISH         = "finish"
    ABORT          = "abort"


_PROGRESS = {
    SwapState.IDLE: 0,
    SwapState.PROFILE_SELECTED: 12,
    SwapState.CAS_ISN_READ: 30,
    SwapState.DME_BACKED_UP: 50,
    SwapState.DME_BSL_FALLBACK: 60,
    SwapState.DME_ISN_WRITTEN: 70,
    SwapState.VERIFIED: 85,
    SwapState.ALIGNED: 95,
    SwapState.DONE: 100,
    SwapState.FAILED: 0,
}


@dataclass
class SwapPrompt:
    state: SwapState
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


@dataclass
class SwapData:
    profile_key: str = ""
    vin: str = ""
    technician_id: str = ""
    gateway: str = ""            # "" direct OBD, or "ZGW" bench-rig gateway
    cas_isn_hex: str = ""
    backup_ref: str = ""
    uds_reject_nrc: str = ""      # hex NRC that triggered the BSL fallback
    error_code: str = ""
    error_detail: str = ""


def _is_virgin(isn: bytes) -> bool:
    return all(b == 0 for b in isn) or all(b == 0xFF for b in isn)


# ─────────────────────────────────────────────────────────────────────
class DmeSwapOrchestrator:
    """Forward-only, asyncio-driven, serialisable orchestrator."""

    def __init__(self, provider: AbstractDmeSwapProvider, *,
                 data: Optional[SwapData] = None,
                 state: SwapState = SwapState.IDLE) -> None:
        self.provider = provider
        self.data = data or SwapData()
        self.state = state

    # ── state control ─────────────────────────────────────────
    def _advance(self, to: SwapState) -> None:
        if to not in _ALLOWED[self.state]:
            raise IllegalSwapTransition(f"{self.state.value} → {to.value} not allowed")
        log.info("dme-swap transition", extra={
            "from": self.state.value, "to": to.value, "vin": self.data.vin})
        self.state = to

    def _fail(self, code: str, detail: str) -> SwapPrompt:
        self.data.error_code = code
        self.data.error_detail = detail
        self.state = SwapState.FAILED
        log.warning("dme-swap failure", extra={"code": code, "detail": detail,
                                                "vin": self.data.vin})
        return SwapPrompt(
            state=SwapState.FAILED,
            title="فشلت العملية — DME swap aborted",
            body=detail,
            expects="ابعت ABORT لإغلاق الجلسة بأمان. النسخة الاحتياطية للـ DME محفوظة لو وصلنا لها.",
            progress_pct=0,
            payload={"error_code": code, "backup_ref": self.data.backup_ref},
            is_terminal=True,
            is_error=True,
        )

    @property
    def profile(self) -> DmeSwapProfile:
        prof = get_dme_swap_profile(self.data.profile_key)
        if prof is None:
            raise RuntimeError("profile not selected yet")
        return prof

    # ── public entry point ────────────────────────────────────
    async def handle(self, event: SwapEvent | str,
                     payload: Optional[dict] = None) -> SwapPrompt:
        if isinstance(event, str):
            try:
                event = SwapEvent(event)
            except ValueError:
                return self._fail("unknown_event", f"Unknown event {event!r}")
        payload = payload or {}
        try:
            return await self._dispatch(event, payload)
        except IllegalSwapTransition as e:
            return self._fail("illegal_transition", str(e))
        except Exception as e:  # noqa: BLE001 — any provider error → honest FAILED
            log.exception("dme-swap unexpected error")
            return self._fail("provider_error", repr(e))

    async def _dispatch(self, event: SwapEvent, payload: dict) -> SwapPrompt:
        if event == SwapEvent.ABORT:
            return self._fail("aborted_by_user", "Session aborted by technician.")
        if event == SwapEvent.SELECT_PROFILE:
            return self._select_profile(payload)
        if event == SwapEvent.READ_CAS_ISN:
            return await self._read_cas_isn()
        if event == SwapEvent.BACKUP_DME:
            return await self._backup_dme()
        if event == SwapEvent.WRITE_DME_ISN:
            return await self._write_dme_isn()
        if event == SwapEvent.BSL_START:
            return await self._bsl_start()
        if event == SwapEvent.VERIFY:
            return await self._verify()
        if event == SwapEvent.ALIGN:
            return await self._align()
        if event == SwapEvent.FINISH:
            return self._finish()
        return self._fail("unhandled_event", f"event {event!r} not handled")

    # ── 1. SELECT_PROFILE ─────────────────────────────────────
    def _select_profile(self, payload: dict) -> SwapPrompt:
        key = (payload.get("profile_key") or "").strip()
        if not key:
            return self._fail("missing_profile",
                              f"Required: profile_key ∈ {sorted(DME_SWAP_PROFILES)}")
        if get_dme_swap_profile(key) is None:
            return self._fail("unknown_profile", f"Unknown profile_key {key!r}")
        self.data.profile_key = key
        self.data.vin = (payload.get("vin") or "").strip().upper()
        self.data.technician_id = (payload.get("technician_id") or "").strip()
        # Bench-rig path: a "ZGW" central gateway bridges the K+DCAN OBD link to
        # PT-CAN and routes standard E-series frames to the DME + CAS. Declared
        # by the caller; the real provider holds the actual ZGW/DME/CAS routing.
        self.data.gateway = (payload.get("gateway") or "").strip().upper()
        self._advance(SwapState.PROFILE_SELECTED)
        p = self.profile
        via = (f" عن طريق جيتواي {self.data.gateway} (بنش ريج → PT-CAN)"
               if self.data.gateway else " على الفيشة OBD")
        return SwapPrompt(
            state=self.state,
            title=f"تم اختيار {p.label_ar}",
            body=(
                f"العربية: {p.chassis} / المناعة: {p.cas_family}. الكنترول المستعمل "
                f"محتاج ISN العربية دي. اضغط READ_CAS_ISN عشان نقرا الـ ISN الأصلي من "
                f"الـ CAS بتاع العربية{via}."
            ),
            expects="READ_CAS_ISN",
            progress_pct=_PROGRESS[self.state],
            payload={
                "chassis": p.chassis, "cas_family": p.cas_family,
                "dme_name": p.dme_name, "requires_bench": p.requires_bench,
                "gateway": self.data.gateway,
            },
        )

    # ── 2. READ_CAS_ISN ───────────────────────────────────────
    async def _read_cas_isn(self) -> SwapPrompt:
        if self.state != SwapState.PROFILE_SELECTED:
            raise IllegalSwapTransition(
                f"READ_CAS_ISN only valid in PROFILE_SELECTED (now {self.state.value})")
        p = self.profile
        isn = await self.provider.read_cas_isn(vin=self.data.vin, cas_family=p.cas_family)
        if len(isn) != ISN_LENGTH:
            return self._fail("bad_isn_length",
                              f"CAS ISN must be {ISN_LENGTH} bytes, got {len(isn)}")
        if _is_virgin(isn):
            return self._fail("virgin_isn",
                              "الـ ISN اللي رجع من الـ CAS كله أصفار/FF — القراءة فشلت. "
                              "اتأكد من توصيل OBD والكونتاكت ON وأعد المحاولة.")
        self.data.cas_isn_hex = isn.hex().upper()
        self._advance(SwapState.CAS_ISN_READ)
        bench_note = (
            "الكنترول ده كتابة الـ ISN بتاعته على **البنش** (مش OBD). جهّز فيشة البنش/سلوك "
            "الـ boot على الـ DME. " if p.requires_bench else "")
        return SwapPrompt(
            state=self.state,
            title="اتقرا ISN العربية من الـ CAS 🔑",
            body=(
                f"الـ ISN الأصلي للعربية: {self.data.cas_isn_hex[:8]}…{self.data.cas_isn_hex[-8:]}. "
                f"{bench_note}قبل أي كتابة، اضغط BACKUP_DME عشان ناخد نسخة كاملة من الكنترول المستعمل."
            ),
            expects="BACKUP_DME",
            progress_pct=_PROGRESS[self.state],
            payload={"isn_prefix": self.data.cas_isn_hex[:8],
                     "requires_bench": p.requires_bench},
        )

    # ── 3. BACKUP_DME ─────────────────────────────────────────
    async def _backup_dme(self) -> SwapPrompt:
        if self.state != SwapState.CAS_ISN_READ:
            raise IllegalSwapTransition(
                f"BACKUP_DME only valid in CAS_ISN_READ (now {self.state.value})")
        p = self.profile
        ref = await self.provider.backup_dme(vin=self.data.vin, dme_name=p.dme_name)
        if not ref:
            return self._fail("backup_empty", "Backup returned no reference — refusing to write.")
        self.data.backup_ref = ref
        self._advance(SwapState.DME_BACKED_UP)
        return SwapPrompt(
            state=self.state,
            title="نسخة احتياطية كاملة للـ DME ✅",
            body=(
                f"اتحفظت نسخة من الكنترول المستعمل ({ref}). دلوقتي اضغط WRITE_DME_ISN "
                f"عشان نكتب ISN العربية جوه الكنترول."
            ),
            expects="WRITE_DME_ISN",
            progress_pct=_PROGRESS[self.state],
            payload={"backup_ref": ref},
        )

    # ── 4. WRITE_DME_ISN — Phase 1 (UDS), auto-fallback to BSL on NRC ──
    async def _write_dme_isn(self) -> SwapPrompt:
        if self.state != SwapState.DME_BACKED_UP:
            raise IllegalSwapTransition(
                f"WRITE_DME_ISN only valid in DME_BACKED_UP (now {self.state.value})")
        if not self.data.backup_ref:
            return self._fail("no_backup", "Refusing to write ISN without a backup.")
        p = self.profile
        isn = bytes.fromhex(self.data.cas_isn_hex)
        try:
            # Phase 1: attempt the write over UDS (programming session).
            await self.provider.write_dme_isn(
                vin=self.data.vin, dme_name=p.dme_name, isn=isn,
                requires_bench=p.requires_bench)
        except DmeUdsWriteRejected as e:
            # The DME refused the UDS write (e.g. NRC 0x33 Security Access
            # Denied / 0x22 Conditions Not Correct). Do NOT crash or fail the
            # job — divert continuously to the guided BSL fallback wizard.
            self.data.uds_reject_nrc = (f"0x{e.nrc:02X}" if e.nrc is not None else "")
            self._advance(SwapState.DME_BSL_FALLBACK)
            return self._bsl_wizard_prompt(p, e)
        self._advance(SwapState.DME_ISN_WRITTEN)
        return SwapPrompt(
            state=self.state,
            title="اتكتب الـ ISN في الكنترول ✍️ (UDS)",
            body=(
                "تمت كتابة ISN العربية في الـ DME المستعمل عن طريق UDS. اضغط VERIFY "
                "عشان نقرا الـ ISN تاني من الكنترول ونتأكد إنه مطابق."
            ),
            expects="VERIFY",
            progress_pct=_PROGRESS[self.state],
            payload={"requires_bench": p.requires_bench, "path": "uds"},
        )

    # ── 4b. BSL fallback wizard (paused, interactive, no external device) ──
    def _bsl_wizard_prompt(self, p: DmeSwapProfile,
                           e: DmeUdsWriteRejected) -> SwapPrompt:
        """Surface the step-by-step Tricore BSL setup. Hardware specifics come
        from the confirmed BslHardwareProfile (NOT guessed here); unconfirmed
        values are shown with a clear warning the tech must verify."""
        from .tricore_bsl import get_bsl_profile
        hw = get_bsl_profile(p.dme_family)
        nrc_note = (f" (الـ DME رفض الكتابة عبر UDS برد NRC {self.data.uds_reject_nrc})"
                    if self.data.uds_reject_nrc else
                    " (مفيش مسار كتابة UDS متاح للكنترول ده)")
        warn = ("" if (hw and hw.verified) else
                "\n\n⚠️ القيم التحت دي **مبدئية** لحد ما تتأكد من توثيق البورده. غلط في "
                "تحديد الـ boot pin = كنترول مضروب. أكّد المكان قبل ما تعمل bridge.")
        chip = hw.chip if hw else "Infineon TriCore (confirm)"
        boot = hw.boot_pin_label if hw else "boot-config pin (confirm)"
        pull = hw.pull if hw else "1kΩ to GND (confirm)"
        pins = hw.serial_pin_map if hw else "FTDI TX/RX/GND → board (confirm)"
        volts = hw.bench_voltage if hw else "12V"
        steps = [
            "1) افصل الكهرباء تماماً وافتح علبة الـ DME (MEVD17) بأمان — اشتغل على "
            "ESD mat والكنترول مفصول.",
            f"2) اعمل bridge لـ {boot} للأرضي عن طريق مقاومة {pull} على شريحة {chip}.",
            f"3) وصّل سيريال الـ FTDI على بوردة الـ DME: {pins}، وبعدين نوّر "
            f"الكنترول بـ {volts} على البنش.",
            "4) اضغط «Start BSL Extraction» — النظام هيعمل fast-init 25ms ويتأكد "
            "من رد المعالج (0x55 handshake) قبل أي كتابة.",
        ]
        return SwapPrompt(
            state=SwapState.DME_BSL_FALLBACK,
            title="الكتابة عبر UDS مرفوضة — تحوّلنا لوضع البوت (BSL) 🔌",
            body=(
                f"الكنترول المستعمل مرفض كتابة الـ ISN عبر UDS{nrc_note}. مفيش مشكلة — "
                "هنكتبها عبر الـ Bootstrap Loader بتاع المعالج بنفس كابل الـ FTDI، من "
                f"غير أي جهاز خارجي. اتبع الخطوات بالترتيب:{warn}\n\n"
                + "\n".join(steps)
            ),
            expects="BSL_START",
            progress_pct=_PROGRESS[SwapState.DME_BSL_FALLBACK],
            payload={
                "path": "bsl",
                "uds_reject_nrc": self.data.uds_reject_nrc,
                "dme_family": p.dme_family,
                "hardware_verified": bool(hw and hw.verified),
                "steps": steps,
                "hardware": {
                    "chip": chip, "boot_pin": boot, "pull": pull,
                    "serial_pin_map": pins, "bench_voltage": volts,
                },
            },
            is_terminal=False,
        )

    # ── 4c. BSL_START — fire the Tricore BSL stack (Phase 2) ──────────────
    async def _bsl_start(self) -> SwapPrompt:
        if self.state != SwapState.DME_BSL_FALLBACK:
            raise IllegalSwapTransition(
                f"BSL_START only valid in DME_BSL_FALLBACK (now {self.state.value})")
        from .tricore_bsl import BslHandshakeFailed, BslNotConfigured
        p = self.profile
        isn = bytes.fromhex(self.data.cas_isn_hex)
        try:
            await self.provider.bsl_write_dme_isn(
                vin=self.data.vin, dme_name=p.dme_name,
                dme_family=p.dme_family, isn=isn)
        except BslHandshakeFailed as e:
            # Physical setup wrong — stay in the wizard, ask to fix wiring/retry.
            return SwapPrompt(
                state=SwapState.DME_BSL_FALLBACK,
                title="المعالج مردّش على الـ handshake ✋",
                body=(
                    f"{e}\n\nراجع: الـ boot pin متوصّل صح للأرضي؟ سيريال الـ FTDI "
                    "(TX/RX) مظبوط؟ في 12V على البنش؟ صلّح ودوس «Start BSL "
                    "Extraction» تاني."
                ),
                expects="BSL_START",
                progress_pct=_PROGRESS[SwapState.DME_BSL_FALLBACK],
                payload={"path": "bsl", "retry": True},
                is_error=True,
            )
        except BslNotConfigured as e:
            # Boot link is up but the confirmed ISN flash profile is missing. We
            # refuse to write a guessed offset — stay paused, ask for the data.
            return SwapPrompt(
                state=SwapState.DME_BSL_FALLBACK,
                title="البوت تمام — ناقص بيانات الـ ISN المؤكدة 📋",
                body=(
                    f"{e}\n\nاتصل الـ handshake بنجاح، بس محتاجين الـ ISN offset "
                    "والـ BSL flash command المؤكدين للبورده دي (يتسجلوا من الأدمن/"
                    "الكاتالوج). سجّلهم ودوس «Start BSL Extraction» تاني — مش "
                    "هنكتب على offset مخمّن."
                ),
                expects="BSL_START",
                progress_pct=_PROGRESS[SwapState.DME_BSL_FALLBACK],
                payload={"path": "bsl", "needs_confirmed_flash_profile": True},
                is_error=False,
            )
        # BSL write succeeded.
        self._advance(SwapState.DME_ISN_WRITTEN)
        return SwapPrompt(
            state=self.state,
            title="اتكتب الـ ISN عبر البوت (BSL) ✍️",
            body=(
                "تمت كتابة ISN العربية في الـ DME عن طريق الـ Bootstrap Loader. "
                "فك توصيلات البنش ورجّع الكنترول، وبعدين اضغط VERIFY للتأكد."
            ),
            expects="VERIFY",
            progress_pct=_PROGRESS[self.state],
            payload={"requires_bench": p.requires_bench, "path": "bsl"},
        )

    # ── 5. VERIFY ─────────────────────────────────────────────
    async def _verify(self) -> SwapPrompt:
        if self.state != SwapState.DME_ISN_WRITTEN:
            raise IllegalSwapTransition(
                f"VERIFY only valid in DME_ISN_WRITTEN (now {self.state.value})")
        p = self.profile
        isn = bytes.fromhex(self.data.cas_isn_hex)
        ok = await self.provider.verify_dme_isn(
            vin=self.data.vin, dme_name=p.dme_name, isn=isn)
        if not ok:
            return self._fail(
                "verify_mismatch",
                "القراءة بعد الكتابة مش مطابقة للـ ISN المطلوب. الكتابة فشلت — "
                "استخدم النسخة الاحتياطية لاسترجاع الكنترول وأعد المحاولة.")
        self._advance(SwapState.VERIFIED)
        return SwapPrompt(
            state=self.state,
            title="تم التحقق من الـ ISN ✅",
            body=(
                "الكنترول دلوقتي شايل ISN العربية الصح. ركّب الـ DME في العربية، "
                "وبعدين اضغط ALIGN عشان نعمل EWS align بين الكنترول والـ CAS على الفيشة."
            ),
            expects="ALIGN",
            progress_pct=_PROGRESS[self.state],
            payload={},
        )

    # ── 6. ALIGN ──────────────────────────────────────────────
    async def _align(self) -> SwapPrompt:
        if self.state != SwapState.VERIFIED:
            raise IllegalSwapTransition(
                f"ALIGN only valid in VERIFIED (now {self.state.value})")
        await self.provider.align_ews(vin=self.data.vin)
        self._advance(SwapState.ALIGNED)
        return SwapPrompt(
            state=self.state,
            title="تمت مزامنة EWS ✅",
            body=(
                "الكنترول والـ CAS اتزامنوا. جرّب تدوّر العربية بالمفتاح. "
                "لو دوّرت، اضغط FINISH لإنهاء الجلسة."
            ),
            expects="FINISH",
            progress_pct=_PROGRESS[self.state],
            payload={},
        )

    # ── 7. FINISH ─────────────────────────────────────────────
    def _finish(self) -> SwapPrompt:
        if self.state != SwapState.ALIGNED:
            raise IllegalSwapTransition(
                f"FINISH only valid in ALIGNED (now {self.state.value})")
        self._advance(SwapState.DONE)
        return SwapPrompt(
            state=self.state,
            title="انتهت العملية 🎉",
            body=(
                "تم تركيب وتكويد الكنترول المستعمل على العربية والمفتاح. كل خطوة محفوظة "
                "في السجل، والنسخة الاحتياطية للكنترول متخزّنة للرجوع إليها."
            ),
            expects="",
            progress_pct=100,
            payload={"backup_ref": self.data.backup_ref,
                     "cas_isn_prefix": self.data.cas_isn_hex[:8]},
            is_terminal=True,
        )

    # ── serialisation (WizardSession persistence) ─────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "data": {
                "profile_key": self.data.profile_key,
                "vin": self.data.vin,
                "technician_id": self.data.technician_id,
                "gateway": self.data.gateway,
                "cas_isn_hex": self.data.cas_isn_hex,
                "backup_ref": self.data.backup_ref,
                "uds_reject_nrc": self.data.uds_reject_nrc,
                "error_code": self.data.error_code,
                "error_detail": self.data.error_detail,
            },
        }

    @classmethod
    def restore(cls, provider: AbstractDmeSwapProvider,
                snapshot: dict[str, Any]) -> "DmeSwapOrchestrator":
        s = snapshot["data"]
        data = SwapData(
            profile_key=s.get("profile_key", ""),
            vin=s.get("vin", ""),
            technician_id=s.get("technician_id", ""),
            gateway=s.get("gateway", ""),
            cas_isn_hex=s.get("cas_isn_hex", ""),
            backup_ref=s.get("backup_ref", ""),
            uds_reject_nrc=s.get("uds_reject_nrc", ""),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(provider, data=data, state=SwapState(snapshot["state"]))
