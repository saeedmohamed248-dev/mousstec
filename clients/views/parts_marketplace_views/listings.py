"""
🚗 سوق قطع الغيار P2P — صفحة القطعة، رفع إعلان جديد (فحص الصور + تنظيف الفورم)، وتقييم البايع.
"""
from __future__ import annotations

import logging

from django.db import connection, transaction
from django.db.models import Count, F
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from clients.models import (
    PartCarMake,
    PartListing,
    PartListingPhoto,
    PartOrder,
    PlatformEvent,
)
from clients.views._shared import _marketplace_auth
from erp_core.localization import current_tenant_symbol as _sym

from clients.views._parts_helpers import (
    _parse_int, _parse_money,
)

logger = logging.getLogger('mouss_tec_core')


MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 MB لكل صورة إعلان


def _validate_photo(photo):
    """Return an error message for an unacceptable upload, else None."""
    if photo.size > MAX_PHOTO_BYTES:
        return 'حجم كل صورة لازم يكون أقل من 8 ميجا.'
    if not (getattr(photo, 'content_type', '') or '').startswith('image/'):
        return 'الملفات المرفوعة لازم تكون صور (JPG / PNG / WEBP).'
    return None


# ─────────────────────────────────────────────────────────────────────
# 1. PUBLIC DETAIL — الفيد نفسه في parts_feed_views.py
# ─────────────────────────────────────────────────────────────────────

def parts_detail(request, listing_code):
    """Public detail page of a listing."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access from main site only')

    listing = get_object_or_404(
        PartListing.objects.select_related('car_make', 'seller_customer', 'seller_tenant')
                            .prefetch_related('photos'),
        listing_code=listing_code,
    )
    customer = _marketplace_auth(request)
    is_owner = bool(customer and listing.seller_customer_id == customer.pk)
    is_buyer = bool(customer and PartOrder.objects.filter(
        listing=listing, buyer_customer=customer,
    ).exists())

    if listing.is_deleted:
        return HttpResponseForbidden('This listing is unavailable.')
    # Sellers may always preview their own listing (draft / pending / rejected /
    # withdrawn) — that's how they check what the admin sees. Buyers keep
    # access to what they bought.
    if not (is_owner or is_buyer):
        if listing.status not in ('active', 'reserved', 'sold'):
            return HttpResponseForbidden('This listing is unavailable.')
        # Hide pending/rejected/suspended listings from the public.
        if listing.moderation_status != 'approved':
            return HttpResponseForbidden('This listing is awaiting admin approval.')
        # A listing created for one buyer's wanted request is private to them.
        if listing.reserved_for_id and not (customer and listing.reserved_for_id == customer.pk):
            return HttpResponseForbidden('This listing is reserved for another buyer.')

    # Count view — atomic F() so concurrent views don't overwrite each other.
    if not is_owner:
        PartListing.objects.filter(pk=listing.pk).update(views_count=F('views_count') + 1)

    # Masked seller view by default. Reveal only if the viewer has an
    # escrow-funded order on this listing OR is the seller themselves.
    from clients.services.trust import contact_view
    reveal_order = None
    if customer and listing.seller_customer_id and not is_owner:
        reveal_order = (
            PartOrder.objects.filter(
                listing=listing,
                buyer_customer=customer,
                status__in=['paid_held', 'shipped', 'delivered', 'released',
                            'refund_requested', 'refunded', 'disputed'],
            )
            .order_by('-created_at').first()
        )
    seller_contact = contact_view(
        listing.seller_customer, viewer=customer, order=reveal_order,
    ) if listing.seller_customer_id else None

    return render(request, 'clients/marketplace/parts_detail.html', {
        'listing': listing,
        'photos': list(listing.photos.all()),
        'customer': customer,
        'is_owner': is_owner,
        'seller_rating': seller_rating_summary(listing),
        'is_watching': bool(customer and listing.watches.filter(customer=customer).exists()),
        'can_buy': listing.can_be_bought_by(customer) if customer else False,
        'seller_contact': seller_contact,
        # 🤝 الضمان اختياري: الشركات (تجار/ورش/مطابع على المنصة) بياناتها التجارية عامة —
        #    المشتري يقدر يكلّمهم مباشرة ويتفق من غير ضمان المنصة.
        'company_contact': _company_contact(listing),
    })


def _company_contact(listing):
    if not listing.seller_tenant_id:
        return None
    phone = ''.join(ch for ch in (listing.seller_tenant.phone or '') if ch.isdigit())
    if not phone:
        return None
    wa = ('2' + phone) if phone.startswith('01') and len(phone) == 11 else phone
    return {'phone': listing.seller_tenant.phone, 'whatsapp': wa}


def seller_rating_summary(listing):
    """{'avg': Decimal, 'count': int} of buyer ratings for this listing's seller."""
    from django.db.models import Avg
    qs = PartOrder.objects.filter(buyer_rating__isnull=False)
    if listing.seller_customer_id:
        qs = qs.filter(listing__seller_customer_id=listing.seller_customer_id)
    elif listing.seller_tenant_id:
        qs = qs.filter(listing__seller_tenant_id=listing.seller_tenant_id)
    else:
        return {'avg': None, 'count': 0}
    agg = qs.aggregate(avg=Avg('buyer_rating'), count=Count('id'))
    return {'avg': agg['avg'], 'count': agg['count'] or 0}


