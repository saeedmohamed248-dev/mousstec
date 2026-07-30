"""🔔 Web Push subscription endpoints.

المتصفح بيستدعي /push/vapid-key/ يجيب الـ public key، يعمل
subscribe عبر Push API، وبعدين POST الـ subscription لـ /push/subscribe/.
الاستهداف بيتحدد من السياق: كوكي mp_session (عميل ماركت بليس) أو
جلسة Django مصادَق عليها (موظف tenant).
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from clients.models import PushSubscription
from clients.views._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')


@require_GET
def vapid_public_key(request):
    """الـ public key اللي المتصفح بيستخدمه في applicationServerKey."""
    key = getattr(settings, 'WEBPUSH_VAPID_PUBLIC_KEY', '')
    if not key:
        return JsonResponse({'enabled': False}, status=200)
    return JsonResponse({'enabled': True, 'public_key': key})


def _resolve_owner(request):
    """يرجّع dict الاستهداف: عميل ماركت بليس أو (schema, user_id) لموظف."""
    customer = _marketplace_auth(request)
    if customer is not None:
        return {'marketplace_customer_id': customer.pk}
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        schema = getattr(connection, 'schema_name', 'public')
        if schema != 'public':
            return {'tenant_schema': schema, 'user_id': user.pk}
    return None


@csrf_exempt  # التوكن/الكوكي هو المصادقة؛ الـ subscription نفسها مش سرّية
@require_POST
def push_subscribe(request):
    if not getattr(settings, 'WEBPUSH_VAPID_PRIVATE_KEY', ''):
        return JsonResponse({'ok': False, 'error': 'push_disabled'}, status=503)

    owner = _resolve_owner(request)
    if owner is None:
        return JsonResponse({'ok': False, 'error': 'not_authenticated'}, status=401)

    try:
        data = json.loads(request.body or b'{}')
        sub = data.get('subscription') or data
        endpoint = sub['endpoint']
        keys = sub.get('keys') or {}
        p256dh = keys['p256dh']
        auth = keys['auth']
    except (json.JSONDecodeError, KeyError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid_subscription'}, status=400)

    # الـ endpoint هو المفتاح الفريد — upsert عشان لو المتصفح جدّد الاشتراك،
    # وأعِد ربطه بالمالك الحالي (نفس الجهاز ممكن يبدّل مستخدم).
    obj, _created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'p256dh': p256dh,
            'auth': auth,
            'user_agent': request.META.get('HTTP_USER_AGENT', '')[:255],
            'is_active': True,
            'failure_count': 0,
            'marketplace_customer_id': owner.get('marketplace_customer_id'),
            'tenant_schema': owner.get('tenant_schema', ''),
            'user_id': owner.get('user_id'),
        },
    )
    return JsonResponse({'ok': True, 'id': obj.pk})


@csrf_exempt
@require_POST
def push_unsubscribe(request):
    try:
        data = json.loads(request.body or b'{}')
        endpoint = data.get('endpoint') or (data.get('subscription') or {}).get('endpoint')
    except json.JSONDecodeError:
        endpoint = None
    if not endpoint:
        return JsonResponse({'ok': False, 'error': 'endpoint_required'}, status=400)
    PushSubscription.objects.filter(endpoint=endpoint).update(is_active=False)
    return JsonResponse({'ok': True})


# =====================================================================
# 💱 Currency preference (display layer)
# =====================================================================

@require_GET
def currency_options(request):
    """قائمة العملات المدعومة + أسعارها الحالية + اختيار العميل الحالي."""
    from clients.services.currency import supported_list
    selected = request.COOKIES.get('mt_currency', 'EGP').upper()
    return JsonResponse({
        'selected': selected,
        'currencies': supported_list(),
    })


@csrf_exempt
@require_POST
def currency_select(request):
    """يحفظ عملة العرض المفضّلة في كوكي (سنة). لا يمسّ أي رصيد."""
    from clients.services.currency import is_supported
    try:
        data = json.loads(request.body or b'{}')
        code = str(data.get('currency', '')).upper()
    except json.JSONDecodeError:
        code = ''
    if not is_supported(code):
        return JsonResponse({'ok': False, 'error': 'unsupported_currency'}, status=400)
    resp = JsonResponse({'ok': True, 'currency': code})
    resp.set_cookie('mt_currency', code, max_age=60 * 60 * 24 * 365,
                    samesite='Lax')
    return resp
