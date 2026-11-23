"""
🔎 سوق قطع الغيار — «مطلوب قطعة» (المشتري بينشر طلب، والبيّاعين بيقدّموا عروض).

اتفصلت من parts_marketplace_views.py لما زاد عن حد حجم الملفات.
parts_marketplace_views بيعيد تصدير الدوال دي، فأي import قديم شغال زي ما هو.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models import (
    CustomerNotification,
    PartCarMake,
    PartListing,
    PartOrder,
    PartWantedOffer,
    PartWantedRequest,
)
from clients.views._shared import _marketplace_auth
from marketplace_b2b.services import parts_orders as orders_svc
from erp_core.localization import current_tenant_symbol as _sym
from clients.views._parts_helpers import _ACTIVE_ORDER_STATUSES, _parse_int, _parse_money

logger = logging.getLogger('mouss_tec_core')

# ─────────────────────────────────────────────────────────────────────
# 4b. WANTED REQUESTS — buyer posts what they need, sellers filter by fitment
# ─────────────────────────────────────────────────────────────────────

def parts_wanted_create(request):
    """Buyer posts a 'Part Wanted' request."""
    customer = _marketplace_auth(request)
    if not customer:
        # 🐛 [FIX]: كان بيودّي لتبويب «دخول» — واللي جاي من إعلان «اكتب طلبك» غالباً عميل جديد
        #    ملوش حساب. صفحة سوق السيارات بتفتح على «حساب جديد» وفيها تبويب الدخول، وبعدها يرجع هنا.
        return redirect('/marketplace/automotive/?next=/marketplace/parts/wanted/new/')
    if customer.sector != 'automotive':
        return JsonResponse({'error': 'هذا الطلب مخصص لقطاع السيارات.'}, status=403)

    if request.method != 'POST':
        return render(request, 'clients/marketplace/parts_wanted_create.html', {
            'customer': customer,
            'makes': PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name'),
        })

    # 🐛 [FIX]: ميزانية مش رقم أو سنة فاضية كانت بترجع 500 بنص الخطأ الداخلي.
    make = PartCarMake.objects.filter(
        pk=_parse_int(request.POST.get('car_make'), 0), is_active=True,
    ).first()
    if make is None:
        return JsonResponse({'error': 'اختار ماركة العربية.'}, status=400)
    year = _parse_int(request.POST.get('car_year'), 0)
    if year < 1950 or year > timezone.now().year + 1:
        return JsonResponse({'error': 'سنة الصنع غير منطقية.'}, status=400)
    model = (request.POST.get('car_model') or '').strip()
    if not model:
        return JsonResponse({'error': 'الموديل مطلوب — مثال: F30, X5, Civic.'}, status=400)
    name = (request.POST.get('part_name') or '').strip()
    if len(name) < 2:
        return JsonResponse({'error': 'اسم القطعة مطلوب.'}, status=400)
    budget_raw = (request.POST.get('max_budget_egp') or '').strip()
    budget = None
    if budget_raw:
        budget = _parse_money(budget_raw)
        if budget is None:
            return JsonResponse({'error': 'الميزانية لازم تكون رقم أكبر من صفر.'}, status=400)
    if PartWantedRequest.objects.filter(
        buyer_customer=customer, status='open', is_deleted=False,
    ).count() >= 20:
        return JsonResponse({'error': 'عندك 20 طلب مفتوح — اقفل طلبات قديمة الأول.'}, status=429)

    try:
        req = PartWantedRequest.objects.create(
            buyer_customer=customer,
            car_make=make,
            car_model=model[:100],
            car_year=year,
            engine_code=(request.POST.get('engine_code') or '').strip()[:30],
            part_name=name[:200],
            part_number_oem=(request.POST.get('part_number_oem') or '').strip()[:120],
            description=(request.POST.get('description') or '').strip()[:3000],
            max_budget_egp=budget,
        )
    except Exception as exc:
        logger.exception("[WANTED] Failed to create request: %s", exc)
        return JsonResponse({'error': 'فشل النشر. حاول مرة أخرى.'}, status=500)

    # 🎯 Instant matches: show the buyer what's already listed, and ping the
    # sellers who have it so offers arrive faster.
    matches = []
    try:
        from marketplace_b2b.services.matching import (
            listings_matching_request, notify_sellers_of_new_request,
        )
        matches = listings_matching_request(req, limit=5)
        notify_sellers_of_new_request(req)
    except Exception:
        logger.exception("[MATCH] wanted-request matching failed for %s", req.pk)
    message = 'تم نشر الطلب. هتوصلك عروض البائعين قريباً.'
    if matches:
        message = f'تم نشر الطلب — ولقينا {len(matches)} قطعة معروضة ممكن تناسبك دلوقتي.'

    # 📣 طلب قطعة اتنشر ⇒ حدث «Lead» لفيسبوك (السيرفر هنا + المتصفح بنفس الـ id في القالب)
    #    عشان حملة «سوق قطع الغيار» تتحسّن على ناس بتطلب فعلاً مش مجرد زيارات.
    pixel_event_id = f'pw-{req.request_code}'
    try:
        from clients.services import meta_capi
        meta_capi.send_contact('Lead', event_id=pixel_event_id, email=customer.email or '',
                               phone=customer.phone or '', external_id=f'mpc-{customer.pk}',
                               request=request, source_url=request.build_absolute_uri())
    except Exception:
        logger.exception("[META_CAPI] wanted-request Lead failed for %s", req.pk)

    return JsonResponse({
        'ok': True,
        'message': message,
        'pixel_event_id': pixel_event_id,
        'request_code': str(req.request_code),
        'redirect': '/marketplace/parts/wanted/mine/',
        'matches': [
            {'title': l.title, 'price': str(l.price_egp),
             'url': f'/marketplace/parts/{l.listing_code}/'} for l in matches
        ],
    })


def parts_wanted_seller_feed(request):
    """
    Seller-facing feed of open wanted requests, filterable by make/model/year/engine.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/wanted/sellers/')
    if customer.sector != 'automotive':
        return JsonResponse({'error': 'هذا السوق مخصص لقطاع السيارات.'}, status=403)

    from clients.services.fitment import open_wanted_requests

    make_slug = (request.GET.get('make') or '').strip().lower()
    model     = (request.GET.get('model') or '').strip()
    year_str  = (request.GET.get('year') or '').strip()
    engine    = (request.GET.get('engine_code') or '').strip()

    make = PartCarMake.objects.filter(slug=make_slug, is_active=True).first() if make_slug else None
    year = _parse_int(year_str) if year_str else None

    requests_qs = list(
        open_wanted_requests(
            make=make, model=model or None, year=year, engine_code=engine or None,
        ).exclude(buyer_customer=customer)  # never offer on your own request
        .annotate(offers_n=Count('offers', filter=~Q(offers__status='withdrawn')))
        .select_related('car_make')[:100]
    )
    my_offers = {
        o.request_id: o for o in PartWantedOffer.objects.filter(
            seller_customer=customer, request_id__in=[r.pk for r in requests_qs],
        )
    }
    for r in requests_qs:
        r.my_offer = my_offers.get(r.pk)

    return render(request, 'clients/marketplace/parts_wanted_seller_feed.html', {
        'customer': customer,
        'requests': requests_qs,
        'makes': PartCarMake.objects.filter(is_active=True).order_by('sort_order', 'name'),
        'condition_choices': PartListing.CONDITION_CHOICES,
        'filters': {
            'make_slug': make_slug, 'model': model,
            'year': year_str, 'engine_code': engine,
        },
    })


