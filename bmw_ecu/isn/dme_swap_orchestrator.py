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
                 fail_align: bool = False) -> None:
        self._isn = isn or self.DEMO_ISN
        self.fail_read = fail_read
        self.fail_backup = fail_backup
        self.fail_write = fail_write
        self.corrupt_verify = corrupt_verify
        self.fail_align = fail_align
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
        if self.fail_write:
            raise RuntimeError("ISN write rejected by DME")
        self._written = bytes(isn)

    async def verify_dme_isn(self, *, vin: str, dme_name: str,
                             isn: bytes) -> bool:
        self.calls.append("verify_dme_isn")
        if self.corrupt_verify:
            return False
        return self._written == bytes(isn)

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
    DME_ISN_WRITTEN  = "dme_isn_written"
    VERIFIED         = "verified"
    ALIGNED          = "aligned"
    DONE             = "done"
    FAILED           = "failed"


_ALLOWED: dict[SwapState, set[SwapState]] = {
    SwapState.IDLE:             {SwapState.PROFILE_SELECTED, SwapState.FAILED},
    SwapState.PROFILE_SELECTED: {SwapState.CAS_ISN_READ,    SwapState.FAILED},
    SwapState.CAS_ISN_READ:     {SwapState.DME_BACKED_UP,   SwapState.FAILED},
    SwapState.DME_BACKED_UP:    {SwapState.DME_ISN_WRITTEN, SwapState.FAILED},
    SwapState.DME_ISN_WRITTEN:  {SwapState.VERIFIED,        SwapState.FAILED},
    SwapState.VERIFIED:         {SwapState.ALIGNED,         SwapState.FAILED},
    SwapState.ALIGNED:          {SwapState.DONE,            SwapState.FAILED},
    SwapState.DONE:             set(),
    SwapState.FAILED:           set(),
}


class IllegalSwapTransition(Exception):
    pass


class SwapEvent(str, enum.Enum):
    SELECT_PROFILE = "select_profile"
    READ_CAS_ISN   = "read_cas_isn"
    BACKUP_DME     = "backup_dme"
    WRITE_DME_ISN  = "write_dme_isn"
    VERIFY         = "verify"
    ALIGN          = "align"
    FINISH         = "finish"
    ABORT          = "abort"


_PROGRESS = {
    SwapState.IDLE: 0,
    SwapState.PROFILE_SELECTED: 12,
    SwapState.CAS_ISN_READ: 30,
    SwapState.DME_BACKED_UP: 50,
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
    cas_isn_hex: str = ""
    backup_ref: str = ""
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
        self._advance(SwapState.PROFILE_SELECTED)
        p = self.profile
        return SwapPrompt(
            state=self.state,
            title=f"تم اختيار {p.label_ar}",
            body=(
                f"العربية: {p.chassis} / المناعة: {p.cas_family}. الكنترول المستعمل "
                f"محتاج ISN العربية دي. اضغط READ_CAS_ISN عشان نقرا الـ ISN الأصلي من "
                f"الـ CAS بتاع العربية (على الفيشة OBD)."
            ),
            expects="READ_CAS_ISN",
            progress_pct=_PROGRESS[self.state],
            payload={
                "chassis": p.chassis, "cas_family": p.cas_family,
                "dme_name": p.dme_name, "requires_bench": p.requires_bench,
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

    # ── 4. WRITE_DME_ISN ──────────────────────────────────────
    async def _write_dme_isn(self) -> SwapPrompt:
        if self.state != SwapState.DME_BACKED_UP:
            raise IllegalSwapTransition(
                f"WRITE_DME_ISN only valid in DME_BACKED_UP (now {self.state.value})")
        if not self.data.backup_ref:
            return self._fail("no_backup", "Refusing to write ISN without a backup.")
        p = self.profile
        isn = bytes.fromhex(self.data.cas_isn_hex)
        await self.provider.write_dme_isn(
            vin=self.data.vin, dme_name=p.dme_name, isn=isn,
            requires_bench=p.requires_bench)
        self._advance(SwapState.DME_ISN_WRITTEN)
        return SwapPrompt(
            state=self.state,
            title="اتكتب الـ ISN في الكنترول ✍️",
            body=(
                "تمت كتابة ISN العربية في الـ DME المستعمل. اضغط VERIFY عشان نقرا الـ ISN "
                "تاني من الكنترول ونتأكد إنه مطابق."
            ),
            expects="VERIFY",
            progress_pct=_PROGRESS[self.state],
            payload={"requires_bench": p.requires_bench},
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
                "cas_isn_hex": self.data.cas_isn_hex,
                "backup_ref": self.data.backup_ref,
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
            cas_isn_hex=s.get("cas_isn_hex", ""),
            backup_ref=s.get("backup_ref", ""),
            error_code=s.get("error_code", ""),
            error_detail=s.get("error_detail", ""),
        )
        return cls(provider, data=data, state=SwapState(snapshot["state"]))
