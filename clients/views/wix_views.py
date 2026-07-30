"""🔌 Wix integration — super-admin control + real-time order webhook.

Super-admin endpoints (public schema, superuser only):
    GET  /superadmin/wix/                         → list + connect form
    POST /superadmin/wix/<tenant_id>/connect/     → save api_key + site_id
    POST /superadmin/wix/<conn_id>/test/          → test connection
    POST /superadmin/wix/<conn_id>/sync/          → trigger sync now
    POST /superadmin/wix/<conn_id>/disconnect/    → deactivate

Public webhook (Wix → us, for real-time order-created events):
    POST /api/webhooks/wix/orders/
"""
from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import connection
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt

from clients.models import Client, WixConnection

logger = logging.getLogger('mouss_tec_core')


def _require_superadmin(request):
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied (tenant schema)')
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden('Superuser only')
    return None


@login_required
@user_passes_test(lambda u: u.is_superuser)
def wix_dashboard(request):
    """صفحة إدارة روابط Wix — كل الشركات + حالة الربط."""
    guard = _require_superadmin(request)
    if guard:
        return guard

    connections = {c.client_id: c for c in WixConnection.objects.select_related('client')}
    tenants = (Client.objects.exclude(schema_name='public')
               .filter(is_active=True).order_by('name'))
    rows = []
    for t in tenants:
        rows.append({'tenant': t, 'conn': connections.get(t.id)})
    return render(request, 'clients/saas_admin/wix_dashboard.html', {
        'rows': rows,
        'connected_count': len(connections),
        'total_tenants': tenants.count(),
    })


@login_required
@user_passes_test(lambda u: u.is_superuser)
def wix_connect(request, tenant_id):
    """يربط شركة بموقع Wix (يحفظ الـ API key + Site ID) ويختبر فوراً."""
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    tenant = get_object_or_404(Client, pk=tenant_id)
    api_key = (request.POST.get('api_key') or '').strip()
    site_id = (request.POST.get('site_id') or '').strip()
    account_id = (request.POST.get('account_id') or '').strip()
    if not api_key or not site_id:
        return JsonResponse({'ok': False, 'error': 'api_key و site_id مطلوبان'}, status=400)

    conn, _created = WixConnection.objects.update_or_create(
        client=tenant,
        defaults={
            'api_key': api_key, 'site_id': site_id, 'account_id': account_id,
            'is_active': True,
        },
    )

    # اختبار فوري عشان الأدمن يعرف إن البيانات صح
    from clients.services.wix_sync import test_connection
    ok, err = test_connection(conn)
    return JsonResponse({
        'ok': True, 'connection_id': conn.pk, 'test_ok': ok,
        'message': '✅ تم الربط والاختبار بنجاح.' if ok else f'⚠️ اتحفظ بس الاختبار فشل: {err}',
    })


@login_required
@user_passes_test(lambda u: u.is_superuser)
def wix_test(request, conn_id):
    guard = _require_superadmin(request)
    if guard:
        return guard
    conn = get_object_or_404(WixConnection, pk=conn_id)
    from clients.services.wix_sync import test_connection
    ok, err = test_connection(conn)
    return JsonResponse({'ok': ok, 'error': err})


@login_required
@user_passes_test(lambda u: u.is_superuser)
def wix_sync_now(request, conn_id):
    """يشغّل المزامنة فوراً (async عبر Celery) — منتجات و/أو طلبات."""
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    conn = get_object_or_404(WixConnection, pk=conn_id)
    mode = request.POST.get('mode', 'both')  # both | products | orders
    from clients.tasks import wix_sync_one
    wix_sync_one.delay(conn.pk, mode=mode)
    return JsonResponse({
        'ok': True,
        'message': 'جاري المزامنة في الخلفية — حدّث الصفحة بعد شوية.',
    })


@login_required
@user_passes_test(lambda u: u.is_superuser)
def wix_disconnect(request, conn_id):
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    conn = get_object_or_404(WixConnection, pk=conn_id)
    conn.is_active = False
    conn.save(update_fields=['is_active', 'updated_at'])
    return JsonResponse({'ok': True, 'message': 'تم فصل الربط.'})


# ─────────────────────────────────────────────────────────────────────
# Real-time order webhook (Wix → us)
# ─────────────────────────────────────────────────────────────────────

@csrf_exempt
def wix_order_webhook(request):
    """يستقبل إشعار طلب جديد من Wix ويشغّل سحب فوري للـ tenant المعني.

    Wix بيبعت الأحداث موقّعة بـ JWT (public key بتاع الـ app). التحقق الكامل
    من التوقيع محتاج مفتاح الـ app العام؛ لحد ما يتضبط، بنعتمد على إن الـ
    site_id في الـ payload لازم يطابق ربط نشط عندنا (مش سرّي لكن يقلّل الضجيج)،
    والمزامنة نفسها idempotent (wix_order_id) فإعادة الإرسال مش بتأذي.
    """
    if request.method != 'POST':
        return HttpResponseForbidden('POST only')
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({'error': 'invalid_json'}, status=400)

    # الـ site id بييجي في الـ payload (metadata.siteId أو data.siteId حسب الحدث)
    site_id = (payload.get('metadata', {}) or {}).get('siteId') or payload.get('siteId', '')
    conn = None
    if site_id:
        conn = WixConnection.objects.filter(site_id=site_id, is_active=True,
                                            sync_orders=True).first()
    if conn is None:
        logger.info('[WIX webhook] no active connection for site=%s', site_id)
        return JsonResponse({'status': 'ignored'})

    from clients.tasks import wix_sync_one
    wix_sync_one.delay(conn.pk, mode='orders')
    return JsonResponse({'status': 'ok'})
