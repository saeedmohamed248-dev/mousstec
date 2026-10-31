"""
📡 Meta Conversions API — أحداث التسجيل والدفع من السيرفر لفيسبوك
===================================================================
الـ Pixel في المتصفح بيتقفل من مانعات الإعلانات ومن iOS، والدفع أصلاً بيحصل على السيرفر
(callback من Paymob أو مراجعة إيصال فودافون كاش) من غير متصفح العميل. فالأحداث المهمة بتتبعت
من السيرفر:

    CompleteRegistration — شركة جديدة اتسجّلت   (event_id = reg-<id>، نفس اللي في المتصفح ⇒ مابيتحسبش مرتين)
    Purchase             — فاتورة اشتراك اتدفعت  (value + currency) — ده اللي الحملة تتحسّن عليه

بيانات العميل بتتبعت مشفّرة SHA-256 (زي ما Meta طالبة): الإيميل، الموبايل بكود الدولة، ورقم
الشركة؛ ومعاهم fbc/fbp/IP/المتصفح من وقت التسجيل (SignupAttribution) عشان الربط بالإعلان.

الإعدادات (.env): META_PIXEL_ID + META_CAPI_TOKEN (من Events Manager ← Settings ← Conversions API)،
و META_CAPI_TEST_CODE اختياري لتجربة الأحداث في «Test events». أي واحد فاضي ⇒ مفيش إرسال.
الإرسال بعد الـ commit في thread منفصل — عمره ما يأخّر أو يوقّف تسجيل أو دفع.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time

import requests
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

GRAPH_VERSION = 'v21.0'


def configured():
    return bool(getattr(settings, 'META_PIXEL_ID', '') and getattr(settings, 'META_CAPI_TOKEN', ''))


def _sha(value):
    value = (value or '').strip().lower()
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def normalize_phone(phone, country='EG'):
    """01012345678 ⇒ 201012345678 · 0501234567 (إمارات) ⇒ 971501234567 — أرقام بس بكود الدولة."""
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('0'):
        digits = ('971' if country == 'AE' else '20') + digits[1:]
    return digits


def user_data(tenant):
    data = {
        'em': [_sha(tenant.email)] if getattr(tenant, 'email', '') else None,
        'ph': [_sha(normalize_phone(tenant.phone, getattr(tenant, 'country', 'EG')))]
        if getattr(tenant, 'phone', '') else None,
        'external_id': [_sha(f'tenant-{tenant.pk}')],
        'country': [_sha((getattr(tenant, 'country', '') or 'EG').lower())],
    }
    attr = getattr(tenant, 'attribution', None) if tenant.pk else None
    if attr is not None:
        data.update({'fbc': attr.fbc or None, 'fbp': attr.fbp or None,
                     'client_ip_address': attr.ip or None, 'client_user_agent': attr.user_agent or None})
    return {k: v for k, v in data.items() if v}


def build_event(event_name, tenant, *, event_id, value=None, currency=None, source_url=''):
    event = {
        'event_name': event_name,
        'event_time': int(time.time()),
        'event_id': event_id,
        'action_source': 'website',
        'user_data': user_data(tenant),
    }
    if source_url:
        event['event_source_url'] = source_url
    if value is not None:
        event['custom_data'] = {'value': float(value), 'currency': currency or 'EGP'}
    return event


def _post(event):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{settings.META_PIXEL_ID}/events"
    payload = {'data': [event], 'access_token': settings.META_CAPI_TOKEN}
    test_code = getattr(settings, 'META_CAPI_TEST_CODE', '')
    if test_code:
        payload['test_event_code'] = test_code
    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code >= 400:
            logger.warning('[META_CAPI] %s rejected %s: %s', event['event_name'], r.status_code, r.text[:300])
        else:
            logger.info('[META_CAPI] sent %s id=%s', event['event_name'], event['event_id'])
    except requests.RequestException as exc:
        logger.warning('[META_CAPI] %s failed: %s', event['event_name'], exc)


def send(event_name, tenant, *, event_id, value=None, currency=None, source_url=''):
    """يبعت الحدث بعد ما الـ transaction تتقفل بنجاح (لو اترجعت ⇒ مفيش حدث)."""
    if not configured() or tenant is None:
        return False
    try:
        event = build_event(event_name, tenant, event_id=event_id, value=value, currency=currency,
                            source_url=source_url)
    except Exception:
        logger.exception('[META_CAPI] could not build %s for tenant=%s', event_name, getattr(tenant, 'pk', None))
        return False
    transaction.on_commit(lambda: threading.Thread(target=_post, args=(event,), daemon=True).start())
    return True


def purchase_currency(tenant):
    return 'AED' if getattr(tenant, 'country', 'EG') == 'AE' else 'EGP'
