"""🛍️ كتالوج السوق (متاجر Mouss Tec) — تصفّح + «اطلب سعر» + صفحة الطلبات في السوبر أدمن.

العميل مش لازم يكون مسجّل: بيكتب اسمه وموبايله. لو مسجّل في السوق بنملاهم له ونربط الطلب بحسابه.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from clients import catalog_options
from clients.catalog_seed import (
    PARTS_CATEGORIES,
    PARTS_CATEGORIES_EN,
    PRINT_CATEGORIES,
    PRINT_CATEGORIES_EN,
)
from clients.models import (
    CatalogItem,
    CatalogQuoteRequest,
    CatalogReview,
    CatalogStore,
    ServiceRequest,
)
from clients.permissions import platform_owner_required
from clients.services import catalog_request_desk as desk
from clients.services import catalog_stats
from clients.services.search_text import text_filter

from ._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')

SECTORS = {
    'printing': {
        'title': _l('سوق الطباعة والتصميم'),
        'subtitle': _l('كروت، كوبايات، أقلام، هدايا، يونيفورم، بنرات، تغليف وتصميم — اطلب سعر وإحنا نرد عليك'),
        'categories': PRINT_CATEGORIES, 'categories_en': PRINT_CATEGORIES_EN, 'icon': 'fa-palette', 'color': '#1E2A57',
    },
    'automotive': {
        'title': _l('سوق قطع الغيار'),
        'subtitle': _l('قطع لكل العربيات — صيني وكوري وياباني وألماني وفرنساوي. قول لنا عربيتك وإحنا نرد بالسعر'),
        'categories': PARTS_CATEGORIES, 'categories_en': PARTS_CATEGORIES_EN, 'icon': 'fa-gears', 'color': '#9A3412',
    },
}
PER_PAGE = 36
ATTACHMENT_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.pdf', '.ai', '.eps', '.psd', '.svg', '.cdr')
ATTACHMENT_MAX = 15 * 1024 * 1024
QUOTE_LIMIT_PER_HOUR = 15


def _notify_owner_of_quote(req_id):
    """🔔 إيميل + واتساب لصاحب المنصة بطلب السعر الجديد (نفس قنوات بلاغات الدعم).

    بيشتغل في thread منفصل عشان العميل مايستناش SMTP أو Meta. النتيجة بتتسجّل على الطلب
    (notified_email / notified_whatsapp) وبتظهر في صفحة الطلبات.
    """
    from django.conf import settings
    try:
        req = CatalogQuoteRequest.objects.select_related('item').get(pk=req_id)
        link = f"https://{getattr(settings, 'BASE_DOMAIN', 'mousstec.com')}/superadmin/catalog/requests/"
        sector = 'قطع غيار' if req.sector == 'automotive' else 'طباعة وتصميم'
        lines = [f"🛍️ طلب سعر جديد ({sector})",
                 f"المنتج: {req.item_title}", f"المتجر: {req.store_name}",
                 f"العميل: {req.name} — {req.phone}"]
        if req.quantity:
            lines.append(f"الكمية: {req.quantity}")
        if req.car_info:
            lines.append(f"العربية: {req.car_info}")
        labels = {'size': 'المقاس', 'dimensions': 'المقاس بالسنتي', 'color': 'اللون', 'shape': 'الشكل',
                  'finish': 'التشطيب'}
        picked = [f"{labels[k]}: {v}" for k, v in (req.options or {}).items() if k in labels and v]
        if picked:
            lines.append('الاختيارات: ' + ' · '.join(picked))
        if req.attachment:
            lines.append('📎 العميل رفع لوجو/تصميم — موجود في صفحة الطلبات')
        elif req.sector == 'printing':
            lines.append('⚠️ العميل ما رفعش ملف التصميم — اطلبه منه من صفحة الطلبات')
        if req.details:
            lines.append(f"التفاصيل: {req.details[:800]}")
        body = '\n'.join(lines) + f"\n\n{link}"

        email_ok = None
        to = getattr(settings, 'CATALOG_NOTIFY_EMAIL', '') or getattr(settings, 'SUPPORT_INBOX_EMAIL', '')
        if to:
            try:
                from django.core.mail import EmailMessage
                msg = EmailMessage(subject=f"[Mousstec 🛍️] طلب سعر: {req.item_title[:80]}", body=body,
                                   from_email=settings.DEFAULT_FROM_EMAIL, to=[to])
                msg.encoding = 'utf-8'
                email_ok = bool(msg.send(fail_silently=False))
            except Exception as e:  # noqa: BLE001 — الطلب اتسجّل، الإشعار مايوقفهوش
                logger.warning('[CATALOG] quote #%s email failed: %s', req.pk, e)
                email_ok = False

        wa_ok = None
        wa_to = getattr(settings, 'CATALOG_NOTIFY_WHATSAPP', '') or getattr(settings, 'SUPPORT_WHATSAPP', '')
        if wa_to:
            try:
                from clients.services.whatsapp_notify import send_platform_whatsapp
                wa_ok = bool(send_platform_whatsapp(wa_to, text=body[:1500]))
            except Exception:  # noqa: BLE001
                logger.warning('[CATALOG] quote #%s whatsapp failed', req.pk)
                wa_ok = False
        CatalogQuoteRequest.objects.filter(pk=req.pk).update(notified_email=email_ok, notified_whatsapp=wa_ok)
    except Exception:
        logger.exception('[CATALOG] notify failed for quote %s', req_id)


def notify_owner_async(req_id):
    from django.conf import settings
    if getattr(settings, 'CATALOG_NOTIFY_SYNC', False):   # الاختبارات
        _notify_owner_of_quote(req_id)
        return
    import threading

    from django.db import connection

    def _run():
        try:
            _notify_owner_of_quote(req_id)
        finally:
            connection.close()   # الـ thread ليه اتصال داتابيز لوحده — نقفله لما يخلص

    threading.Thread(target=_run, daemon=True).start()


def _with_photo_first(qs):
    """📸 ترتيب الصور: صورة عليها اسم براند (2) ⇒ صورة عادية (1) ⇒ من غير صورة (0) — والترتيب الثابت جوّه كل مجموعة."""
    return qs.annotate(has_photo=Case(
        When(Q(image__isnull=True) | Q(image=''), then=Value(0)),
        When(image__contains='-brand-', then=Value(2)),
        default=Value(1), output_field=IntegerField()))


def _is_en():
    return (get_language() or 'ar').startswith('en')


def _categories(meta):
    """[(key, label, icon)] باللغة الحالية."""
    en = _is_en()
    return [(k, meta['categories_en'].get(k, v[0]) if en else v[0], v[1]) for k, v in meta['categories'].items()]


def _category_label(meta, key):
    if _is_en() and key in meta['categories_en']:
        return meta['categories_en'][key]
    return meta['categories'].get(key, ('', ''))[0]


def _sector_or_404(sector):
    if sector not in SECTORS:
        raise Http404
    return SECTORS[sector]


def _open_customer_requests(sector):
    """📣 طلبات العملاء المفتوحة في القطاع — اللي الإدارة وافقت عليها ولسه ماانتهتش (نفس شرط فيد التجار).

    بنجيب بس الحقول اللي بتتعرض للعامة: من غير اسم العميل ولا موبايله ولا ميزانيته ولا مرفقاته.
    """
    return (ServiceRequest.objects
            .filter(sector=sector, status='open', is_approved=True, expires_at__gt=timezone.now())
            .only('request_code', 'title', 'description', 'customer_city', 'urgency', 'offers_count', 'created_at')
            .order_by('-created_at'))


def _new_request_url(sector, customer):
    """رابط «اكتب طلبك»: المسجّل يروح لفورم الطلب في لوحته، وغير المسجّل يدخل الأول ويرجع للفورم."""
    target = '/marketplace/dashboard/?new=1'
    if customer:
        return target
    from urllib.parse import quote
    return f'/marketplace/{sector}/?tab=login&next={quote(target, safe="")}'


STOCK_PREVIEW = 12


def _traders_stock(sector, *, q, make, cat, first_page):
    """🏪 أصناف مخازن التجار (زي فيكست) في كتالوج قطع الغيار.

    🐛 [FIX]: «سوق قطع الغيار» اللي كل اللينكات بتودّي عليه (الرئيسية، 404، لوحة العميل) كان بيعرض
    منتجات المتاجر التجريبية بس (16 منتج)، والأصناف الحقيقية اللي الإدارة وافقت عليها (مئات من فيكست)
    كانت في صفحة تانية (/marketplace/parts/traders/) محدش بيوصلها. دلوقتي أول 12 بتظهر فوق المعروضات
    بنفس البحث والماركة، ولينك «شوف الكل». القسم (cat) خاص بالكتالوج ⇒ مع قسم مختار مابنعرضهاش.
    """
    if sector != 'automotive' or cat:
        return [], 0
    from clients.models import PartCarMake
    from clients.services import parts_stock_orders as stock_svc
    part_make = None
    if make:
        part_make = PartCarMake.objects.filter(Q(name__iexact=make) | Q(slug__iexact=make)).first()
        if part_make is None:     # ماركة الكتالوج بالعربي — نفلتر بالاسم نفسه
            part_make = PartCarMake(name=make, slug=make)
    qs = stock_svc.filter_stock_items(stock_svc.public_stock_items(), q=q, make=part_make)
    count = qs.count()
    return (list(qs.select_related('tenant')[:STOCK_PREVIEW]) if first_page and count else []), count


def catalog_home(request, sector):
    meta = _sector_or_404(sector)
    customer = _marketplace_auth(request)
    wanted = _open_customer_requests(sector)
    if request.GET.get('tab') == 'requests':
        # 🛍️ السوق وشّين: معروضات الشركات + طلبات العملاء (عشان الشركات تدخل تشوف المطلوب وتقدّم عروض)
        return render(request, 'clients/marketplace/catalog.html', {
            'sector': sector, 'meta': meta, 'tab': 'requests', 'customer': customer,
            'wanted': Paginator(wanted, PER_PAGE).get_page(request.GET.get('page')),
            'wanted_count': wanted.count(),
            'offers_count': CatalogItem.objects.filter(store__sector=sector, store__is_active=True,
                                                       is_active=True).count(),
            'new_request_url': _new_request_url(sector, customer),
            'categories': [], 'page': None,
        })
    items = (CatalogItem.objects.filter(store__sector=sector, store__is_active=True, is_active=True)
             .select_related('store'))
    cat = request.GET.get('cat', '')
    q = (request.GET.get('q') or '').strip()[:80]
    make = (request.GET.get('make') or '').strip()[:60]
    if cat in meta['categories']:
        items = items.filter(category=cat)
    else:
        cat = ''
    if make:
        items = items.filter(Q(car_make=make) | Q(car_make=''))
    if q:
        # 🐛 [FIX]: البحث كان على العنوان العربي بس — «tri-fold» ماكانتش بتلاقي «بروشور تري فولد»
        items = items.filter(text_filter(q, ('title', 'title_en', 'description', 'description_en', 'car_models',
                                             'car_models_en', 'part_number', 'store__name')))
    # ترتيب ثابت بيخلط المتاجر (أول منتج من كل متجر، بعدين التاني…) — والصفحات مابتتكررش.
    # «اختيار Mouss Tec» وتقييم Mouss Tec الأعلى الأول (من غير ما يأثر على ثبات الصفحات).
    # 📸 المنتجات اللي ليها صور الأول (طلب صاحب المنصة) — وبعدين نفس الترتيب الثابت القديم.
    items = _with_photo_first(items).order_by('-has_photo', '-store__is_featured', 'sort',
                                              '-store__platform_score', 'store__sort', 'id')
    page = Paginator(items, PER_PAGE).get_page(request.GET.get('page'))
    page.object_list = catalog_stats.decorate_items(page.object_list)
    stores = catalog_stats.decorate_stores(
        CatalogStore.objects.filter(sector=sector, is_active=True)
        .annotate(n=Count('items', filter=Q(items__is_active=True)),
                  # 🏷️ الشركات اللي معروضاتها عليها اسم براند (شغل حقيقي) تظهر الأول
                  n_brand=Count('items', filter=Q(items__is_active=True, items__image__contains='-brand-')))
        .order_by(Case(When(n_brand__gt=0, then=Value(0)), default=Value(1), output_field=IntegerField()),
                  '-is_featured', '-platform_score', 'sort'))
    makes = []
    if sector == 'automotive':
        # [(القيمة في الفلتر، الاسم المعروض)] — الاسم بالإنجليزي لو اللغة إنجليزي
        pairs = set(CatalogItem.objects.filter(store__sector='automotive', is_active=True)
                    .exclude(car_make='').values_list('car_make', 'car_make_en'))
        makes = sorted({(m, (m_en if _is_en() and m_en else m)) for m, m_en in pairs}, key=lambda x: x[1])
    stock_items, stock_count = _traders_stock(sector, q=q, make=make, cat=cat, first_page=page.number == 1)
    # 📣 جاي من إعلان الشركات (utm_campaign=shops) ⇒ زرار «سجّل شركتك» يظهر فوق الصفحة.
    #    🐛 [FIX]: الإعلان كان بيوصّل أصحاب المطابع للسوق وزرار التسجيل مدفون تحت المعروضات
    #    فمحدش سجّل. بنقبل كمان «role=shop» الملزوق في رابط الإعلان القديم.
    from_shops_ad = (request.GET.get('utm_campaign') == 'shops'
                     or 'role=shop' in (request.META.get('QUERY_STRING') or '').replace('%3D', '='))
    return render(request, 'clients/marketplace/catalog.html', {
        'from_shops_ad': from_shops_ad,
        'sector': sector, 'meta': meta, 'page': page, 'stores': stores,
        'stock_items': stock_items, 'stock_count': stock_count,
        'categories': _categories(meta),
        'cat': cat, 'q': q, 'make': make, 'makes': makes,
        'customer': customer, 'tab': 'offers',
        'wanted_count': wanted.count(), 'offers_count': page.paginator.count,
        'new_request_url': _new_request_url(sector, customer),
        'show_activity': not (q or cat or make) and page.number == 1,
        'recent': catalog_stats.recent_requests(sector) if page.number == 1 else [],
        'top': catalog_stats.top_items(sector) if not (q or cat or make) and page.number == 1 else [],
    })


def catalog_store(request, slug):
    store = get_object_or_404(CatalogStore, slug=slug, is_active=True)
    catalog_stats.decorate_stores([store])
    items = _with_photo_first(store.items.filter(is_active=True)).order_by('-has_photo', 'sort', 'id')
    page = Paginator(items, 100).get_page(1)
    page.object_list = catalog_stats.decorate_items(page.object_list)
    return render(request, 'clients/marketplace/catalog.html', {
        'sector': store.sector, 'meta': SECTORS[store.sector], 'store': store, 'page': page,
        'categories': [], 'customer': _marketplace_auth(request),
        'reviews': store.reviews.filter(is_visible=True)[:20],
        'min_reviews': catalog_stats.MIN_REVIEWS,
        'seo_jsonld': _store_ld(store),
    })


# ─────────────────────────── 🔎 SEO (بيانات منظّمة لجوجل) ───────────────────────────

def _store_rating_ld(store):
    if getattr(store, 'rating', None) and getattr(store, 'reviews_count', 0) >= catalog_stats.MIN_REVIEWS:
        return {'@type': 'AggregateRating', 'ratingValue': round(float(store.rating), 1),
                'reviewCount': store.reviews_count, 'bestRating': 5, 'worstRating': 1}
    return None


def _store_ld(store):
    from erp_core.seo import breadcrumbs_ld, jsonld
    meta = SECTORS[store.sector]
    url = f'/marketplace/catalog/store/{store.slug}/'
    org = {'@context': 'https://schema.org', '@type': 'Store', 'name': store.name, 'url': f'https://{settings.BASE_DOMAIN}{url}',
           'description': (store.description or '')[:300] or None, 'aggregateRating': _store_rating_ld(store),
           'address': {'@type': 'PostalAddress', 'addressCountry': 'EG'}}
    org = {k: v for k, v in org.items() if v is not None}
    crumbs = breadcrumbs_ld([('Mouss Tec', '/'), (meta['title'], f'/marketplace/catalog/{store.sector}/'),
                             (store.name, url)])
    return jsonld(org) + jsonld(crumbs)


def _item_ld(item):
    from erp_core.seo import breadcrumbs_ld, jsonld, public_base
    store, meta = item.store, SECTORS[item.store.sector]
    url = f'/marketplace/catalog/item/{item.item_code}/'
    prod = {'@context': 'https://schema.org', '@type': 'Product', 'name': item.title,
            'description': (item.description or item.title)[:500],
            'category': _category_label(meta, item.category) or None,
            'image': (item.image.url if item.image.url.startswith('http') else public_base() + item.image.url)
            if item.image else None,
            'brand': {'@type': 'Brand', 'name': store.name}, 'url': public_base() + url}
    prod = {k: v for k, v in prod.items() if v is not None}
    # الكتالوج من غير أسعار ولا تقييم للمنتج نفسه ⇒ جوجل بيعلّم «Product» كخطأ في Search Console؛
    # بنكتفي بمسار التصفح (بيظهر في نتايج البحث) لحد ما يبقى فيه سعر.
    prod = None
    crumbs = breadcrumbs_ld([('Mouss Tec', '/'), (meta['title'], f'/marketplace/catalog/{store.sector}/'),
                             (store.name, f'/marketplace/catalog/store/{store.slug}/'), (item.title, url)])
    # 🐛 [FIX]: «'' + SafeString» بيرجّع نص عادي ⇒ Django كان بيهرّب الـ <script> ويطبعه كلام فوق صفحة المنتج.
    return mark_safe((jsonld(prod) if prod else '') + jsonld(crumbs))


def _track_quote_lead(request, req):
    """📣 طلب سعر من الكتالوج ⇒ حدث «Lead» لفيسبوك (السيرفر هنا + المتصفح بنفس الـ id في الصفحة اللي بعدها).

    ده اللي حملة «مطبوعات الشركات» بتتحسّن عليه — من غيره فيسبوك مايعرفش مين من الإعلان طلب سعر فعلاً.
    """
    event_id = f'cq-{req.request_code}'
    request.session['pixel_event'] = {'name': 'Lead', 'id': event_id}
    try:
        from clients.services import meta_capi
        meta_capi.send_contact('Lead', event_id=event_id, phone=req.phone,
                               external_id=f'mpc-{req.customer_id}' if req.customer_id else '',
                               request=request, source_url=request.build_absolute_uri())
    except Exception:
        logger.exception('[META_CAPI] catalog quote Lead failed for %s', req.pk)


def catalog_item(request, item_code):
    item = get_object_or_404(CatalogItem.objects.select_related('store'), item_code=item_code,
                             is_active=True, store__is_active=True)
    customer = _marketplace_auth(request)
    form = {
        'name': getattr(customer, 'full_name', '') or '',
        'phone': (getattr(customer, 'phone', '') or '').replace('+20', '0'),
        'quantity': item.min_qty, 'details': '', 'car_info': '',
    }
    errors = []
    opts = catalog_options.item_options(item) if item.store.sector == 'printing' else {}
    chosen = {}
    if request.method == 'POST':
        form = {k: (request.POST.get(k) or '').strip() for k in ('name', 'phone', 'quantity', 'details', 'car_info')}
        # 🎨 اختيارات العميل — بنقبل بس القيم الموجودة في المنتج (ومتخزّنة بالعربي لصاحب المنصة)
        for key, group in (('size', 'sizes'), ('shape', 'shapes'), ('finish', 'finishes')):
            val = (request.POST.get('opt_' + key) or '').strip()
            if val and val in {v for v, _ in opts.get(group, [])}:
                chosen[key] = val
        # 📏 المقاس بالسنتي — إجباري مع «مقاس خاص» (من غيره المطبعة ماتعرفش تسعّر)
        dims = ' '.join((request.POST.get('opt_dims') or '').split())[:80]
        if dims:
            chosen['dimensions'] = dims
        elif chosen.get('size') == catalog_options.CUSTOM_SIZE:
            errors.append(_('اكتب المقاس اللي محتاجه بالسنتي (طول × عرض × ارتفاع).'))
        # 📎 لوجو/تصميم العميل — صور وPDF وملفات التصميم المعروفة بس، لحد ١٥ ميجا
        upload = request.FILES.get('attachment')
        if upload:
            ext = os.path.splitext(upload.name or '')[1].lower()
            if ext not in ATTACHMENT_EXT:
                errors.append(_('نوع الملف ده مش مدعوم — ابعت صورة أو PDF أو ملف تصميم (AI / PSD / EPS / SVG).'))
            elif upload.size > ATTACHMENT_MAX:
                errors.append(_('الملف أكبر من ١٥ ميجا — صغّره أو ابعته على الواتساب بعد ما نكلمك.'))
        color = (request.POST.get('opt_color') or '').strip()
        if color in {k for k, _, _ in opts.get('colors', [])}:
            chosen['color_key'] = color
            chosen['color'] = catalog_options.COLORS[color][0]
            chosen['color_hex'] = catalog_options.COLORS[color][2]
        digits = ''.join(ch for ch in form['phone'] if ch.isdigit())
        if len(form['name']) < 2:
            errors.append(_('اكتب اسمك عشان نعرف نكلمك.'))
        if not 8 <= len(digits) <= 15:
            errors.append(_('رقم الموبايل مش مظبوط — اكتبه زي 01012345678.'))
        from erp_core.client_ip import client_ip
        ip = client_ip(request)
        key = f'catalog_quote:{ip or "x"}'
        if cache.get(key, 0) >= QUOTE_LIMIT_PER_HOUR:
            errors.append(_('بعتّ طلبات كتير في وقت قصير — استنى شوية وجرّب تاني.'))
        if not errors:
            cache.set(key, cache.get(key, 0) + 1, 3600)
            req = CatalogQuoteRequest.objects.create(
                item=item, item_title=item.title[:200], store_name=item.store.name[:120],
                sector=item.store.sector, customer=customer,
                name=form['name'][:120], phone=form['phone'][:30], quantity=form['quantity'][:60],
                details=form['details'][:3000], car_info=form['car_info'][:200], ip_address=ip,
                options=chosen, attachment=upload or None,
            )
            try:
                from clients.models import PlatformEvent
                PlatformEvent.objects.create(
                    event_type='other', tenant_schema='public', tenant_name='Marketplace',
                    description=f'🛍️ طلب سعر: {item.title[:80]} — {req.name[:40]}',
                    metadata={'catalog_request': str(req.request_code), 'sector': req.sector},
                )
            except Exception:
                logger.exception('[CATALOG] event log failed')
            from django.db import transaction
            transaction.on_commit(lambda: notify_owner_async(req.pk))
            # 🏪 منتج بائع سوق ⇒ الطلب يوصل للبائع نفسه كمان (هو اللي هيكلّم العميل)
            seller = item.store.seller_or_none
            if seller and not seller.is_blocked:
                from clients.services.market_seller import notify_seller
                notify_seller(seller, f'Mouss Tec — طلب سعر جديد: {item.title[:80]}',
                              f'طلب سعر جديد على «{item.title[:100]}» من {req.name[:60]} — {req.phone}.',
                              '/marketplace/seller/customers/')
            _track_quote_lead(request, req)
            return redirect(f'/marketplace/catalog/item/{item.item_code}/?sent=1#quote')
    related = list(_with_photo_first(CatalogItem.objects.filter(store=item.store, is_active=True)
                                     .exclude(pk=item.pk)).select_related('store')
                   .order_by('-has_photo', 'sort', 'id')[:8])
    catalog_stats.decorate_items([item])
    catalog_stats.decorate_stores([item.store])
    return render(request, 'clients/marketplace/catalog_item.html', {
        'opts': opts, 'chosen': chosen,
        'ask_dims': any(catalog_options.is_vague_size(v) for v, _label in opts.get('sizes', [])),
        'reviews': item.store.reviews.filter(is_visible=True)[:5],
        'item': item, 'store': item.store, 'sector': item.store.sector, 'meta': SECTORS[item.store.sector],
        'form': form, 'errors': errors, 'related': related, 'customer': customer,
        'sent': request.GET.get('sent') == '1',
        'category_label': _category_label(SECTORS[item.store.sector], item.category),
        'seo_jsonld': _item_ld(item),
    })


# ─────────────────────────── 👑 السوبر أدمن ───────────────────────────

def _decorate_for_printer(request, r):
    """🖨️ اللي ناقص عشان الطلب يروح المطبعة + رسالتين واتساب جاهزين (للعميل وللمطبعة).

    🐛 [FIX]: الطلب كان بيوصل «المقاس: كبير» ومن غير ملف تصميم، والصفحة مابتقولش إن فيه حاجة ناقصة
    ولا بتدّي طريقة تطلبها — فصاحب المنصة مايعرفش يبعت إيه للمطبعة.
    """
    opts = r.options or {}
    printing = r.sector == 'printing'
    r.attachment_url = ''
    if r.attachment:
        try:
            r.attachment_url = request.build_absolute_uri(r.attachment.url)
        except Exception:  # noqa: BLE001 — ملف ناقص في التخزين مايوقّعش الصفحة
            logger.warning('[CATALOG] attachment url failed for quote %s', r.pk)
    r.needs_dims = printing and not opts.get('dimensions') and (
        not opts.get('size') or catalog_options.is_vague_size(opts.get('size')))
    r.needs_design = printing and not r.attachment_url
    r.ask_text = ''
    if r.needs_dims or r.needs_design:
        ask = [_('أهلاً %(name)s، معاك Mouss Tec بخصوص طلب «%(title)s».') % {'name': r.name, 'title': r.item_title},
               _('عشان نبعت طلبك للمطبعة ونرد عليك بالسعر محتاجين منك:')]
        if r.needs_dims:
            ask.append('• ' + _('المقاس بالسنتي (طول × عرض × ارتفاع)'))
        if r.needs_design:
            ask.append('• ' + _('ملف اللوجو/التصميم (PDF أو AI أو صورة بجودة عالية)'))
        ask.append(_('ابعتهم هنا على الواتساب 🙏'))
        r.ask_text = '\n'.join(ask)
    r.printer_text = ''
    if printing:
        size = opts.get('size', '')
        if opts.get('dimensions'):
            size = f"{size} ({opts['dimensions']})" if size else opts['dimensions']
        rows = [(_('المنتج'), r.item_title), (_('الكمية'), r.quantity), (_('المقاس'), size),
                (_('اللون'), opts.get('color', '')), (_('الشكل'), opts.get('shape', '')),
                (_('التشطيب'), opts.get('finish', '')), (_('التفاصيل'), r.details[:800]),
                (_('ملف التصميم'), r.attachment_url)]
        r.printer_text = '\n'.join([_('طلب تسعير من Mouss Tec')] + [f'{k}: {v}' for k, v in rows if v])

def _decorate_for_desk(request, r):
    """🗂️ تنظيم الطلب للعرض: أرقام/لينكات من التفاصيل، عمر الطلب، معاينة الملف."""
    r.wa_number = desk.wa_digits(r.phone)
    r.phone_local = desk.local_phone(r.phone)
    r.contacts = desk.extract_contacts(r.details, main_phone=r.phone)
    r.age = desk.age_info(r.created_at, r.status)
    r.attachment_name = os.path.basename(r.attachment.name) if r.attachment else ''
    r.attachment_is_image = desk.is_image(r.attachment_name)
    r.review_url = (request.build_absolute_uri(f'/marketplace/catalog/review/{r.review_token}/')
                    if r.review_token else '')
    r.existing_review = getattr(r, 'review', None) if r.review_token else None
    _decorate_for_printer(request, r)


def _filtered_requests(request):
    status = request.GET.get('status', 'open')
    qs = CatalogQuoteRequest.objects.select_related('item', 'item__store', 'customer', 'review')
    if status == 'open':
        qs = qs.filter(status__in=('new', 'contacted', 'quoted'))
    elif status in dict(CatalogQuoteRequest.STATUS_CHOICES):
        qs = qs.filter(status=status)
    else:
        status = 'all'
    sector = request.GET.get('sector', '')
    if sector in SECTORS:
        qs = qs.filter(sector=sector)
    else:
        sector = ''
    q = (request.GET.get('q') or '').strip()[:80]
    if q:
        digits = ''.join(ch for ch in q.translate(desk._ARABIC_DIGITS) if ch.isdigit())
        cond = (Q(name__icontains=q) | Q(item_title__icontains=q) | Q(store_name__icontains=q)
                | Q(details__icontains=q) | Q(admin_notes__icontains=q))
        if len(digits) >= 4:
            cond |= Q(phone__contains=digits[-9:]) | Q(details__contains=digits[-9:])
        qs = qs.filter(cond)
    sort = request.GET.get('sort', 'new')
    qs = qs.order_by('created_at' if sort == 'old' else '-created_at')
    return qs, {'status': status, 'sector': sector, 'q': q, 'sort': sort}


def _requests_csv(request, qs):
    import csv

    from django.http import HttpResponse
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    stamp = timezone.localtime().strftime('%Y%m%d-%H%M')
    resp['Content-Disposition'] = f'attachment; filename="catalog-requests-{stamp}.csv"'
    resp.write('\ufeff')   # BOM ⇒ Excel يفتح العربي صح
    w = csv.writer(resp)
    w.writerow(desk.CSV_HEADER)
    for r in qs[:5000]:
        url = ''
        if r.attachment:
            try:
                url = request.build_absolute_uri(r.attachment.url)
            except Exception:  # noqa: BLE001
                url = ''
        w.writerow(desk.csv_row(r, attachment_url=url))
    return resp


@login_required
@platform_owner_required
def catalog_requests_admin(request):
    qs, f = _filtered_requests(request)
    if request.GET.get('export') == 'csv':
        return _requests_csv(request, qs)
    all_qs = CatalogQuoteRequest.objects.all()
    counts = dict(all_qs.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))
    tabs = [(key, label, counts.get(key, 0)) for key, label in CatalogQuoteRequest.STATUS_CHOICES]
    now = timezone.now()
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    month = all_qs.filter(created_at__gte=now - timezone.timedelta(days=30))
    month_total = month.count()
    month_done = month.filter(status='done').count()
    kpis = {
        'today': all_qs.filter(created_at__gte=today).count(),
        'open': sum(counts.get(k, 0) for k in ('new', 'contacted', 'quoted')),
        'late': all_qs.filter(status='new', created_at__lt=now - timezone.timedelta(hours=6)).count(),
        'month': month_total,
        'conversion': round(100 * month_done / month_total) if month_total else 0,
    }
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    for r in page.object_list:
        _decorate_for_desk(request, r)
    from urllib.parse import urlencode
    base_qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'clients/saas_admin/catalog_requests.html', {
        'reviews_hidden': CatalogReview.objects.filter(is_visible=False).count(),
        'reviews_total': CatalogReview.objects.count(),
        'page': page, **f, 'tabs': tabs, 'kpis': kpis, 'base_qs': base_qs,
        'status_choices': CatalogQuoteRequest.STATUS_CHOICES,
        'flow': [('new', _('جديد')), ('contacted', _('اتواصلت')), ('quoted', _('اتبعت السعر')), ('done', _('اتنفّذ'))],
        'stores_count': CatalogStore.objects.count(), 'items_count': CatalogItem.objects.count(),
    })


@login_required
@platform_owner_required
def catalog_request_sheet(request, request_id):
    """🧾 أمر شغل للطلب — صفحة نضيفة تتطبع أو تتحفظ PDF (من غير رقم العميل لو للمطبعة)."""
    r = get_object_or_404(CatalogQuoteRequest.objects.select_related('item'), pk=request_id)
    _decorate_for_desk(request, r)
    return render(request, 'clients/saas_admin/catalog_request_sheet.html', {
        'r': r, 'for_printer': request.GET.get('for') == 'printer',
    })


@login_required
@platform_owner_required
def catalog_request_vcard(request, request_id):
    """📇 احفظ العميل في موبايلك بكل أرقامه (.vcf)."""
    from django.http import HttpResponse
    r = get_object_or_404(CatalogQuoteRequest, pk=request_id)
    resp = HttpResponse(desk.vcard(r), content_type='text/vcard; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="customer-{r.pk}.vcf"'
    return resp


@login_required
@platform_owner_required
@require_POST
def catalog_request_update(request, request_id):
    req = get_object_or_404(CatalogQuoteRequest, pk=request_id)
    status = request.POST.get('status', '')
    if status in dict(CatalogQuoteRequest.STATUS_CHOICES):
        req.status = status
    if 'admin_notes' in request.POST:
        req.admin_notes = request.POST.get('admin_notes', '')[:3000]
    fields = ['status', 'admin_notes', 'updated_at']
    if req.status == 'done' and not req.review_token:
        import uuid
        req.review_token = uuid.uuid4()   # ⭐ لينك تقييم للعميل (مرة واحدة)
        fields.append('review_token')
    req.save(update_fields=fields)
    if request.headers.get('X-Requested-With') == 'fetch':   # ⚡ تغيير الحالة من غير ما الصفحة تتحمّل تاني
        from django.http import JsonResponse
        return JsonResponse({'ok': True, 'status': req.status, 'label': req.get_status_display(),
                             'review_url': (request.build_absolute_uri(
                                 f'/marketplace/catalog/review/{req.review_token}/') if req.review_token else '')})
    back = request.POST.get('next') or '/superadmin/catalog/requests/'
    return redirect(back if back.startswith('/superadmin/catalog/') else '/superadmin/catalog/requests/')


# ─────────────────────────── ⭐ تقييم العميل ───────────────────────────

def catalog_review(request, token):
    """العميل بيقيّم بعد ما طلبه يتنفّذ — من لينك بيوصله من Mouss Tec (طلب = تقييم واحد)."""
    req = get_object_or_404(CatalogQuoteRequest.objects.select_related('item', 'item__store'),
                            review_token=token, status='done')
    store = req.item.store if req.item else CatalogStore.objects.filter(name=req.store_name).first()
    if store is None:
        raise Http404
    existing = CatalogReview.objects.filter(quote_request=req).first()
    errors = []
    form = {'rating': '', 'comment': '', 'name': req.name}
    if request.method == 'POST' and not existing:
        form = {k: (request.POST.get(k) or '').strip() for k in ('rating', 'comment', 'name')}
        try:
            rating = int(form['rating'])
        except ValueError:
            rating = 0
        if not 1 <= rating <= 5:
            errors.append(_('اختار عدد النجوم من ١ لـ ٥.'))
        if not errors:
            existing = CatalogReview.objects.create(
                quote_request=req, store=store, item=req.item, rating=rating,
                comment=form['comment'][:1500], reviewer_name=(form['name'] or req.name)[:120],
            )
    return render(request, 'clients/marketplace/catalog_review.html', {
        'req': req, 'store': store, 'review': existing, 'errors': errors, 'form': form,
        'sector': store.sector, 'meta': SECTORS[store.sector], 'stars': range(1, 6),
    })


# ─────────────────────────── 🏪 اعرض منتجاتك (للتجار) ───────────────────────────

def _merchant_offer(settings):
    raw = getattr(settings, 'MARKETPLACE_MERCHANT_OFFER', None)
    if raw is None:
        return _('أول ٥٠ مطبعة أو محل قطع غيار: العرض في السوق ببلاش ٣ شهور.')
    return '' if raw.strip().lower() in ('', 'off', '0') else raw


def catalog_sell(request):
    """صفحة جذب المطابع ومحلات قطع الغيار — بأرقام حقيقية من السيستم."""
    from django.conf import settings
    stats = catalog_stats.merchant_pitch_stats()
    for c in stats['top_categories']:
        meta = SECTORS.get(c['sector'])
        c['label'] = _category_label(meta, c['item__category']) if meta else c['item__category']
    return render(request, 'clients/marketplace/catalog_sell.html', {
        'stats': stats,
        'offer': _merchant_offer(settings),
        'whatsapp': ''.join(ch for ch in getattr(settings, 'SUPPORT_WHATSAPP', '') if ch.isdigit()),
        'stores_count': CatalogStore.objects.filter(is_active=True).count(),
        'items_count': CatalogItem.objects.filter(is_active=True).count(),
    })