@require_POST
def parts_wanted_offer_submit(request, request_code):
    """
    Seller answers a wanted request with a price.
    🐛 [FIX]: الموديل PartWantedOffer كان موجود لكن مفيش أي طريقة للبائع
       يقدّم عرض — دورة "اطلب قطعة" كانت بتقف عند نشر الطلب.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)
    if customer.sector != 'automotive':
        return JsonResponse({'error': 'هذا السوق مخصص لقطاع السيارات.'}, status=403)
    req = get_object_or_404(PartWantedRequest, request_code=request_code, is_deleted=False)
    if not req.is_visible_to_sellers:
        return JsonResponse({'error': 'الطلب ده مبقاش متاح.'}, status=400)
    if req.buyer_customer_id == customer.pk:
        return JsonResponse({'error': 'مينفعش تقدّم عرض على طلبك.'}, status=400)

    price = _parse_money(request.POST.get('price_egp'))
    if price is None:
        return JsonResponse({'error': 'السعر لازم يكون أكبر من صفر.'}, status=400)
    warranty = _parse_int(request.POST.get('warranty_days') or 3)
    if warranty is None or warranty < 1 or warranty > 90:
        return JsonResponse({'error': 'فترة الضمان لازم بين 1 و 90 يوم.'}, status=400)
    condition = (request.POST.get('condition') or 'used_good').strip()
    if condition not in dict(PartListing.CONDITION_CHOICES):
        return JsonResponse({'error': 'حالة القطعة غير صالحة.'}, status=400)
    notes = (request.POST.get('notes') or '').strip()[:500]

    try:
        with transaction.atomic():
            offer, created = PartWantedOffer.objects.get_or_create(
                request=req, seller_customer=customer,
                defaults={'price_egp': price, 'condition': condition,
                          'warranty_days': warranty, 'notes': notes},
            )
            if not created:
                if offer.status not in ('pending', 'withdrawn'):
                    return JsonResponse({'error': 'عرضك على الطلب ده اتقفل بالفعل.'}, status=400)
                created = offer.status == 'withdrawn'  # re-offer counts as new for the buyer
                offer.price_egp = price
                offer.condition = condition
                offer.warranty_days = warranty
                offer.notes = notes
                offer.status = 'pending'
                offer.save(update_fields=['price_egp', 'condition', 'warranty_days', 'notes', 'status'])
    except IntegrityError:
        return JsonResponse({'error': 'قدّمت عرض على الطلب ده بالفعل.'}, status=400)

    if created and req.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=req.buyer_customer,
            title=f'💬 عرض جديد على «{req.part_name[:60]}»',
            body=f'بائع عرض {price} {_sym()} — {dict(PartListing.CONDITION_CHOICES)[condition]}، ضمان {warranty} يوم.',
            level='info', icon='fa-tags',
            action_url='/marketplace/parts/wanted/mine/', action_label='شوف العروض',
        )
    return JsonResponse({
        'ok': True,
        'message': 'تم إرسال عرضك للمشتري.' if created else 'تم تحديث عرضك.',
    })


@require_POST
def parts_wanted_offer_withdraw(request, offer_id):
    """Seller withdraws a still-pending offer."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    updated = PartWantedOffer.objects.filter(
        pk=offer_id, seller_customer=customer, status='pending',
    ).update(status='withdrawn')
    if not updated:
        return JsonResponse({'error': 'العرض مش موجود أو اتقفل.'}, status=400)
    return JsonResponse({'ok': True, 'message': 'تم سحب العرض.'})


