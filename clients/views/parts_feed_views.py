"""
🛒 سوق قطع الغيار — الصفحات العامة (الفيد + صنف من مخازن التجار).

اتفصلت من parts_marketplace_views.py لما زاد عن حد حجم الملفات:
  * /marketplace/parts/              — الكل
  * /marketplace/parts/individuals/  — سوق الأفراد (عميل بيبيع حاجته اللي مش محتاجها)
  * /marketplace/parts/traders/      — سوق التجار (قطع الشركات + أصناف مخازنهم)
  * /marketplace/parts/stock/<id>/   — صنف من مخزن تاجر + «اطلب القطعة»
"""
from __future__ import annotations

import logging

from django.db import connection
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from clients.models import PartCarMake, PartListing
from clients.views._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')


def _parse_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

# ─────────────────────────────────────────────────────────────────────
# 1. PUBLIC FEED — anyone can browse, only logged-in customers can buy
# ─────────────────────────────────────────────────────────────────────

FEED_PAGE_SIZE = 48

# 🏪 أقسام السوق: «الكل» + سوق الأفراد (مستعمل/حاجات مش محتاجينها) + سوق التجار.
#    زي OLX/eBay: البائع الفرد في صفحة لوحده بعيد عن محلات قطع الغيار.
FEED_SECTIONS = ('all', 'individuals', 'traders')


def _page_number(request, param='page'):
    n = _parse_int(request.GET.get(param), 1) or 1
    return max(n, 1)


def parts_feed(request, section=None):
    """
    Public feed of active part listings, filterable by car make.

    ``section`` (من الـ URL أو ?seller=):
      * ``all``         — كل حاجة (قطع الأفراد + قطع التجار + مخازن التجار)
      * ``individuals`` — سوق الأفراد: عميل بيبيع حاجته اللي مش محتاجها
      * ``traders``     — سوق التجار: قطع الشركات + أصناف مخازنهم
    """
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access from main site only')

    if section is None:
        section = (request.GET.get('seller') or 'all').strip().lower()
    if section not in FEED_SECTIONS:
        section = 'all'

    public_q = Q(
        listings__status='active', listings__moderation_status='approved',
        listings__is_deleted=False, listings__reserved_for__isnull=True,
    )
    # عدّاد كل ماركة بيتحسب على القسم المفتوح بس.
    if section == 'individuals':
        public_q &= Q(listings__seller_customer__isnull=False)
    elif section == 'traders':
        public_q &= Q(listings__seller_tenant__isnull=False)
    # 🐛 [FIX]: عدّاد الماركة (listings_count) كان بيزيد مع كل قطعة جديدة حتى
    #    لو لسه مستنية موافقة أو اترفضت، وماكانش بيقل أبداً مع البيع —
    #    دلوقتي بيتحسب لحظياً من القطع المعروضة فعلاً.
    makes = (
        PartCarMake.objects.filter(is_active=True)
        .annotate(live_count=Count('listings', filter=public_q))
        .order_by('sort_order', 'name')
    )
    selected_make_slug = request.GET.get('make', '').strip().lower()
    q = request.GET.get('q', '').strip()[:100]
    condition = request.GET.get('condition', '').strip()
    sort = request.GET.get('sort', 'new')

    listings = PartListing.objects.filter(
        status='active', moderation_status='approved', is_deleted=False,
        reserved_for__isnull=True,  # private (wanted-offer) listings never hit the feed
    ).select_related('car_make', 'seller_tenant').prefetch_related('photos')
    if section == 'individuals':
        listings = listings.filter(seller_customer__isnull=False)
    elif section == 'traders':
        listings = listings.filter(seller_tenant__isnull=False)

    selected_make = None
    if selected_make_slug:
        selected_make = makes.filter(slug=selected_make_slug).first()
        if selected_make:
            listings = listings.filter(car_make=selected_make)
    if q:
        # 🐛 [FIX]: البحث كان على العنوان والوصف بس، رغم إن الخانة بتقول
        #    "اسم قطعة، موديل، رقم OEM".
        listings = listings.filter(
            Q(title__icontains=q) | Q(description__icontains=q)
            | Q(part_number__icontains=q) | Q(car_model__icontains=q)
            | Q(engine_code__icontains=q)
        )
    if condition and condition in dict(PartListing.CONDITION_CHOICES):
        listings = listings.filter(condition=condition)
    if sort == 'price_low':
        listings = listings.order_by('price_egp', '-created_at')
    elif sort == 'price_high':
        listings = listings.order_by('-price_egp', '-created_at')
    else:
        listings = listings.order_by('-created_at')

    # 🐛 [FIX]: السوق كان بيقطع عند أول 60 قطعة من غير صفحات — أي قطعة بعدها
    #    كانت مستخبية عن المشترين خالص. دلوقتي فيه صفحات.
    from django.core.paginator import Paginator
    listings_page = Paginator(listings, FEED_PAGE_SIZE).get_page(_page_number(request))

    # 🐛 [FIX]: أصناف الشركات (طلبات «نشر في سوق التجار») اللي الإدارة وافقت عليها
    #    ماكانتش بتظهر هنا خالص — دلوقتي ليها قسم «من مخازن التجار».
    #    (مخازن التجار مالهاش مكان في سوق الأفراد.)
    stock_page = None
    if section != 'individuals':
        from clients.services import parts_stock_orders as stock_svc
        stock_qs = stock_svc.filter_stock_items(
            stock_svc.public_stock_items(), q=q, make=selected_make,
            condition=condition if condition in dict(PartListing.CONDITION_CHOICES) else '', sort=sort,
        )
        stock_page = Paginator(stock_qs, FEED_PAGE_SIZE).get_page(_page_number(request, 'spage'))

    # روابط الصفحات بتحافظ على الفلاتر.
    from urllib.parse import urlencode
    keep = {k: v for k, v in (('make', selected_make_slug), ('q', q),
                              ('condition', condition), ('sort', sort if sort != 'new' else ''))
            if v}
    filter_qs = urlencode(keep)

    customer = _marketplace_auth(request)
    return render(request, 'clients/marketplace/parts_feed.html', {
        'section': section,
        'section_base': {'all': '/marketplace/parts/',
                         'individuals': '/marketplace/parts/individuals/',
                         'traders': '/marketplace/parts/traders/'}[section],
        'filter_qs': filter_qs,
        'stock_items': stock_page.object_list if stock_page else [],
        'stock_page': stock_page,
        'makes': makes,
        'selected_make': selected_make,
        'selected_make_slug': selected_make_slug,
        'listings': listings_page.object_list,
        'listings_page': listings_page,
        'q': q, 'condition': condition, 'sort': sort,
        'customer': customer,
        'condition_choices': PartListing.CONDITION_CHOICES,
    })


