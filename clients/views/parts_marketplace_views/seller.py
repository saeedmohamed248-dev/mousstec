"""
🚗 سوق قطع الغيار P2P — أدوات البايع: دليل الأسعار، إعلاناتي، سحب/تعديل إعلان، المتابعة ❤ والمحفوظات.
"""
from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from clients.models import (
    CustomerNotification,
    PartCarMake,
    PartListing,
    PartWantedOffer,
    PartWantedRequest,
)
from clients.views._shared import _marketplace_auth
from erp_core.localization import current_tenant_symbol as _sym

from clients.views._parts_helpers import (
    _parse_int,
)
from clients.views.parts_marketplace_views.listings import _clean_listing_post

logger = logging.getLogger('mouss_tec_core')


def parts_price_guide(request):
    """GET ?make=<id>&title=&condition=&part_number= → realistic price range (JSON)."""
    from marketplace_b2b.services.matching import price_guide
    guide = price_guide(
        car_make_id=_parse_int(request.GET.get('make'), 0),
        title=(request.GET.get('title') or '')[:200],
        condition=(request.GET.get('condition') or '').strip()[:20],
        part_number=(request.GET.get('part_number') or '')[:120],
    )
    if guide is None:
        return JsonResponse({'ok': True, 'guide': None})
    return JsonResponse({'ok': True, 'guide': {k: str(v) for k, v in guide.items()}})


# ─────────────────────────────────────────────────────────────────────
# 4a. SELLER TOOLS — my listings + withdraw
# ─────────────────────────────────────────────────────────────────────

def parts_my_listings(request):
    """
    Seller's own listings with their moderation state.
    🐛 [FIX]: البائع بعد ما يعرض قطعة ماكانش ليه أي صفحة يشوف فيها هل
       اتقبلت ولا اترفضت (وسبب الرفض)، ولا يقدر يسحب قطعة اتباعت برّه المنصة.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/my-listings/')
    listings = list(
        PartListing.objects.filter(seller_customer=customer, is_deleted=False)
        .select_related('car_make', 'reserved_for')
        .prefetch_related('photos')
        .order_by('-created_at')[:100]
    )
    for l in listings:
        # Prefill for the edit modal. Rendered with autoescape inside an HTML
        # attribute: the browser decodes the entities back before JS runs.
        l.edit_json = json.dumps({
            'code': str(l.listing_code), 'title': l.title, 'description': l.description,
            'car_make': l.car_make_id, 'car_model': l.car_model,
            'car_year_from': l.car_year_from, 'car_year_to': l.car_year_to,
            'engine_code': l.engine_code, 'part_number': l.part_number,
            'condition': l.condition, 'city': l.city,
            'price_egp': str(l.price_egp), 'warranty_days': l.warranty_days,
        }, ensure_ascii=False)
    return render(request, 'clients/marketplace/parts_my_listings.html', {
        'customer': customer,
        'listings': listings,
        'makes': PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name'),
        'condition_choices': PartListing.CONDITION_CHOICES,
    })


@require_POST
def parts_listing_withdraw(request, listing_code):
    """Seller withdraws an unsold listing (sold elsewhere / changed mind)."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    with transaction.atomic():
        listing = get_object_or_404(
            PartListing.objects.select_for_update(),
            listing_code=listing_code, is_deleted=False,
        )
        if listing.seller_customer_id != customer.pk:
            return JsonResponse({'error': 'not your listing'}, status=403)
        if listing.status not in ('draft', 'active'):
            return JsonResponse({
                'error': 'مينفعش تسحب قطعة محجوزة أو اتباعت — فيه طلب شراء عليها.',
            }, status=400)
        listing.status = 'removed'
        listing.save(update_fields=['status', 'updated_at'])
        # A private wanted-offer listing: the buyer's accepted offer is void.
        if listing.reserved_for_id:
            # 🐛 [FIX]: الطلب كان بيرجع «مفتوح» لكن كل العروض التانية كانت اترفضت
            #    وقت القبول — المشتري يلاقي طلبه فاضي ومحدش يبلّغه. دلوقتي العروض
            #    المنافسة بترجع تاني قيد الانتظار والمشتري بيوصله إشعار.
            reqs = list(PartWantedRequest.objects.select_for_update().filter(
                offers__linked_listing=listing, status='matched',
            ))
            PartWantedOffer.objects.filter(linked_listing=listing, status='accepted').update(status='withdrawn')
            for req in reqs:
                req.status = 'open'
                req.save(update_fields=['status'])
                PartWantedOffer.objects.filter(request=req, status='rejected').update(status='pending')
                if req.buyer_customer_id:
                    CustomerNotification.objects.create(
                        customer_id=req.buyer_customer_id,
                        title=f'↩️ البائع سحب عرضه على «{req.part_name[:60]}»',
                        body='طلبك رجع مفتوح — تقدر تختار عرض تاني أو تستنى عروض جديدة.',
                        level='warning', icon='fa-rotate-left',
                        action_url='/marketplace/parts/wanted/mine/', action_label='طلباتي',
                    )
    return JsonResponse({'ok': True, 'message': 'تم سحب القطعة من السوق.'})


