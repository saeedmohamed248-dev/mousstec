"""🛍️ كتالوج السوق (متاجر Mouss Tec) — تصفّح + «اطلب سعر» + صفحة الطلبات في السوبر أدمن.

العميل مش لازم يكون مسجّل: بيكتب اسمه وموبايله. لو مسجّل في السوق بنملاهم له ونربط الطلب بحسابه.
"""
from __future__ import annotations

import logging
import os

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from django.utils.translation import get_language, gettext as _, gettext_lazy as _l

from clients.catalog_seed import PARTS_CATEGORIES, PARTS_CATEGORIES_EN, PRINT_CATEGORIES, PRINT_CATEGORIES_EN
from clients.models import CatalogItem, CatalogQuoteRequest, CatalogReview, CatalogStore
from clients.services import catalog_stats
from clients import catalog_options
from clients.permissions import platform_owner_required

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
        labels = {'size': 'المقاس', 'color': 'اللون', 'shape': 'الشكل', 'finish': 'التشطيب'}
        picked = [f"{labels[k]}: {v}" for k, v in (req.options or {}).items() if k in labels and v]
        if picked:
            lines.append('الاختيارات: ' + ' · '.join(picked))
        if req.attachment:
            lines.append('📎 العميل رفع لوجو/تصميم — موجود في صفحة الطلبات')
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


def catalog_home(request, sector):
    meta = _sector_or_404(sector)
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
        items = items.filter(Q(title__icontains=q) | Q(description__icontains=q)
                             | Q(car_models__icontains=q) | Q(store__name__icontains=q))
    # ترتيب ثابت بيخلط المتاجر (أول منتج من كل متجر، بعدين التاني…) — والصفحات مابتتكررش.
    # «اختيار Mouss Tec» وتقييم Mouss Tec الأعلى الأول (من غير ما يأثر على ثبات الصفحات).
    items = items.order_by('-store__is_featured', 'sort', '-store__platform_score', 'store__sort', 'id')
    page = Paginator(items, PER_PAGE).get_page(request.GET.get('page'))
    page.object_list = catalog_stats.decorate_items(page.object_list)
    stores = catalog_stats.decorate_stores(
        CatalogStore.objects.filter(sector=sector, is_active=True)
        .annotate(n=Count('items', filter=Q(items__is_active=True)))
        .order_by('-is_featured', '-platform_score', 'sort'))
    makes = []
    if sector == 'automotive':
        # [(القيمة في الفلتر، الاسم المعروض)] — الاسم بالإنجليزي لو اللغة إنجليزي
        pairs = set(CatalogItem.objects.filter(store__sector='automotive', is_active=True)
                    .exclude(car_make='').values_list('car_make', 'car_make_en'))
        makes = sorted({(m, (m_en if _is_en() and m_en else m)) for m, m_en in pairs}, key=lambda x: x[1])
    return render(request, 'clients/marketplace/catalog.html', {
        'sector': sector, 'meta': meta, 'page': page, 'stores': stores,
        'categories': _categories(meta),
        'cat': cat, 'q': q, 'make': make, 'makes': makes,
        'customer': _marketplace_auth(request),
        'show_activity': not (q or cat or make) and page.number == 1,
        'recent': catalog_stats.recent_requests(sector) if page.number == 1 else [],
        'top': catalog_stats.top_items(sector) if not (q or cat or make) and page.number == 1 else [],
    })


def catalog_store(request, slug):
    store = get_object_or_404(CatalogStore, slug=slug, is_active=True)
    catalog_stats.decorate_stores([store])
    items = store.items.filter(is_active=True).order_by('sort', 'id')
    page = Paginator(items, 100).get_page(1)
    page.object_list = catalog_stats.decorate_items(page.object_list)
    return render(request, 'clients/marketplace/catalog.html', {
        'sector': store.sector, 'meta': SECTORS[store.sector], 'store': store, 'page': page,
        'categories': [], 'customer': _marketplace_auth(request),
        'reviews': store.reviews.filter(is_visible=True)[:20],
        'min_reviews': catalog_stats.MIN_REVIEWS,
    })


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
            return redirect(f'/marketplace/catalog/item/{item.item_code}/?sent=1#quote')
    related = list(CatalogItem.objects.filter(store=item.store, is_active=True).exclude(pk=item.pk)
                   .select_related('store').order_by('sort')[:8])
    catalog_stats.decorate_items([item])
    catalog_stats.decorate_stores([item.store])
    return render(request, 'clients/marketplace/catalog_item.html', {
        'opts': opts, 'chosen': chosen,
        'reviews': item.store.reviews.filter(is_visible=True)[:5],
        'item': item, 'store': item.store, 'sector': item.store.sector, 'meta': SECTORS[item.store.sector],
        'form': form, 'errors': errors, 'related': related, 'customer': customer,
        'sent': request.GET.get('sent') == '1',
        'category_label': _category_label(SECTORS[item.store.sector], item.category),
    })


# ─────────────────────────── 👑 السوبر أدمن ───────────────────────────

@login_required
@platform_owner_required
def catalog_requests_admin(request):
    status = request.GET.get('status', 'open')
    qs = CatalogQuoteRequest.objects.select_related('item', 'item__store', 'customer')
    if status == 'open':
        qs = qs.filter(status__in=('new', 'contacted', 'quoted'))
    elif status in dict(CatalogQuoteRequest.STATUS_CHOICES):
        qs = qs.filter(status=status)
    sector = request.GET.get('sector', '')
    if sector in SECTORS:
        qs = qs.filter(sector=sector)
    tabs = [(key, label, CatalogQuoteRequest.objects.filter(status=key).count())
            for key, label in CatalogQuoteRequest.STATUS_CHOICES]
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    for r in page.object_list:
        digits = ''.join(ch for ch in r.phone if ch.isdigit())
        if digits.startswith('0') and len(digits) == 11:   # 01xxxxxxxxx ⇒ 201xxxxxxxxx
            digits = '20' + digits[1:]
        r.wa_number = digits
        r.review_url = (request.build_absolute_uri(f'/marketplace/catalog/review/{r.review_token}/')
                        if r.review_token else '')
        r.existing_review = getattr(r, 'review', None) if r.review_token else None
    return render(request, 'clients/saas_admin/catalog_requests.html', {
        'reviews_hidden': CatalogReview.objects.filter(is_visible=False).count(),
        'reviews_total': CatalogReview.objects.count(),
        'page': page, 'status': status, 'sector': sector, 'tabs': tabs,
        'status_choices': CatalogQuoteRequest.STATUS_CHOICES,
        'stores_count': CatalogStore.objects.count(), 'items_count': CatalogItem.objects.count(),
    })


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
