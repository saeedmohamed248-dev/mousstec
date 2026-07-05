"""Engine-swap diagnosis — the "why won't it start after a DME swap" brain.

A technician fits a second-hand DME (and often the gearbox) and the car
won't crank/start. This module turns the raw facts a scan collects —
    • the CAS immobiliser ISN,
    • the newly-fitted DME's ISN,
    • the engine code the stored FA (Fahrzeugauftrag) claims,
    • the engine the DME actually reports,
into a structured, bilingual diagnosis the bot renders:

    • WILL it start?  (no, if the ISN pair doesn't match)
    • the ranked actions to fix it, each tagged with WHERE it happens
      (OBD vs bench) and whether the software can drive it over the cable.

This is deliberately transport/crypto-agnostic and Django-free so it unit
-tests as pure Python. The honest hardware truth lives in the inputs:
`dme_requires_bench=True` for Bosch MEVD17 (N18) — its ISN is NOT writable
over UDS/OBD, so the align-ISN action is flagged bench-only and the bot
cannot do it over the D-CAN cable. `diagnose_from_profile()` fills that
flag from KNOWN_PROFILES so callers don't hard-code it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _is_blank_isn(isn: Optional[bytes]) -> bool:
    """A virgin / unreadable ISN window is all 0x00 or all 0xFF."""
    if not isn:
        return True
    return len(set(isn)) == 1 and isn[0] in (0x00, 0xFF)


@dataclass(frozen=True)
class SwapAction:
    """One remediation step, tagged with where it runs and if the bot can."""
    key: str                 # "align_isn" | "update_fa" | "adapt_egs"
    title_ar: str
    title_en: str
    where: str               # "obd" | "bench"
    bot_can_do: bool         # can the software drive it over the cable now?
    detail_ar: str = ""
    detail_en: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title_ar": self.title_ar, "title_en": self.title_en,
            "where": self.where, "bot_can_do": self.bot_can_do,
            "detail_ar": self.detail_ar, "detail_en": self.detail_en,
        }


@dataclass(frozen=True)
class EngineSwapDiagnosis:
    isn_mismatch: bool          # CAS ISN != DME ISN → immobiliser blocks start
    isn_unconfirmed: bool       # one side unreadable/virgin → can't be sure
    fa_engine_mismatch: bool    # FA says engine X, DME reports engine Y
    fa_engine: str
    actual_engine: str
    will_start: bool            # False while the ISN pair doesn't match
    actions: tuple[SwapAction, ...] = ()
    summary_ar: str = ""
    summary_en: str = ""

    def to_dict(self) -> dict:
        return {
            "isn_mismatch": self.isn_mismatch,
            "isn_unconfirmed": self.isn_unconfirmed,
            "fa_engine_mismatch": self.fa_engine_mismatch,
            "fa_engine": self.fa_engine,
            "actual_engine": self.actual_engine,
            "will_start": self.will_start,
            "actions": [a.to_dict() for a in self.actions],
            "summary_ar": self.summary_ar,
            "summary_en": self.summary_en,
        }


def diagnose_engine_swap(*,
                         cas_isn: Optional[bytes],
                         dme_isn: Optional[bytes],
                         fa_engine: str,
                         dme_reported_engine: str,
                         dme_requires_bench: bool) -> EngineSwapDiagnosis:
    """Diagnose a post-swap no-start from the four facts a scan gives us.

    `dme_requires_bench` comes from the DME profile (True for MEVD17/N18):
    when True the ISN write can't ride the OBD/D-CAN cable, so the align
    step is bench-only and `bot_can_do=False`.
    """
    fa_engine = (fa_engine or "").strip().upper()
    dme_reported_engine = (dme_reported_engine or "").strip().upper()

    isn_unconfirmed = _is_blank_isn(cas_isn) or _is_blank_isn(dme_isn)
    isn_mismatch = (not isn_unconfirmed) and (cas_isn != dme_isn)
    fa_engine_mismatch = bool(fa_engine and dme_reported_engine
                              and fa_engine != dme_reported_engine)
    will_start = not isn_mismatch and not isn_unconfirmed

    actions: list[SwapAction] = []

    if isn_mismatch or isn_unconfirmed:
        where = "bench" if dme_requires_bench else "obd"
        actions.append(SwapAction(
            key="align_isn",
            title_ar="تزاوج الـ ISN بين الـ CAS والكمبيوتر الجديد",
            title_en="Align ISN between CAS and the new DME",
            where=where,
            bot_can_do=(where == "obd"),
            detail_ar=(
                "الكمبيوتر الجديد مش متزاوج مع إيموبيلايزر العربية، فالـ CAS "
                "بيقفل التشغيل. "
                + ("على MEVD17 (N18) الـ ISN مايتكتبش عبر OBD — لازم بنش "
                   "(فك الـ DME + boot pin + سيريال / TriCore BSL)."
                   if where == "bench" else
                   "ينفع يتعمل عبر الكابل بعد قراية ISN الـ CAS.")
            ),
            detail_en=(
                "The new DME isn't married to the car's immobiliser, so the "
                "CAS blocks starting. "
                + ("On MEVD17 (N18) the ISN can't be written over OBD — bench "
                   "required (open the DME, boot pin + serial / TriCore BSL)."
                   if where == "bench" else
                   "Doable over the cable after reading the CAS ISN.")
            ),
        ))

    if fa_engine_mismatch:
        actions.append(SwapAction(
            key="update_fa",
            title_ar=f"تحديث الـ FA من {fa_engine} إلى {dme_reported_engine}",
            title_en=f"Update the FA from {fa_engine} to {dme_reported_engine}",
            where="obd",
            bot_can_do=True,
            detail_ar=(
                f"الـ FA المخزّن بيقول {fa_engine} بينما الكمبيوتر الفعلي "
                f"{dme_reported_engine}، عشان كده ISTA بتعرّفها غلط. تحديث الـ "
                "FA بيتعمل عبر الكابل."
            ),
            detail_en=(
                f"The stored FA says {fa_engine} but the actual DME is "
                f"{dme_reported_engine}, so ISTA identifies the car wrong. "
                "Updating the FA is done over the cable."
            ),
        ))

    # Build the human summary.
    if isn_mismatch:
        summary_ar = ("🔴 مش هتدور: الكمبيوتر الجديد مش متزاوج مع الـ CAS "
                      "(اختلاف ISN). لازم تزاوج ISN الأول.")
        summary_en = ("🔴 Won't start: the new DME isn't married to the CAS "
                      "(ISN mismatch). Align the ISN first.")
    elif isn_unconfirmed:
        summary_ar = ("🟠 مش متأكدين من الـ ISN (قراية ناقصة/virgin). أكّد "
                      "قراية ISN الـ CAS والكمبيوتر قبل أي حكم.")
        summary_en = ("🟠 ISN unconfirmed (unreadable/virgin). Confirm the CAS "
                      "and DME ISN reads before concluding.")
    else:
        summary_ar = "🟢 الـ ISN متطابق — الإيموبيلايزر مش سبب عدم الدوران."
        summary_en = "🟢 ISN matches — the immobiliser is not the no-start cause."

    if fa_engine_mismatch:
        summary_ar += (f" وكمان الـ FA بيقول {fa_engine} والكمبيوتر "
                       f"{dme_reported_engine} — محتاج تحديث FA.")
        summary_en += (f" Also the FA says {fa_engine} vs DME "
                       f"{dme_reported_engine} — FA update needed.")

    return EngineSwapDiagnosis(
        isn_mismatch=isn_mismatch,
        isn_unconfirmed=isn_unconfirmed,
        fa_engine_mismatch=fa_engine_mismatch,
        fa_engine=fa_engine,
        actual_engine=dme_reported_engine,
        will_start=will_start,
        actions=tuple(actions),
        summary_ar=summary_ar,
        summary_en=summary_en,
    )


def diagnose_from_profile(*,
                          profile_name: str,
                          cas_isn: Optional[bytes],
                          dme_isn: Optional[bytes],
                          fa_engine: str) -> EngineSwapDiagnosis:
    """Same as `diagnose_engine_swap` but pulls the actual engine + the
    bench requirement from KNOWN_PROFILES so callers don't hard-code them.

    Imported lazily to keep the pure-logic core Django/​profile-free.
    """
    from ..execution import KNOWN_PROFILES

    profile = KNOWN_PROFILES[profile_name]
    return diagnose_engine_swap(
        cas_isn=cas_isn,
        dme_isn=dme_isn,
        fa_engine=fa_engine,
        dme_reported_engine=getattr(profile, "engine", "") or "",
        dme_requires_bench=bool(getattr(profile, "requires_bench", False)),
    )