_CONTENT_FIELDS = ('car_make', 'title', 'description', 'condition', 'car_model',
                   'car_year_from', 'car_year_to', 'engine_code', 'part_number')


@require_POST
def parts_listing_edit(request, listing_code):
    """
    ✏️ Seller edits their own listing.

    * Price / warranty / city changes on a live listing apply immediately.
    * Changing what the part *is* (title, description, fitment…) — or fixing
      a rejected listing — sends it back to admin review, hidden meanwhile.
    * A lower price notifies every buyer who saved (❤) the listing.
    🐛 [FIX]: كان مفيش أي تعديل من البائع — القطعة المرفوضة كانت نهاية الطريق
       (لازم يعيد رفعها من الأول بالصور)، وماكانش ينفع حتى يقلل السعر.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    data, err = _clean_listing_post(request)
    if err:
        return err
    with transaction.atomic():
        listing = get_object_or_404(
            PartListing.objects.select_for_update(),
            listing_code=listing_code, is_deleted=False,
        )
        if listing.seller_customer_id != customer.pk:
            return JsonResponse({'error': 'not your listing'}, status=403)
        if listing.status not in ('draft', 'active'):
            return JsonResponse({'error': 'مينفعش تعدّل قطعة محجوزة أو اتباعت.'}, status=400)
        if listing.moderation_status == 'suspended':
            return JsonResponse({'error': 'القطعة معلّقة من الإدارة — تواصل مع الدعم.'}, status=400)
        if listing.reserved_for_id:
            return JsonResponse({'error': 'دي قطعة خاصة باتفاق مع مشتري — مينفعش تتعدّل.'}, status=400)

        old_price = listing.price_egp
        content_changed = any(getattr(listing, f) != data[f] for f in _CONTENT_FIELDS)
        for field, value in data.items():
            setattr(listing, field, value)
        needs_review = content_changed or listing.moderation_status == 'rejected'
        if needs_review:
            listing.moderation_status = 'pending_approval'
            listing.status = 'draft'
            listing.rejection_reason = ''
        listing.save()

    price_dropped = (not needs_review and listing.is_publicly_visible
                     and listing.price_egp < old_price)
    if price_dropped:
        _notify_price_drop(listing, old_price)
    return JsonResponse({
        'ok': True,
        'message': ('اتحفظ التعديل — القطعة رجعت لمراجعة الإدارة قبل ما تظهر تاني.'
                    if needs_review else 'اتحفظ التعديل.'),
        'moderation_status': listing.moderation_status,
    })


def _notify_price_drop(listing, old_price):
    from clients.models import PartListingWatch
    watches = (PartListingWatch.objects.filter(listing=listing)
               .select_related('customer')[:500])
    for w in watches:
        CustomerNotification.objects.create(
            customer=w.customer,
            title='📉 السعر نزل على قطعة حافظها',
            body=f'«{listing.title[:80]}» بقت {listing.price_egp} بدل {old_price} {_sym()}.',
            level='success', icon='fa-tag',
            action_url=f'/marketplace/parts/{listing.listing_code}/', action_label='شوف القطعة',
        )


@require_POST
def parts_listing_watch(request, listing_code):
    """❤ Toggle saving a listing (price-drop alerts)."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)
    from clients.models import PartListingWatch
    listing = get_object_or_404(PartListing, listing_code=listing_code, is_deleted=False)
    if listing.seller_customer_id == customer.pk:
        return JsonResponse({'error': 'دي قطعتك.'}, status=400)
    existing = PartListingWatch.objects.filter(customer=customer, listing=listing)
    if existing.exists():
        existing.delete()
        return JsonResponse({'ok': True, 'watching': False, 'message': 'اتشالت من المحفوظات.'})
    if not listing.is_publicly_visible:
        return JsonResponse({'error': 'القطعة مش معروضة حالياً.'}, status=400)
    try:
        PartListingWatch.objects.create(customer=customer, listing=listing,
                                        price_when_saved=listing.price_egp)
    except IntegrityError:
        pass
    return JsonResponse({'ok': True, 'watching': True,
                         'message': 'اتحفظت — هنبلّغك لو السعر نزل.'})


def parts_saved(request):
    """❤ Buyer's saved listings."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/saved/')
    from clients.models import PartListingWatch
    watches = (PartListingWatch.objects.filter(customer=customer, listing__is_deleted=False)
               .select_related('listing', 'listing__car_make')
               .prefetch_related('listing__photos')[:100])
    return render(request, 'clients/marketplace/parts_saved.html', {
        'customer': customer, 'watches': watches,
    })