# ─────────────────────────────────────────────────────────────────────
# 2. SELLER FLOW — list a part for sale (customer-side, simplest path)
# ─────────────────────────────────────────────────────────────────────

def _clean_listing_post(request, *, default_city=''):
    """
    Validate the listing fields of a sell / edit form.
    Returns (data, None) or (None, JsonResponse error). Shared by create
    and seller edit so both enforce the same rules.
    """
    # 🐛 [FIX]: أي قيمة غير رقمية (سنة/ضمان/ماركة) كانت بترمي exception
    #    وترجع 500 بنص الخطأ الداخلي للعميل، والحالة ماكانتش بتتحقق.
    make = PartCarMake.objects.filter(
        pk=_parse_int(request.POST.get('car_make'), 0), is_active=True,
    ).first()
    if make is None:
        return None, JsonResponse({'error': 'اختار ماركة العربية.'}, status=400)
    title = (request.POST.get('title') or '').strip()[:200]
    if len(title) < 3:
        return None, JsonResponse({'error': 'اكتب اسم القطعة (3 حروف على الأقل).'}, status=400)
    description = (request.POST.get('description') or '').strip()[:5000]
    if len(description) < 10:
        return None, JsonResponse({'error': 'اكتب وصف للقطعة وحالتها (10 حروف على الأقل).'}, status=400)
    price = _parse_money(request.POST.get('price_egp'))
    if price is None:
        return None, JsonResponse({'error': 'السعر يجب أن يكون أكبر من صفر.'}, status=400)
    warranty = _parse_int(request.POST.get('warranty_days') or 3)
    if warranty is None or warranty < 1 or warranty > 90:
        return None, JsonResponse({'error': 'فترة الضمان لازم بين 1 و 90 يوم.'}, status=400)
    condition = (request.POST.get('condition') or 'used_good').strip()
    if condition not in dict(PartListing.CONDITION_CHOICES):
        return None, JsonResponse({'error': 'حالة القطعة غير صالحة.'}, status=400)

    max_year = timezone.now().year + 1
    year_from = _parse_int(request.POST.get('car_year_from')) if request.POST.get('car_year_from') else None
    year_to = _parse_int(request.POST.get('car_year_to')) if request.POST.get('car_year_to') else None
    for y in (year_from, year_to):
        if y is not None and not (1950 <= y <= max_year):
            return None, JsonResponse({'error': f'سنة الموديل لازم بين 1950 و {max_year}.'}, status=400)
    if (request.POST.get('car_year_from') and year_from is None) or \
            (request.POST.get('car_year_to') and year_to is None):
        return None, JsonResponse({'error': 'سنة الموديل لازم تكون رقم.'}, status=400)
    if year_from and year_to and year_from > year_to:
        year_from, year_to = year_to, year_from

    return {
        'car_make': make,
        'title': title,
        'description': description,
        'price_egp': price,
        'warranty_days': warranty,
        'condition': condition,
        'car_model': (request.POST.get('car_model') or '').strip()[:100],
        'car_year_from': year_from,
        'car_year_to': year_to,
        'engine_code': (request.POST.get('engine_code') or '').strip().upper()[:30],
        'part_number': (request.POST.get('part_number') or '').strip()[:120],
        'city': (request.POST.get('city') or default_city or '').strip()[:100],
    }, None