def parts_feed_individuals(request):
    """👤 سوق الأفراد — العميل بيعرض حاجته اللي مش محتاجها (مستعمل/زيادة)."""
    return parts_feed(request, section='individuals')


def parts_feed_traders(request):
    """🏪 سوق التجار — قطع الشركات ومخازنهم."""
    return parts_feed(request, section='traders')


def parts_stock_detail(request, item_id):
    """🏬 صفحة صنف من مخازن التجار + «اطلب القطعة» (فاتورة عرض سعر عند الشركة)."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access from main site only')
    from clients.services import parts_stock_orders as stock_svc
    item = get_object_or_404(stock_svc.public_stock_items(), pk=item_id)
    customer = _marketplace_auth(request)
    error = ''
    placed = None
    qty = 1
    note = ''
    if request.method == 'POST':
        if customer is None:
            from clients.views.marketplace_core_views import marketplace_login_page_url
            return redirect(marketplace_login_page_url(request, request.path))
        qty = _parse_int(request.POST.get('quantity'), 0)
        note = (request.POST.get('note') or '')[:stock_svc.MAX_NOTE_LEN]
        try:
            placed = stock_svc.place_order(item, customer, qty, note)
        except stock_svc.StockOrderError as exc:
            error = str(exc)
        except Exception:  # noqa: BLE001 — مشكلة في سيستم الشركة ماتطلعش 500 للعميل
            logger.exception("[PARTS STOCK] order failed for item %s", item.pk)
            error = 'حصلت مشكلة وإحنا بنبعت طلبك للمحل — جرّب تاني بعد شوية.'
    return render(request, 'clients/marketplace/parts_stock_detail.html', {
        'item': item, 'customer': customer, 'error': error, 'placed': placed,
        'qty': qty or 1, 'note': note,
        'max_qty': min(item.available_qty, 999),
    })
