"""🚗 أدوات السوبر أدمن — نزاعات واسترداد سوق قطع الغيار."""
from __future__ import annotations

import logging

from clients.permissions import platform_owner_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from clients.models import (
    CustomerNotification,
    PartOrder,
    PlatformEvent,
)

from erp_core.localization import current_tenant_symbol as _sym
from ._common import _require_superadmin

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()


# =====================================================================
# 🚗 Admin: Parts marketplace dispute / refund resolution
# =====================================================================

@login_required
@platform_owner_required
def super_admin_parts_refund_approve(request, order_code):
    """
    ✅ Approve a buyer's refund request — refunds the buyer (escrow → buyer).
    POST: admin_note (optional).
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.status != 'refund_requested':
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)

    note = (request.POST.get('admin_note') or '').strip()
    # Who pays return shipping is decided by the reason category — never the
    # platform (see escrow.RETURN_REASON_TO_PAYER). Admin may override the
    # category the buyer picked.
    return_reason = (request.POST.get('return_reason') or order.return_reason or 'defective').strip()
    from clients.services import escrow as escrow_svc
    from django.core.exceptions import ValidationError as _DjVE

    try:
        with transaction.atomic():
            # 🐛 [FIX]: الموافقة كانت بتقلب حالة الطلب بس، والـ EscrowHold كان
            #    بيفضل "محجوز" للأبد — السجل المالي مش مطابق للواقع.
            escrow_svc.refund_to_buyer(order, return_reason=return_reason, by_user=request.user)
            order.status = 'refunded'
            order.refunded_at = timezone.now()
            order.admin_notes = (order.admin_notes + '\n' if order.admin_notes else '') + f'[REFUND APPROVED] {note}'
            order.save(update_fields=['status', 'refunded_at', 'admin_notes'])
    except _DjVE as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)

    payer_label = 'عليك' if order.return_shipping_payer == 'buyer' else 'على البائع'
    seller_payer_label = 'عليك' if order.return_shipping_payer == 'seller' else 'على المشتري'
    # Notify buyer
    if order.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=order.buyer_customer,
            title='✅ تم قبول طلب الإرجاع',
            body=(f'هنرجع لك مبلغ {order.amount_paid} {_sym()} خلال 3-5 أيام عمل. '
                  f'شحن الإرجاع {payer_label}.'),
            level='success', icon='fa-rotate-left',
            action_url='/marketplace/parts/orders/', action_label='طلباتي',
        )
    # Notify seller
    if order.listing.seller_customer_id:
        CustomerNotification.objects.create(
            customer=order.listing.seller_customer,
            title='⚠️ تم قبول طلب الإرجاع',
            body=(f'الإدارة وافقت على إرجاع «{order.listing.title}». تواصل مع المشتري لاسترداد القطعة. '
                  f'شحن الإرجاع {seller_payer_label}.'),
            level='warning', icon='fa-rotate-left',
            action_url='/marketplace/parts/sales/', action_label='مبيعاتي',
        )
    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='parts_market',
        user_name=request.user.username,
        description=f"✅ موافقة إرجاع طلب {order.order_code} — {order.amount_paid} {_sym()} إلى المشتري",
    )

    return JsonResponse({'ok': True, 'message': 'تم قبول الإرجاع وإشعار الطرفين.'})


@login_required
@platform_owner_required
def super_admin_parts_refund_reject(request, order_code):
    """
    ❌ Reject the refund request → releases funds to seller (admin decided
    the part is as described). POST: admin_note (required, ≥ 10 chars).
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.status != 'refund_requested':
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)

    note = (request.POST.get('admin_note') or '').strip()
    if len(note) < 10:
        return JsonResponse({'error': 'لازم تكتب سبب الرفض بالتفصيل (10 حروف على الأقل).'}, status=400)

    from clients.services import escrow as escrow_svc
    from django.core.exceptions import ValidationError as _DjVE
    try:
        with transaction.atomic():
            # 🐛 [FIX]: الرفض كان بيقلب الطلب "released" من غير ما يحرّك
            #    الـ EscrowHold — الفلوس كانت بتفضل محجوزة في السجلات.
            escrow_svc.release_to_seller(order, by_user=request.user, reason='refund request rejected by admin')
            order.status = 'released'
            order.released_at = timezone.now()
            order.admin_notes = (order.admin_notes + '\n' if order.admin_notes else '') + f'[REFUND REJECTED] {note}'
            order.save(update_fields=['status', 'released_at', 'admin_notes'])
    except _DjVE as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)

    with transaction.atomic():
        # Notify both parties
        if order.buyer_customer_id:
            CustomerNotification.objects.create(
                customer=order.buyer_customer,
                title='❌ تم رفض طلب الإرجاع',
                body=f'الإدارة قررت أن «{order.listing.title}» مطابق للوصف. السبب: {note[:150]}',
                level='danger', icon='fa-circle-xmark',
            )
        if order.listing.seller_customer_id:
            CustomerNotification.objects.create(
                customer=order.listing.seller_customer,
                title='💰 تم تحرير أموالك',
                body=f'الإدارة رفضت طلب الإرجاع. المبلغ {order.seller_payout} {_sym()} في طريقه لحسابك.',
                level='success', icon='fa-money-bill-wave',
            )

        PlatformEvent.objects.create(
            event_type='other', tenant_schema='public', tenant_name='parts_market',
            user_name=request.user.username,
            description=f"❌ رفض إرجاع طلب {order.order_code} — تحرير {order.seller_payout} {_sym()} للبائع",
        )

    return JsonResponse({'ok': True, 'message': 'تم رفض الإرجاع وتحرير أموال البائع.'})
