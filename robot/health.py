"""robot/health.py — «🩺 صحة الخدمات»: whether each online service the robot
leans on is answering — Azure (its hearing, its voice), Google's hearing,
Gemini — and when one isn't, why, and what the owner can do about it.

Every call notes how it went (`note`, kept in the cache a day, per shop); the
robot dashboard shows it (`report`), and «مخ موس» asks for it when it starts
and when the net comes back (/api/robot/v1/status/), so its log says at once
«Gemini ✗ 429: billing» instead of a deaf robot nobody can explain.

Never in the way: a note that can't be written (Redis down) is dropped.
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

from django.utils.translation import gettext as _

SERVICES = ("azure_stt", "azure_tts", "google_stt", "gemini")
KEEP_S = 24 * 3600
# The same news every call isn't news: written again only when it changes, or
# this long after (so «last seen» stays fresh) — not on every sentence.
REWRITE_S = 60
_written: dict = {}                 # (shop, service) → (ok, code, when) last written by this process


def _key(service: str) -> str:
    return f"robot:health:{service}"


def _shop() -> str:
    try:
        from django.db import connection
        return str(getattr(connection, "schema_name", "") or "")
    except Exception:
        return ""


def code_of(exc) -> Optional[str]:
    """The HTTP status in a failed call («401»), «timeout», «network», or None."""
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None):
        return str(response.status_code)
    name = type(exc).__name__.lower()
    if "timeout" in name or "timed out" in str(exc).lower():
        return "timeout"
    if "connection" in name:
        return "network"
    found = re.search(r"\b([1-5]\d\d)\b", str(exc))
    return found.group(1) if found else None


def note(service: str, ok: bool, *, code=None, detail: str = "") -> None:
    """How a call to `service` went: `ok`, else its `code` («429», «timeout»)."""
    code = None if ok else (str(code) if code is not None else None)
    mark = (_shop(), service)
    last = _written.get(mark)
    now = time.monotonic()
    if last is not None and last[0] == ok and last[1] == code and now - last[2] < REWRITE_S:
        return
    try:
        from django.core.cache import cache
        from django.utils import timezone

        cache.set(_key(service), {"ok": ok, "code": code, "detail": str(detail or "")[:200],
                                  "at": timezone.now().isoformat()}, KEEP_S)
        _written[mark] = (ok, code, now)
    except Exception:                    # the robot's answer matters, not this note
        pass


def last(service: str) -> Optional[dict]:
    try:
        from django.core.cache import cache
        found = cache.get(_key(service))
    except Exception:
        return None
    return found if isinstance(found, dict) else None


def ready(service: str) -> bool:
    """Set up on this server (its key is there)."""
    if service in ("azure_stt", "azure_tts"):
        return bool(os.getenv("ROBOT_AZURE_SPEECH_KEY", "").strip()
                    and os.getenv("ROBOT_AZURE_SPEECH_REGION", "").strip())
    if service == "google_stt":
        return bool(os.getenv("ROBOT_GOOGLE_STT_KEY", "").strip())
    from .audio import gemini_key
    return bool(gemini_key())


def label(service: str) -> str:
    return {"azure_stt": _("Azure — السمع"), "azure_tts": _("Azure — الصوت"),
            "google_stt": _("Google — السمع"), "gemini": "Gemini"}[service]


def remedy(service: str, code) -> str:
    """What the owner does about it, in a word the shop understands."""
    code = str(code or "")
    if service in ("azure_stt", "azure_tts"):
        if code in ("401", "403"):
            return _("مفتاح Azure Speech غلط أو مش بتاع نفس المنطقة: راجع ROBOT_AZURE_SPEECH_KEY "
                     "و ROBOT_AZURE_SPEECH_REGION في .env على السيرفر.")
        if code == "429":
            return _("Azure وصل لحدّه (الدقايق المجانية خلصت أو طلبات كتير مع بعض): استنى شوية أو ارفع "
                     "الاشتراك لـ S0.")
    elif service == "google_stt":
        if code in ("400", "401", "403"):
            return _("مفتاح Google Speech غلط أو Speech-to-Text مش متفعّل عليه في Google Cloud.")
        if code == "429":
            return _("حصة Google Speech خلصت.")
    elif service == "gemini":
        if code == "429":
            return _("حصة Gemini المجانية خلصت: فعّل الفوترة (Billing) على المفتاح من Google AI Studio.")
        if code == "404":
            return _("موديل Gemini اتشال: السيرفر بيجرّب اللي بعده — لو فضلت كده حدّث ROBOT_STT_MODEL.")
        if code in ("400", "401", "403"):
            return _("مفتاح Gemini غلط أو مقفول: راجع GEMINI_API_KEY في .env على السيرفر.")
        if code.startswith("5") or code == "timeout":
            return _("Gemini زحمة دلوقتي: السيرفر بيجرّب موديل تاني لوحده.")
    if code == "timeout":
        return _("مبيردش في الوقت: الخدمة زحمة أو النت عند السيرفر بطيء.")
    if code == "network":
        return _("السيرفر مش واصل للخدمة: النت عند السيرفر أو الخدمة واقعة.")
    return _("رجّعت خطأ %(code)s.") % {"code": code} if code else ""


def _minutes_left(service: str):
    from . import teachers

    teacher = {"azure_stt": "azure", "google_stt": "google", "gemini": "gemini"}.get(service)
    if teacher is None:
        return None
    try:
        return round(teachers.minutes_left(teacher), 1)
    except Exception:                    # the DB is the page's business, not this card's
        return None


def report() -> list:
    """[{"service", "label", "ready", "ok", "code", "detail", "remedy", "at",
    "minutes_left", "whole_minutes_left"}] — `ok` None: not used since the server
    started (or a day)."""
    out = []
    for service in SERVICES:
        left = _minutes_left(service)
        seen = last(service) if ready(service) else None
        ok = seen.get("ok") if seen else None
        code = seen.get("code") if seen else None
        out.append({
            "service": service, "label": label(service), "ready": ready(service),
            "ok": ok, "code": code, "detail": (seen or {}).get("detail", ""),
            "remedy": remedy(service, code) if ok is False else
            ("" if ready(service) else _("مش متظبطة على السيرفر (المفتاح ناقص).")),
            "at": (seen or {}).get("at", ""), "minutes_left": left,
            "whole_minutes_left": int(left) if left is not None else None,   # never shown as more
        })
    return out
