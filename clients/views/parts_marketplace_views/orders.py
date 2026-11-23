"""
🚗 سوق قطع الغيار P2P — بعد الشراء: مشترياتي/مبيعاتي، الشحن، تأكيد الاستلام، المرتجع، الإلغاء، التقييم، المحفظة.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models import (
    CustomerNotification,
    MarketplaceCustomer,
    PartOrder,
    PlatformEvent,
)
from clients.views._shared import _marketplace_auth
from marketplace_b2b.services import parts_orders as orders_svc

from clients.views._parts_helpers import (
    _parse_int,
)

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 4. POST-PURCHASE — orders list, confirm delivery, request refund
# ─────────────────────────────────────────────────────────────────────

def parts_my_orders(request):
    """List the current customer's purchases."""
    customer = _marketplace_auth(request)
    if not customer:
        # 🐛 [FIX]: من غير ?next العميل (اللي جاي من إشعار) كان بيرجع للوحة مش للطلب.
        return redirect('/marketplace/login/?next=/marketplace/parts/orders/')
    orders = list(
        PartOrder.objects.filter(buyer_customer=customer)
        .select_related('listing', 'listing__car_make', 'listing__seller_customer')
        .order_by('-created_at')[:50]
    )
    _decorate_orders(orders, customer, mode='buyer')
    return render(request, 'clients/marketplace/parts_orders.html', {
        'customer': customer, 'orders': orders, 'mode': 'buyer',
        'paid_flag': request.GET.get('paid', ''),
        **_orders_page_choices(),
    })


def parts_my_sales(request):
    """List the current customer's sales."""
    customer = _marketplace_auth(request)
    if not customer:
        # 🐛 [FIX]: من غير ?next العميل (اللي جاي من إشعار) كان بيرجع للوحة مش للطلب.
        return redirect('/marketplace/login/?next=/marketplace/parts/sales/')
    # Unpaid / cancelled checkouts are noise for the seller — they only need
    # orders that were actually paid.
    orders = list(
        PartOrder.objects.filter(listing__seller_customer=customer)
        .exclude(status__in=('pending_payment', 'cancelled'))
        .select_related('listing', 'listing__car_make', 'buyer_customer')
        .order_by('-created_at')[:50]
    )
    _decorate_orders(orders, customer, mode='seller')
    return render(request, 'clients/marketplace/parts_orders.html', {
        'customer': customer, 'orders': orders, 'mode': 'seller',
        **_orders_page_choices(),
    })


def _orders_page_choices():
    from clients.models import DisputeTicket
    buyer_categories = ('item_not_received', 'item_not_as_described', 'damaged_on_arrival',
                        'wrong_item', 'counterfeit', 'payment_issue', 'other')
    seller_categories = ('buyer_misuse', 'payment_issue', 'other')
    cats = dict(DisputeTicket.CATEGORY_CHOICES)
    return {
        'return_reason_choices': PartOrder.RETURN_REASON_CHOICES,
        'buyer_dispute_choices': [(c, cats[c]) for c in buyer_categories],
        'seller_dispute_choices': [(c, cats[c]) for c in seller_categories],
    }


def _decorate_orders(orders, customer, *, mode):
    """
    Attach per-order UI flags so the template stays dumb:
      * can_dispute      — inside the dispute window and no open ticket yet
      * open_dispute     — the open ticket (if any)
      * receipt_url      — Vodafone-Cash upload page for an unpaid manual order
      * show_shipping    — seller may see the buyer's address once paid
    🐛 [FIX]: البائع ماكانش بيشوف عنوان/تليفون المشتري خالص — مكانش يعرف
       يشحن على فين. والمشتري ماكانش عنده أي زرار لفتح نزاع رغم إن الـ API موجود.
    """
    from clients.models import DisputeTicket, ManualPaymentReceipt
    if not orders:
        return
    ids = [o.pk for o in orders]
    open_tickets = {
        t.order_id: t for t in DisputeTicket.objects.filter(
            order_id__in=ids, status__in=('open', 'under_review'), is_deleted=False,
        )
    }
    receipts = {}
    if mode == 'buyer':
        for r in ManualPaymentReceipt.objects.filter(
            purchase_type='parts', purchase_id__in=ids, status='pending',
        ).order_by('created_at'):
            receipts[r.purchase_id] = r
    for o in orders:
        o.open_dispute = open_tickets.get(o.pk)
        o.can_dispute = (o.open_dispute is None and DisputeTicket.is_within_window(o))
        o.show_shipping = mode == 'seller' and o.status not in ('pending_payment', 'cancelled')
        o.ship_deadline = (o.paid_at + orders_svc.SELLER_SHIP_DEADLINE
                           if (o.status == 'paid_held' and o.paid_at) else None)
        r = receipts.get(o.pk)
        o.receipt_url = (
            reverse('manual_payment_upload', args=[r.receipt_code])
            if (r and o.status == 'pending_payment') else ''
        )
        o.receipt_uploaded = bool(r and r.txn_reference)


