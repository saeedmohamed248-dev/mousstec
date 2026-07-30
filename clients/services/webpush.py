"""🔔 Web Push delivery — pywebpush + VAPID.

الاستخدام:
    from clients.services.webpush import notify_marketplace_customer, notify_tenant_user
    notify_marketplace_customer(customer_id, title='...', body='...', url='/...')

المفاتيح (مرة واحدة لكل deployment):
    python -c "from py_vapid import Vapid; v=Vapid(); v.generate_keys(); \
               print('PRIVATE:', v.private_pem().decode()); \
               print('PUBLIC (b64url):', v.public_key_urlsafe_base64())"
ثم WEBPUSH_VAPID_PRIVATE_KEY / WEBPUSH_VAPID_PUBLIC_KEY في .env.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

# اشتراك بيفشل كذا مرة ورا بعض غالباً متصفح اتشال — بنعطّله بدل ما نفضل نحاول
_MAX_CONSECUTIVE_FAILURES = 5


def webpush_configured() -> bool:
    return bool(getattr(settings, 'WEBPUSH_VAPID_PRIVATE_KEY', '')
                and getattr(settings, 'WEBPUSH_VAPID_PUBLIC_KEY', ''))


def send_to_subscription(subscription, payload: dict) -> bool:
    """يبعت payload لاشتراك واحد. يعطّل الاشتراكات الميتة (404/410).

    Returns True on success. Never raises.
    """
    if not webpush_configured():
        return False
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.error("[WEBPUSH] pywebpush not installed — pip install pywebpush")
        return False

    try:
        webpush(
            subscription_info={
                'endpoint': subscription.endpoint,
                'keys': {'p256dh': subscription.p256dh, 'auth': subscription.auth},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
            vapid_claims={'sub': f"mailto:{getattr(settings, 'WEBPUSH_VAPID_ADMIN_EMAIL', 'admin@mousstec.com')}"},
            ttl=3600,
        )
        subscription.failure_count = 0
        subscription.last_success_at = timezone.now()
        subscription.save(update_fields=['failure_count', 'last_success_at'])
        return True
    except WebPushException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in (404, 410):
            # المتصفح ألغى الاشتراك — نعطّله نهائياً
            subscription.is_active = False
            subscription.save(update_fields=['is_active'])
            logger.info("[WEBPUSH] subscription gone (HTTP %s) — deactivated #%s",
                        status, subscription.pk)
        else:
            subscription.failure_count += 1
            if subscription.failure_count >= _MAX_CONSECUTIVE_FAILURES:
                subscription.is_active = False
            subscription.save(update_fields=['failure_count', 'is_active'])
            logger.warning("[WEBPUSH] send failed (HTTP %s) sub=#%s: %s",
                           status, subscription.pk, exc)
        return False
    except Exception as exc:
        logger.warning("[WEBPUSH] unexpected error sub=#%s: %s", subscription.pk, exc)
        return False


def _payload(title: str, body: str, url: str) -> dict:
    return {
        'title': title,
        'body': body,
        'url': url or '/',
        'icon': '/static/icon-192.png',
        'badge': '/static/icon-192.png',
    }


def notify_marketplace_customer(customer_id: int, *, title: str, body: str,
                                url: str = '/') -> int:
    """يبعت لكل اشتراكات عميل ماركت بليس النشطة. Returns عدد الناجح."""
    from clients.models import PushSubscription
    sent = 0
    subs = PushSubscription.objects.filter(
        marketplace_customer_id=customer_id, is_active=True)
    for sub in subs:
        if send_to_subscription(sub, _payload(title, body, url)):
            sent += 1
    return sent


def notify_tenant_user(tenant_schema: str, user_id: int, *, title: str,
                       body: str, url: str = '/') -> int:
    """يبعت لموظف tenant (اشتراكاته متخزنة بـ schema + user_id)."""
    from clients.models import PushSubscription
    sent = 0
    subs = PushSubscription.objects.filter(
        tenant_schema=tenant_schema, user_id=user_id, is_active=True)
    for sub in subs:
        if send_to_subscription(sub, _payload(title, body, url)):
            sent += 1
    return sent
