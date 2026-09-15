"""أدوات HTTP مشتركة — ردود آمنة لا تكسر الصفحة في وش المستخدم.

السبب من وجود الملف ده: Django بيرمي `SuspiciousOperation` (وبالتالي صفحة
"Bad Request (400)" الخام) في حالات كتير مالهاش علاقة بالمستخدم — أشهرها
`DisallowedRedirect` لما نعمل redirect لـ URL بسكيمة مش http/https (زي
Referer جاي من تطبيق موبايل: `whatsapp://`، `android-app://`، `intent://`).
الدوال هنا بتنضّف أي وجهة قبل ما نستخدمها.
"""
from __future__ import annotations

from django.utils.http import url_has_allowed_host_and_scheme

#: أحرف ممنوعة جوه هيدر Location (بتكسر الرد بـ BadHeaderError → 500).
_CTRL_CHARS = ('\r', '\n', '\x00')


def safe_internal_path(raw, default: str = '/') -> str:
    """يرجّع مسار داخلي آمن (يبدأ بـ / ومش //) أو `default`."""
    nxt = (raw or '').strip()
    if not nxt.startswith('/') or nxt.startswith('//') or nxt.startswith('/\\'):
        return default
    if any(ch in nxt for ch in _CTRL_CHARS):
        return default
    return nxt


def safe_back(request, default: str = '/') -> str:
    """وجهة رجوع آمنة مبنية على الـ Referer.

    الـ Referer هيدر بيتحكم فيه المتصفح بالكامل، فممنوع نمرّره لـ redirect
    من غير فحص: سكيمة غريبة → DisallowedRedirect → 400، ودومين تاني →
    open redirect.
    """
    ref = (request.META.get('HTTP_REFERER') or '').strip()
    if not ref or any(ch in ref for ch in _CTRL_CHARS):
        return default
    try:
        allowed = {request.get_host()}
    except Exception:  # noqa: BLE001 — DisallowedHost وغيره
        allowed = set()
    if url_has_allowed_host_and_scheme(ref, allowed_hosts=allowed,
                                       require_https=request.is_secure()):
        return ref
    return default
