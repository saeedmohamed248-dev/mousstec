"""
📣 تتبّع مصدر الزائر (UTM / fbclid / gclid) لحد ما يسجّل شركة
=================================================================
اللينك بتاع الإعلان شكله كده:
    https://mousstec.com/automotive/?utm_source=facebook&utm_medium=paid&utm_campaign=workshops_cairo&utm_content=video1

أول ما الزائر يفتحه، المصدر بيتحفظ في كوكي موقّعة ``mt_attr`` لمدة 90 يوم (على الدومين
الرئيسي وكل الصب دومينز). لو ضغط بعد كده على إعلان تاني، الكوكي بتتحدّث (آخر حملة — زي
إعدادات فيسبوك الافتراضية). الزيارة العادية من غير بارامترات مابتمسحش المصدر.

لما يسجّل شركة: ``save_for_tenant`` بينسخ المصدر لـ SignupAttribution.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core import signing

logger = logging.getLogger(__name__)

COOKIE = 'mt_attr'
MAX_AGE = 60 * 60 * 24 * 90
_SALT = 'mt-attribution'
UTM_KEYS = ('source', 'medium', 'campaign', 'content', 'term')
_CLICK_KEYS = ('fbclid', 'gclid')


def _cut(value, n):
    return (value or '').strip()[:n]


def from_query(request):
    """المصدر من بارامترات اللينك — None لو مفيش حملة في اللينك."""
    q = request.GET
    data = {k: _cut(q.get(f'utm_{k}'), 150) for k in UTM_KEYS}
    for k in _CLICK_KEYS:
        data[k] = _cut(q.get(k), 255)
    if not any(data.values()):
        return None
    if data['fbclid']:
        # صيغة fbc الرسمية: fb.1.<وقت الضغط بالملي ثانية>.<fbclid>
        data['fbc'] = f"fb.1.{int(time.time() * 1000)}.{data['fbclid']}"
    data['landing_path'] = _cut(request.path, 300)
    ref = request.META.get('HTTP_REFERER', '')
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    data['referrer'] = '' if base in ref else _cut(ref, 300)
    return data


def read(request):
    raw = request.COOKIES.get(COOKIE)
    if not raw:
        return {}
    try:
        data = signing.loads(raw, salt=_SALT, max_age=MAX_AGE)
        return data if isinstance(data, dict) else {}
    except signing.BadSignature:
        return {}


def cookie_domain(request):
    """الكوكي على ‎.mousstec.com عشان اللي فتح auto.mousstec.com وسجّل على mousstec.com يتحسب."""
    host = request.get_host().split(':')[0].lower()
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    return f'.{base}' if host == base or host.endswith(f'.{base}') else None


class AttributionMiddleware:
    """بيحفظ مصدر الإعلان في كوكي لما اللينك فيه utm_*/fbclid/gclid (GET بس — رخيص جداً)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method == 'GET' and request.GET:
            try:
                data = from_query(request)
                if data:
                    response.set_cookie(COOKIE, signing.dumps(data, salt=_SALT), max_age=MAX_AGE,
                                        domain=cookie_domain(request), samesite='Lax',
                                        secure=request.is_secure(), httponly=True)
            except Exception:
                logger.exception('[ATTRIBUTION] failed to store campaign cookie')
        return response


def client_ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')) or None


def save_for_tenant(tenant, request):
    """ينسخ مصدر الزائر للشركة اللي لسه متسجّلة (مرة واحدة). عمره مايوقّف التسجيل."""
    from clients.models import SignupAttribution
    try:
        data = read(request)
        fbc = data.get('fbc') or request.COOKIES.get('_fbc', '')
        return SignupAttribution.objects.update_or_create(tenant=tenant, defaults={
            **{k: data.get(k, '') for k in UTM_KEYS + _CLICK_KEYS + ('landing_path', 'referrer')},
            'fbc': _cut(fbc, 300),
            'fbp': _cut(request.COOKIES.get('_fbp', ''), 120),
            'ip': client_ip(request),
            'user_agent': _cut(request.META.get('HTTP_USER_AGENT', ''), 300),
        })[0]
    except Exception:
        logger.exception('[ATTRIBUTION] could not save signup source tenant=%s', getattr(tenant, 'pk', None))
        return None
