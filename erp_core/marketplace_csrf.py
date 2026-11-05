"""🛡️ حماية CSRF لعملاء السوق (Marketplace) — تحقق من مصدر الطلب (Origin/Referer).

ليه: عملاء السوق بيدخلوا بكوكي `mp_session` (مش session Django)، وأغلب endpoints السوق
`csrf_exempt` (طلبات JSON من الواجهة). الحماية الوحيدة كانت `SameSite=Lax` — وده مايمنعش
طلب جاي من صب-دومين تاني على نفس الموقع (same-site)، يعني صفحة على شركة تانية
`*.mousstec.com` كانت تقدر تقبل عرض / تشتري باقة / تمسح لوجو باسم العميل.

الحل (OWASP — Verifying Origin With Standard Headers): أي طلب بيغيّر بيانات
(POST/PUT/PATCH/DELETE) ومعاه كوكي `mp_session` لازم يكون جاي من **نفس الـ host بالظبط**:
  • `Origin` موجود ⇒ لازم يطابق الـ host (أو يكون في MARKETPLACE_TRUSTED_ORIGINS).
  • مفيش `Origin` ⇒ نبص على `Referer` بنفس القاعدة.
  • مفيش الاتنين ⇒ مسموح: هجوم CSRF بيحصل من متصفح الضحية، والمتصفح **دايماً** بيبعت
    Origin مع أي POST جاي من صفحة تانية — فطلب من غير Origin ولا Referer جاي من
    client مش متصفح (سكريبت/تطبيق) مش من صفحة مهاجم. (`Origin: null` مرفوض.)
طلب من غير `mp_session` مش متأثر خالص (webhooks، APIs، تسجيل/دخول أول مرة) —
مفيش كوكي يتسرق فمفيش CSRF.

ميزة الطريقة دي: كل endpoints السوق (الحالية والجاية) محمية من مكان واحد من غير ما نلمس
أي زرار أو fetch في الواجهة.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger('mouss_tec_core')

UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
COOKIE = 'mp_session'


def _netloc(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return ''
    host = parts.hostname.lower()
    default = 443 if parts.scheme == 'https' else 80
    return host if parts.port in (None, default) else f'{host}:{parts.port}'


def _request_netloc(request) -> str:
    try:
        host = request.get_host().lower()
    except Exception:   # DisallowedHost — Django هيرفضه لوحده بعدين
        return ''
    for suffix in (':443', ':80'):
        if host.endswith(suffix):
            return host[: -len(suffix)]
    return host


class MarketplaceOriginCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in UNSAFE_METHODS and request.COOKIES.get(COOKIE):
            if not self._same_origin(request):
                logger.warning(
                    "🛡️ [MP-CSRF] blocked %s %s origin=%r referer=%r host=%r",
                    request.method, request.path, request.META.get('HTTP_ORIGIN'),
                    (request.META.get('HTTP_REFERER') or '')[:120], request.META.get('HTTP_HOST'),
                )
                return JsonResponse(
                    {"error": "الطلب اترفض لأسباب أمان — حدّث الصفحة وحاول تاني.", "code": "csrf_origin"},
                    status=403,
                )
        return self.get_response(request)

    @staticmethod
    def _same_origin(request) -> bool:
        expected = _request_netloc(request)
        if not expected:
            return False
        trusted = {_netloc(o) for o in getattr(settings, 'MARKETPLACE_TRUSTED_ORIGINS', [])}
        trusted.discard('')
        origin = request.META.get('HTTP_ORIGIN')
        if origin:
            got = _netloc(origin)       # «null» (iframe sandbox / file://) ⇒ '' ⇒ مرفوض
            return bool(got) and (got == expected or got in trusted)
        referer = request.META.get('HTTP_REFERER')
        if referer:
            got = _netloc(referer)
            return bool(got) and (got == expected or got in trusted)
        return True