def parts_wanted_my_requests(request):
    """Buyer's wanted requests with every offer received."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/login/?next=/marketplace/parts/wanted/mine/')
    reqs = list(
        PartWantedRequest.objects.filter(buyer_customer=customer, is_deleted=False)
        .select_related('car_make')
        .prefetch_related('offers', 'offers__seller_customer', 'offers__linked_listing')
        .order_by('-created_at')[:50]
    )
    from marketplace_b2b.services.matching import listings_matching_request
    for r in reqs:
        r.visible_offers = [o for o in r.offers.all() if o.status != 'withdrawn']
        r.accepted_offer = next((o for o in r.visible_offers if o.status == 'accepted'), None)
        r.matching_listings = listings_matching_request(r, limit=3) if r.status == 'open' else []
    return render(request, 'clients/marketplace/parts_wanted_mine.html', {
        'customer': customer,
        'wanted_requests': reqs,
    })


@require_POST
def parts_wanted_offer_accept(request, offer_id):
    """
    Buyer accepts one offer → we create a *private* listing from it (visible
    and purchasable only by this buyer), reject the competing offers and send
    the buyer to the normal escrow checkout.
    """
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)

    with transaction.atomic():
        offer = get_object_or_404(
            PartWantedOffer.objects.select_for_update().select_related('request', 'request__car_make'),
            pk=offer_id,
        )
        req = PartWantedRequest.objects.select_for_update().get(pk=offer.request_id)
        if req.buyer_customer_id != customer.pk:
            return JsonResponse({'error': 'not your request'}, status=403)
        if req.status != 'open' or req.is_deleted:
            return JsonResponse({'error': 'الطلب ده مش مفتوح.'}, status=400)
        if offer.status != 'pending':
            return JsonResponse({'error': 'العرض ده مبقاش متاح.'}, status=400)
        if not offer.seller_customer_id:
            return JsonResponse({'error': 'بائع العرض غير صالح.'}, status=400)

        listing = PartListing.objects.create(
            seller_customer_id=offer.seller_customer_id,
            title=req.part_name[:200],
            description=(
                (offer.notes or '').strip()
                or f'عرض على طلبك: {req.part_name} — {req.car_make.name} {req.car_model} {req.car_year}'
            ),
            car_make=req.car_make,
            car_model=req.car_model,
            car_year_from=req.car_year,
            car_year_to=req.car_year,
            engine_code=req.engine_code,
            part_number=req.part_number_oem,
            condition=offer.condition,
            price_egp=offer.price_egp,
            warranty_days=offer.warranty_days,
            status='active',
            # The buyer approved this exact offer for themselves; the listing
            # is private (reserved_for) so it never reaches the public feed.
            moderation_status='approved',
            moderated_at=timezone.now(),
            reserved_for=customer,
        )
        offer.status = 'accepted'
        offer.linked_listing = listing
        offer.save(update_fields=['status', 'linked_listing'])
        PartWantedOffer.objects.filter(request=req, status='pending').exclude(pk=offer.pk).update(status='rejected')
        req.status = 'matched'
        req.save(update_fields=['status'])

    CustomerNotification.objects.create(
        customer_id=offer.seller_customer_id,
        title=f'🤝 المشتري قبل عرضك على «{req.part_name[:60]}»',
        body=(f'اتعملت قطعة خاصة بالمشتري بسعر {offer.price_egp} {_sym()}. '
              f'أول ما يدفع (الفلوس في الـ Escrow) هيوصلك إشعار بعنوان الشحن.'),
        level='success', icon='fa-handshake',
        action_url='/marketplace/parts/my-listings/', action_label='قطعي',
    )
    return JsonResponse({
        'ok': True,
        'message': 'تم قبول العرض — كمّل الدفع عشان البائع يشحن.',
        'redirect': f'/marketplace/parts/{listing.listing_code}/',
    })


@require_POST
def parts_wanted_cancel(request, request_code):
    """Buyer closes their own wanted request."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'unauth'}, status=401)
    with transaction.atomic():
        req = get_object_or_404(
            PartWantedRequest.objects.select_for_update(),
            request_code=request_code, buyer_customer=customer, is_deleted=False,
        )
        if req.status not in ('open', 'matched'):
            return JsonResponse({'error': 'الطلب ده مقفول بالفعل.'}, status=400)
        accepted = req.offers.filter(status='accepted').select_related('linked_listing').first()
        if accepted and accepted.linked_listing_id:
            if PartOrder.objects.filter(
                listing_id=accepted.linked_listing_id,
                status__in=[st for st in _ACTIVE_ORDER_STATUSES if st != 'pending_payment'],
            ).exists():
                return JsonResponse({'error': 'دفعت بالفعل على العرض ده — تابعه من طلباتي.'}, status=400)
            pending = PartOrder.objects.filter(
                listing_id=accepted.linked_listing_id, status='pending_payment',
            )
            for o in pending:
                orders_svc.cancel_unpaid(o, reason='ألغى المشتري طلب القطعة', notify_buyer=False)
            PartListing.objects.filter(
                pk=accepted.linked_listing_id, status__in=('active', 'reserved'),
            ).update(status='removed')
        req.status = 'cancelled'
        req.save(update_fields=['status'])
        req.offers.filter(status__in=('pending', 'accepted')).update(status='rejected')
    return JsonResponse({'ok': True, 'message': 'تم إلغاء الطلب.'})