@require_POST
def parts_mark_shipped(request, order_code):
    """Seller marks an order as shipped."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.listing.seller_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    tracking = (request.POST.get('tracking') or '').strip()[:200]
    with transaction.atomic():
        # Lock + conditional update so a double click / concurrent dispute
        # can't overwrite a newer status.
        updated = PartOrder.objects.filter(pk=order.pk, status='paid_held').update(
            status='shipped', shipped_at=timezone.now(), shipping_tracking=tracking,
        )
    if not updated:
        order.refresh_from_db(fields=['status'])
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)
    if order.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=order.buyer_customer,
            title='🚚 شُحنت قطعتك',
            body=(f'البائع شحن «{order.listing.title}».'
                  + (f' بيانات الشحن: {tracking}.' if tracking else '')
                  + f' أكد الاستلام لما توصل عشان يبدأ ضمان {order.warranty_days} يوم.'),
            level='info', icon='fa-truck',
            action_url='/marketplace/parts/orders/', action_label='تفاصيل',
        )
    return JsonResponse({'ok': True, 'message': 'تم تعليم الطلب كمشحون.'})


@require_POST
def parts_confirm_delivery(request, order_code):
    """Buyer confirms receipt → warranty window starts."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.buyer_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    with transaction.atomic():
        order = PartOrder.objects.select_for_update().select_related('listing').get(pk=order.pk)
        if not order.mark_delivered():
            return JsonResponse({'error': 'لا يمكن تأكيد التسليم في هذه الحالة.'}, status=400)
    if order.listing.seller_customer_id:
        CustomerNotification.objects.create(
            customer=order.listing.seller_customer,
            title='📦 المشتري استلم القطعة',
            body=(f'تم تأكيد استلام «{order.listing.title}». المبلغ هيتحوّل لك تلقائياً '
                  f'بعد انتهاء الضمان ({order.warranty_days} يوم) لو مفيش مشكلة.'),
            level='info', icon='fa-box-open',
            action_url='/marketplace/parts/sales/', action_label='مبيعاتي',
        )
    return JsonResponse({
        'ok': True,
        'message': f'تم التأكيد. فترة الضمان: {order.warranty_days} يوم.',
        'warranty_ends_at': order.warranty_ends_at.isoformat() if order.warranty_ends_at else None,
    })


@require_POST
def parts_request_refund(request, order_code):
    """Buyer requests refund within warranty window."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.buyer_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    if order.status != 'delivered':
        return JsonResponse({'error': 'الإرجاع متاح فقط خلال فترة الضمان.'}, status=400)
    if order.warranty_ends_at and timezone.now() > order.warranty_ends_at:
        return JsonResponse({'error': 'انتهت فترة الضمان — لا يمكن الإرجاع.'}, status=400)

    reason = (request.POST.get('reason') or '').strip()[:2000]
    if len(reason) < 10:
        return JsonResponse({'error': 'اكتب سبب الإرجاع بالتفصيل (10 حروف على الأقل).'}, status=400)
    # The reason category decides who pays return shipping (never the
    # platform) — recorded now, applied when the admin approves.
    return_reason = (request.POST.get('return_reason') or 'defective').strip()
    if return_reason not in dict(PartOrder.RETURN_REASON_CHOICES):
        return JsonResponse({'error': 'اختار نوع مشكلة الإرجاع.'}, status=400)

    updated = PartOrder.objects.filter(pk=order.pk, status='delivered').update(
        status='refund_requested', refund_reason=reason, return_reason=return_reason,
    )
    if not updated:
        return JsonResponse({'error': 'الإرجاع متاح فقط خلال فترة الضمان.'}, status=400)

    if order.listing.seller_customer_id:
        CustomerNotification.objects.create(
            customer=order.listing.seller_customer,
            title='⚠️ طلب إرجاع جديد',
            body=f'المشتري طلب إرجاع «{order.listing.title}». السبب: {reason[:120]}',
            level='warning', icon='fa-rotate-left',
            action_url='/marketplace/parts/sales/', action_label='عرض الطلب',
        )
    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='parts_market',
        user_name=customer.full_name,
        description=f"⚠️ طلب إرجاع للطلب {order.order_code}: {reason[:80]}",
    )
    return JsonResponse({'ok': True, 'message': 'تم تسجيل طلب الإرجاع. هنتواصل معك قريباً.'})


@require_POST
def parts_cancel_order(request, order_code):
    """Buyer abandons an unpaid checkout → the listing goes back on sale."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.buyer_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    from clients.models import ManualPaymentReceipt
    if ManualPaymentReceipt.objects.filter(
        purchase_type='parts', purchase_id=order.pk, status='pending',
    ).exclude(txn_reference='').exists():
        return JsonResponse({
            'error': 'رفعت إيصال تحويل وهو قيد المراجعة — تواصل مع الدعم لو عايز تلغي.',
        }, status=400)
    if not orders_svc.cancel_unpaid(order, reason='ألغاه المشتري', notify_buyer=False):
        return JsonResponse({'error': 'الطلب ده مش في انتظار الدفع — مينفعش يتلغي.'}, status=400)
    ManualPaymentReceipt.objects.filter(
        purchase_type='parts', purchase_id=order.pk, status='pending',
    ).update(status='rejected', reviewed_at=timezone.now(), review_notes='ألغى المشتري الطلب')
    return JsonResponse({'ok': True, 'message': 'تم إلغاء الطلب.'})


