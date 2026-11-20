"""🔔 إشعارات الموبايل (Firebase Cloud Messaging — HTTP v1).

الإعداد (متغيرات بيئة على السيرفر):
    FCM_SERVICE_ACCOUNT_JSON   محتوى ملف service account من Firebase (JSON كامل)
                               أو FCM_SERVICE_ACCOUNT_FILE = مسار الملف
من غيرهم الإشعارات بتتسجّل في اللوج بس — ومفيش أي عملية في السيستم بتقع بسببها.

مفيش مكتبة جديدة: التوقيع بـ PyJWT (موجودة مع simplejwt) والطلبات بـ requests.
"""
import json
import logging
import os
import time

from django.core.cache import cache
from django.db import connection

logger = logging.getLogger('mouss_tec_core')

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
_CACHE_KEY = 'mobile_api:fcm_access_token'


def _service_account():
    raw = os.getenv('FCM_SERVICE_ACCOUNT_JSON', '').strip()
    path = os.getenv('FCM_SERVICE_ACCOUNT_FILE', '').strip()
    try:
        if raw:
            return json.loads(raw)
        if path and os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                return json.load(fh)
    except (ValueError, OSError):
        logger.warning('[PUSH] ملف Firebase service account مش مظبوط')
    return None


def is_configured():
    sa = _service_account()
    return bool(sa and sa.get('private_key') and sa.get('client_email') and sa.get('project_id'))


def _access_token(sa):
    cached = cache.get(_CACHE_KEY)
    if cached:
        return cached
    import jwt
    import requests
    now = int(time.time())
    assertion = jwt.encode({'iss': sa['client_email'], 'scope': SCOPE, 'aud': TOKEN_URL,
                            'iat': now, 'exp': now + 3600}, sa['private_key'], algorithm='RS256')
    resp = requests.post(TOKEN_URL, data={
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}, timeout=10)
    resp.raise_for_status()
    token = resp.json()['access_token']
    cache.set(_CACHE_KEY, token, 50 * 60)
    return token


def send_to_tokens(tokens, title, body, data=None):
    """يبعت لكل توكن؛ بيرجّع التوكنات اللي Firebase قال إنها مابقتش شغالة (عشان تتقفل)."""
    sa = _service_account()
    if not tokens:
        return []
    if not sa:
        logger.info('[PUSH] (مش متظبط) %s — %s → %d جهاز', title, body, len(tokens))
        return []
    import requests
    try:
        access = _access_token(sa)
    except Exception:   # noqa: BLE001 — الإشعار عمره ما يوقّع عملية
        logger.exception('[PUSH] فشل توكن Firebase')
        return []
    url = f"https://fcm.googleapis.com/v1/projects/{sa['project_id']}/messages:send"
    payload_data = {k: str(v) for k, v in (data or {}).items()}
    dead = []
    for tok in tokens:
        msg = {'message': {'token': tok, 'notification': {'title': title, 'body': body},
                           'data': payload_data,
                           'android': {'priority': 'high', 'notification': {'sound': 'default'}},
                           'apns': {'payload': {'aps': {'sound': 'default'}}}}}
        try:
            r = requests.post(url, json=msg, timeout=10,
                              headers={'Authorization': f'Bearer {access}'})
        except Exception:   # noqa: BLE001
            logger.exception('[PUSH] فشل الإرسال')
            continue
        if r.status_code in (404, 400) and ('UNREGISTERED' in r.text or 'INVALID_ARGUMENT' in r.text):
            dead.append(tok)
        elif r.status_code >= 300:
            logger.warning('[PUSH] FCM %s: %s', r.status_code, r.text[:300])
    return dead


def deliver(schema_name, user_ids, title, body, data=None):
    """جوه schema الشركة: هات أجهزة المستخدمين وابعت، واقفل التوكنات الميتة."""
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        from .models import MobileDevice
        devices = MobileDevice.objects.filter(user_id__in=list(user_ids), is_active=True,
                                              user__is_active=True)
        tokens = list(devices.values_list('token', flat=True))
        dead = send_to_tokens(tokens, title, body, data)
        if dead:
            MobileDevice.objects.filter(token__in=dead).update(is_active=False)
        return len(tokens)


def notify_users(user_ids, title, body, data=None):
    """نقطة الدخول من السيستم: بيبعت في الخلفية (Celery) بعد ما الـ transaction يتحفظ.

    آمن 100%: أي خطأ هنا بيتسجّل ومابيوقّعش العملية الأصلية."""
    user_ids = sorted({int(u) for u in user_ids if u})
    schema = getattr(connection, 'schema_name', 'public')
    if not user_ids or schema == 'public':
        return

    def _send():
        try:
            from .tasks import send_push
            send_push.delay(schema, user_ids, title, body, data or {})
        except Exception:   # noqa: BLE001 — Celery/Redis واقع ⇒ نحاول على طول
            try:
                deliver(schema, user_ids, title, body, data)
            except Exception:   # noqa: BLE001
                logger.exception('[PUSH] فشل الإشعار')

    from django.db import transaction
    transaction.on_commit(_send)


def users_with_perm(perm):
    """موظفين الشركة اللي عندهم الصلاحية دي (المالك/السوبر يوزر دايماً) — ولهم جهاز مسجّل."""
    from django.contrib.auth import get_user_model
    from .models import MobileDevice
    User = get_user_model()
    ids = set(MobileDevice.objects.filter(is_active=True).values_list('user_id', flat=True))
    if not ids:
        return []
    return [u.pk for u in User.objects.filter(pk__in=ids, is_active=True) if u.has_perm(perm)]


def users_with_roles(roles):
    """موظفين الورشة بالدور (admin/manager…) — زي صلاحيات تطبيق الورش."""
    from django.contrib.auth import get_user_model
    from .models import MobileDevice
    from .permissions import has_role
    User = get_user_model()
    ids = set(MobileDevice.objects.filter(is_active=True).values_list('user_id', flat=True))
    if not ids:
        return []
    return [u.pk for u in User.objects.filter(pk__in=ids, is_active=True) if has_role(u, roles)]
