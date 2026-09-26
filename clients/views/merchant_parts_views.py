"""
🏪 Merchant (tenant) side of the P2P parts marketplace.

The parts market was designed for two kinds of sellers — marketplace
customers (8% commission) and merchant companies / tenants (4%) — and
``PartListing.seller_tenant`` has existed since day one, but there was no
screen a merchant could use to list a part, see who bought it, or mark it
shipped. This module closes that loop from the merchant's own subdomain:

  /marketplace/merchant/parts/                         listings + sales
  /marketplace/merchant/parts/create/                  POST new listing
  /marketplace/merchant/parts/<code>/withdraw/         POST withdraw
  /marketplace/merchant/parts/order/<code>/shipped/    POST mark shipped
  /marketplace/merchant/parts/order/<code>/dispute/    POST open dispute

Listings go through the same admin moderation queue as customer listings,
and buyers check out through the same escrow flow on the main site.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models import (
    Client,
    CustomerNotification,
    DisputeTicket,
    PartCarMake,
    PartListing,
    PartOrder,
)
from clients.views.parts_marketplace_views import (
    _validate_photo,
    create_listing_from_post,
)

logger = logging.getLogger('mouss_tec_core')


def _merchant_tenant(request):
    """The logged-in merchant's tenant, or None (public schema / anonymous)."""
    if not request.user.is_authenticated:
        return None
    schema = getattr(connection, 'schema_name', 'public')
    if schema == 'public':
        return None
    return Client.objects.filter(schema_name=schema).first()


def _public_parts_url(path=''):
    """Absolute URL of a page on the main marketplace domain."""
    base = getattr(settings, 'BASE_DOMAIN', '') or 'mousstec.com'
    scheme = 'http' if settings.DEBUG else 'https'
    return f'{scheme}://{base}/marketplace/parts/{path}'


def merchant_parts_home(request):
    tenant = _merchant_tenant(request)
    if tenant is None:
        return redirect('/secure-portal/')
    listings = (
        PartListing.objects.filter(seller_tenant=tenant, is_deleted=False)
        .select_related('car_make').prefetch_related('photos')
        .order_by('-created_at')[:100]
    )
    orders = list(
        PartOrder.objects.filter(listing__seller_tenant=tenant)
        .exclude(status__in=('pending_payment', 'cancelled'))
        .select_related('listing', 'listing__car_make')
        .order_by('-created_at')[:100]
    )
    open_tickets = set(
        DisputeTicket.objects.filter(
            order_id__in=[o.pk for o in orders], status__in=('open', 'under_review'),
            is_deleted=False,
        ).values_list('order_id', flat=True)
    )
    for o in orders:
        o.can_dispute = o.pk not in open_tickets and DisputeTicket.is_within_window(o)
    cats = dict(DisputeTicket.CATEGORY_CHOICES)
    return render(request, 'clients/marketplace/merchant_parts.html', {
        'tenant': tenant,
        'listings': listings,
        'orders': orders,
        'makes': PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name'),
        'condition_choices': PartListing.CONDITION_CHOICES,
        'dispute_choices': [(c, cats[c]) for c in ('buyer_misuse', 'payment_issue', 'other')],
        'public_parts_url': _public_parts_url(),
        'is_automotive': tenant.industry == 'automotive',
    })


@require_POST
def merchant_parts_create(request):
    tenant = _merchant_tenant(request)
    if tenant is None:
        return JsonResponse({'error': 'سجل دخول بحساب الشركة.'}, status=401)
    if tenant.industry != 'automotive':
        return JsonResponse({'error': 'سوق قطع الغيار لشركات قطاع السيارات بس.'}, status=403)
    if getattr(tenant, 'is_fraud_flagged', False):
        return JsonResponse({'error': 'الحساب موقوف عن البيع في السوق — تواصل مع الدعم.'}, status=403)
    listing, err = create_listing_from_post(
        request, seller_tenant=tenant,
        default_city=getattr(tenant, 'city', '') or '',
        seller_label=tenant.name,
    )
    if err:
        return err
    return JsonResponse({
        'ok': True,
        'message': 'تم استلام القطعة — هتظهر في السوق بعد موافقة الإدارة.',
        'listing_code': str(listing.listing_code),
    })


