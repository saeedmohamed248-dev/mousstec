"""🖨️ أدوات السوبر أدمن — طلبات طباعة التصاميم (عرض سعر ومتابعة لحد التسليم)."""
from __future__ import annotations

import logging
from decimal import Decimal

from clients.permissions import platform_owner_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from clients.models import (
    DesignPrintRequest,
    PlatformEvent,
)

from erp_core.localization import current_tenant_symbol as _sym

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()


# =====================================================================
# 🖨️ طلبات طباعة التصاميم — عرض سعر ومتابعة لحد التسليم
# =====================================================================
# 🐛 [FIX]: العميل كان بيبعت التصميم للطباعة وعنده صفحة بتتابع ٦ مراحل
#    (عرض سعر ← قبول ← طباعة ← شحن ← تسليم) — بس مفيش أي زرار في أي مكان
#    للإدارة تبعت سعر أو تحرّك الطلب، ولا العميل يقبل العرض. كل الطلبات كانت
#    بتفضل «في الانتظار» للأبد. الانتقالات المسموحة:
PRINT_TRANSITIONS = {
    'quote':         ({'pending', 'quoted'}, 'quoted'),
    'in_production': ({'accepted'}, 'in_production'),
    'shipped':       ({'in_production'}, 'shipped'),
    'delivered':     ({'shipped'}, 'delivered'),
    'cancel':        ({'pending', 'quoted', 'accepted', 'in_production'}, 'cancelled'),
}
_PRINT_NOTICE = {
    'in_production': ('🖨️ طلب الطباعة اتبدأ', 'طلبك «{p}» دخل الطباعة.', 'info', 'fa-print'),
    'shipped':       ('🚚 طلب الطباعة اتشحن', 'طلبك «{p}» اتشحن وفي الطريق ليك.', 'info', 'fa-truck'),
    'delivered':     ('📦 طلب الطباعة اتسلّم', 'طلبك «{p}» اتسلّم. شكراً لثقتك 🙏', 'success', 'fa-box-open'),
}


@login_required
@platform_owner_required
def super_admin_print_request_update(request, request_id):
    """POST action ∈ quote(price, note) | in_production | shipped | delivered | cancel(note) → JSON."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    from clients.services.customer_notify import notify_customer
    action = (request.POST.get('action') or '').strip()
    note = (request.POST.get('note') or '').strip()[:1000]
    if action not in PRINT_TRANSITIONS:
        return JsonResponse({'error': 'إجراء غير معروف.'}, status=400)
    allowed_from, new_status = PRINT_TRANSITIONS[action]
    with transaction.atomic():
        pr = get_object_or_404(DesignPrintRequest.objects.select_for_update().select_related('customer'),
                               pk=request_id)
        if pr.status not in allowed_from:
            return JsonResponse({'error': f'مينفعش من حالة «{pr.get_status_display()}».'}, status=409)
        fields = ['status', 'updated_at']
        if action == 'quote':
            try:
                price = Decimal((request.POST.get('price') or '').strip())
            except Exception:
                price = Decimal('0')
            if price <= 0:
                return JsonResponse({'error': 'اكتب سعر أكبر من صفر.'}, status=400)
            pr.quoted_price = price.quantize(Decimal('0.01'))
            fields.append('quoted_price')
        if note:
            pr.admin_notes = note
            fields.append('admin_notes')
        pr.status = new_status
        pr.save(update_fields=fields)

    product = pr.get_product_type_display()
    url = '/marketplace/design-store/print-orders/'
    if action == 'quote':
        notify_customer(
            pr.customer, title='💰 عرض سعر لطلب الطباعة',
            body=(f'طلبك «{product}» × {pr.quantity}: السعر {pr.quoted_price:,.2f} {_sym()}.'
                  + (f' {note}' if note else '') + ' ادخل وافق على العرض أو ارفضه.'),
            level='info', icon='fa-tags', action_url=url, action_label='شوف العرض', sent_by=request.user)
    elif action == 'cancel':
        notify_customer(
            pr.customer, title='❌ طلب الطباعة اتلغى',
            body=f'طلبك «{product}» اتلغى' + (f': {note}' if note else '.'),
            level='warning', icon='fa-circle-xmark', action_url=url, action_label='طلباتي', sent_by=request.user)
    else:
        title, body, level, icon = _PRINT_NOTICE[action]
        notify_customer(pr.customer, title=title, body=body.format(p=product), level=level, icon=icon,
                        action_url=url, action_label='تابع الطلب', sent_by=request.user)
    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='marketplace', user_name=request.user.username,
        description=f"🖨️ طلب طباعة #{pr.pk} ({product}) → {pr.get_status_display()}"
                    + (f" — {pr.quoted_price} {_sym()}" if action == 'quote' else ''),
    )
    return JsonResponse({'ok': True, 'status': pr.status, 'status_label': pr.get_status_display(),
                         'message': f'تم — الطلب بقى «{pr.get_status_display()}» والعميل اتبلّغ.'})