def create_listing_from_post(request, *, seller_customer=None, seller_tenant=None,
                             default_city='', seller_label=''):
    """
    Validate a "sell a part" form and create the pending listing + photos.
    Shared by customer sellers (/marketplace/parts/sell/) and merchant
    (tenant) sellers (/marketplace/merchant/parts/). Returns (listing, error).
    """
    data, err = _clean_listing_post(request, default_city=default_city)
    if err:
        return None, err
    make, price = data['car_make'], data['price_egp']

    photos = request.FILES.getlist('photos')
    if len(photos) < 3:
        return None, JsonResponse({
            'error': 'لازم ترفع 3 صور على الأقل لتوثيق حالة القطعة من كل الزوايا.'
        }, status=400)
    photos = photos[:10]  # cap at 10 photos
    for photo in photos:
        err = _validate_photo(photo)
        if err:
            return None, JsonResponse({'error': err}, status=400)

    try:
        with transaction.atomic():
            # Listings stay in `draft` + `pending_approval` until a
            # Super Admin reviews them; approval flips status→active.
            listing = PartListing.objects.create(
                seller_customer=seller_customer,
                seller_tenant=seller_tenant,
                status='draft',
                moderation_status='pending_approval',
                **data,
            )
            for idx, photo in enumerate(photos):
                PartListingPhoto.objects.create(
                    listing=listing, image=photo,
                    is_primary=(idx == 0), sort_order=idx,
                )

            PlatformEvent.objects.create(
                event_type='other', tenant_schema='public', tenant_name='parts_market',
                user_name=seller_label,
                description=f"🛒 قطعة جديدة معروضة: «{listing.title}» — {make.name} — {price} {_sym()}",
            )
    except Exception as exc:
        logger.exception("[PARTS] Failed to create listing: %s", exc)
        return None, JsonResponse({'error': 'فشل النشر. حاول مرة أخرى.'}, status=500)
    return listing, None


def parts_create(request):
    """Form to create a new listing as a marketplace customer."""
    customer = _marketplace_auth(request)
    if not customer:
        # 🐛 [FIX]: كان بيودّي لـ /marketplace/login/ من غير ?next — اللي داخل يبيع حاجته
        #    بعد التسجيل كان بيلاقي نفسه في اللوحة ويتوه. غالباً عميل جديد ⇒ صفحة «حساب جديد».
        return redirect('/marketplace/automotive/?next=/marketplace/parts/sell/')
    if customer.sector != 'automotive':
        return render(request, 'clients/marketplace/parts_unavailable.html', {
            'reason': 'سوق قطع الغيار خاص بقطاع السيارات فقط.',
        }, status=403)

    makes = PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name')

    if request.method == 'POST':
        listing, err = create_listing_from_post(
            request, seller_customer=customer,
            default_city=customer.city or '', seller_label=customer.full_name,
        )
        if err:
            return err
        return JsonResponse({
            'ok': True,
            'message': 'تم استلام القطعة وهي الآن في انتظار موافقة الإدارة قبل النشر.',
            'listing_code': str(listing.listing_code),
            'detail_url': f'/marketplace/parts/{listing.listing_code}/',
            'my_listings_url': '/marketplace/parts/my-listings/',
            'moderation_status': 'pending_approval',
        })

    from django.conf import settings as _s
    return render(request, 'clients/marketplace/parts_create.html', {
        'customer': customer,
        'makes': makes,
        'condition_choices': PartListing.CONDITION_CHOICES,
        # 🛒 النسبة من الإعدادات (كانت 8% مكتوبة في القالب والجافاسكريبت)
        'commission_pct': str(getattr(_s, 'MARKETPLACE_PARTS_COMMISSION_INDIVIDUAL_PCT', '8')),
    })