@require_POST
def parts_rate_order(request, order_code):
    """
    ⭐ Buyer rates the seller once the part was received. Ratings show on
    every listing of that seller — the trust signal the market was missing.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(PartOrder.objects.select_related('listing'), order_code=order_code)
    if order.buyer_customer_id != customer.pk:
        return JsonResponse({'error': 'not your order'}, status=403)
    if order.status not in ('delivered', 'released') or not order.delivered_at:
        return JsonResponse({'error': 'التقييم متاح بعد ما تستلم القطعة.'}, status=400)
    rating = _parse_int(request.POST.get('rating'), 0)
    if not 1 <= rating <= 5:
        return JsonResponse({'error': 'التقييم لازم من 1 لـ 5.'}, status=400)
    review = (request.POST.get('review') or '').strip()[:1000]
    updated = PartOrder.objects.filter(pk=order.pk, buyer_rating__isnull=True).update(
        buyer_rating=rating, buyer_review=review, rated_at=timezone.now(),
    )
    if not updated:
        return JsonResponse({'error': 'قيّمت الطلب ده قبل كده.'}, status=400)
    if order.listing.seller_customer_id:
        CustomerNotification.objects.create(
            customer=order.listing.seller_customer,
            title=f'⭐ المشتري قيّمك {rating}/5',
            body=f'على «{order.listing.title[:60]}»' + (f': {review[:150]}' if review else '.'),
            level='info', icon='fa-star',
            action_url='/marketplace/parts/sales/', action_label='مبيعاتي',
        )
    return JsonResponse({'ok': True, 'message': 'شكراً لتقييمك!'})


def parts_wallet(request):
    """
    💸 Seller / buyer wallet: money still in escrow, money released and
    waiting for the transfer, transfers already sent — plus where to send it.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/wallet/')
    from clients.models import MarketplacePayout
    from marketplace_b2b.services.payouts import wallet_summary

    if request.method == 'POST':
        method = (request.POST.get('payout_method') or '').strip()
        account = (request.POST.get('payout_account') or '').strip()[:100]
        name = (request.POST.get('payout_account_name') or '').strip()[:120]
        if method not in dict(MarketplaceCustomer.PAYOUT_METHOD_CHOICES):
            return JsonResponse({'error': 'اختار وسيلة استلام الفلوس.'}, status=400)
        digits = ''.join(ch for ch in account if ch.isdigit())
        if method == 'vodafone_cash' and not (len(digits) == 11 and digits.startswith('01')):
            return JsonResponse({'error': 'رقم فودافون كاش لازم 11 رقم ويبدأ بـ 01.'}, status=400)
        if method == 'instapay' and len(account) < 5:
            return JsonResponse({'error': 'اكتب عنوان إنستاباي (مثال: name@instapay) أو رقم الموبايل.'}, status=400)
        if method == 'bank_transfer' and len(account.replace(' ', '')) < 10:
            return JsonResponse({'error': 'اكتب رقم الحساب أو الـ IBAN كامل.'}, status=400)
        if len(name) < 3:
            return JsonResponse({'error': 'اكتب اسم صاحب الحساب زي ما هو في البنك / المحفظة.'}, status=400)
        MarketplaceCustomer.objects.filter(pk=customer.pk).update(
            payout_method=method, payout_account=account, payout_account_name=name,
        )
        return JsonResponse({'ok': True, 'message': 'تم حفظ وسيلة استلام الفلوس.'})

    payouts = (
        MarketplacePayout.objects.filter(customer=customer)
        .select_related('order', 'order__listing').order_by('-created_at')[:50]
    )
    return render(request, 'clients/marketplace/parts_wallet.html', {
        'customer': customer,
        'summary': wallet_summary(customer=customer),
        'payouts': payouts,
        'payout_methods': MarketplaceCustomer.PAYOUT_METHOD_CHOICES,
    })
