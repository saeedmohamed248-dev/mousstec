"""
🚗 سوق قطع الغيار P2P — النزاعات (خلال ٣ أيام) وفك الضمان التلقائي `auto_release_expired_warranties`.
"""
from __future__ import annotations

import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from clients.models import (
    CustomerNotification,
    PartOrder,
)
from clients.views._shared import _marketplace_auth

from clients.views.parts_marketplace_views.listings import _validate_photo

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 4c. DISPUTES — buyer or seller opens a ticket within the 3-day window
# ─────────────────────────────────────────────────────────────────────

def parts_open_dispute(request, order_code):
    """Buyer or seller opens a dispute on their own order."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'يجب تسجيل الدخول.'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    from clients.services.disputes import open_dispute
    from django.core.exceptions import ValidationError as DjVE, PermissionDenied

    order = get_object_or_404(PartOrder, order_code=order_code)
    # Decide role automatically based on which side this customer is on.
    if order.buyer_customer_id == customer.pk:
        role = 'buyer'
    elif order.listing.seller_customer_id == customer.pk:
        role = 'seller'
    else:
        return JsonResponse({'error': 'هذا الطلب ليس لك.'}, status=403)

    evidence = request.FILES.getlist('evidence')[:5]
    for photo in evidence:
        err = _validate_photo(photo)
        if err:
            return JsonResponse({'error': err}, status=400)

    try:
        ticket = open_dispute(
            order=order, opener=customer, opener_role=role,
            category=(request.POST.get('category') or '').strip(),
            description=(request.POST.get('description') or '').strip()[:5000],
        )
    except PermissionDenied as exc:
        return JsonResponse({'error': str(exc)}, status=403)
    except DjVE as exc:
        return JsonResponse({'error': '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc)}, status=400)

    from clients.models import DisputeEvidence
    for photo in evidence:
        DisputeEvidence.objects.create(ticket=ticket, image=photo, uploaded_by_role=role)

    # Tell the other side — until now they only found out when money froze.
    other = order.listing.seller_customer if role == 'buyer' else order.buyer_customer
    if other is not None:
        CustomerNotification.objects.create(
            customer=other,
            title=f'⚖️ تم فتح نزاع على «{order.listing.title[:60]}»',
            body=(f'{"المشتري" if role == "buyer" else "البائع"} فتح نزاع '
                  f'({ticket.get_category_display()}). المبلغ مجمّد لحين قرار الإدارة — '
                  f'فريق الدعم هيتواصل معاك.'),
            level='warning', icon='fa-scale-balanced',
            action_url='/marketplace/parts/sales/' if role == 'buyer' else '/marketplace/parts/orders/',
            action_label='تفاصيل الطلب',
        )

    return JsonResponse({
        'ok': True,
        'ticket_code': str(ticket.ticket_code),
        'status': ticket.status,
        'message': 'تم فتح النزاع. المبلغ مجمّد لحين قرار الإدارة.',
    })


# ─────────────────────────────────────────────────────────────────────
# 5. BACKGROUND — auto-release escrow after warranty expires
# ─────────────────────────────────────────────────────────────────────

def auto_release_expired_warranties():
    """
    Run periodically (cron / celery beat). Releases escrow for delivered
    orders whose warranty window has passed. Returns count released.
    """
    now = timezone.now()
    qs = PartOrder.objects.filter(status='delivered', warranty_ends_at__lt=now)
    n = 0
    for order in qs.select_related('listing', 'listing__seller_customer')[:200]:
        if order.release_to_seller():
            n += 1
    if n:
        logger.info("[PARTS] Auto-released %d order(s) from escrow", n)
    return n