@require_POST
def merchant_parts_withdraw(request, listing_code):
    tenant = _merchant_tenant(request)
    if tenant is None:
        return JsonResponse({'error': 'unauth'}, status=401)
    with transaction.atomic():
        listing = get_object_or_404(
            PartListing.objects.select_for_update(),
            listing_code=listing_code, seller_tenant=tenant, is_deleted=False,
        )
        if listing.status not in ('draft', 'active'):
            return JsonResponse({'error': 'القطعة محجوزة أو اتباعت — مينفعش تتسحب.'}, status=400)
        listing.status = 'removed'
        listing.save(update_fields=['status', 'updated_at'])
    return JsonResponse({'ok': True, 'message': 'تم سحب القطعة من السوق.'})


@require_POST
def merchant_parts_mark_shipped(request, order_code):
    tenant = _merchant_tenant(request)
    if tenant is None:
        return JsonResponse({'error': 'unauth'}, status=401)
    order = get_object_or_404(
        PartOrder.objects.select_related('listing', 'buyer_customer'),
        order_code=order_code, listing__seller_tenant=tenant,
    )
    tracking = (request.POST.get('tracking') or '').strip()[:200]
    updated = PartOrder.objects.filter(pk=order.pk, status='paid_held').update(
        status='shipped', shipped_at=timezone.now(), shipping_tracking=tracking,
    )
    if not updated:
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)
    if order.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=order.buyer_customer,
            title='🚚 شُحنت قطعتك',
            body=(f'{tenant.name} شحن «{order.listing.title}».'
                  + (f' بيانات الشحن: {tracking}.' if tracking else '')
                  + f' أكد الاستلام لما توصل عشان يبدأ ضمان {order.warranty_days} يوم.'),
            level='info', icon='fa-truck',
            action_url='/marketplace/parts/orders/', action_label='تفاصيل',
        )
    return JsonResponse({'ok': True, 'message': 'تم تعليم الطلب كمشحون.'})


@require_POST
def merchant_parts_open_dispute(request, order_code):
    tenant = _merchant_tenant(request)
    if tenant is None:
        return JsonResponse({'error': 'unauth'}, status=401)
    from django.core.exceptions import PermissionDenied, ValidationError
    from clients.models import DisputeEvidence
    from clients.services.disputes import open_dispute

    order = get_object_or_404(PartOrder.objects.select_related('listing'), order_code=order_code,
                              listing__seller_tenant=tenant)
    evidence = request.FILES.getlist('evidence')[:5]
    for photo in evidence:
        err = _validate_photo(photo)
        if err:
            return JsonResponse({'error': err}, status=400)
    try:
        ticket = open_dispute(
            order=order, opener=tenant, opener_role='seller',
            category=(request.POST.get('category') or '').strip(),
            description=(request.POST.get('description') or '').strip()[:5000],
        )
    except PermissionDenied as exc:
        return JsonResponse({'error': str(exc)}, status=403)
    except ValidationError as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)
    for photo in evidence:
        DisputeEvidence.objects.create(ticket=ticket, image=photo, uploaded_by_role='seller')
    if order.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=order.buyer_customer,
            title=f'⚖️ تم فتح نزاع على «{order.listing.title[:60]}»',
            body='البائع فتح نزاع على الطلب. المبلغ مجمّد لحين قرار الإدارة — فريق الدعم هيتواصل معاك.',
            level='warning', icon='fa-scale-balanced',
            action_url='/marketplace/parts/orders/', action_label='تفاصيل الطلب',
        )
    return JsonResponse({'ok': True, 'message': 'تم فتح النزاع. المبلغ مجمّد لحين قرار الإدارة.'})
