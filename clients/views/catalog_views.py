"""🛍️ كتالوج السوق (متاجر Mouss Tec) — تصفّح + «اطلب سعر» + صفحة الطلبات في السوبر أدمن.

العميل مش لازم يكون مسجّل: بيكتب اسمه وموبايله. لو مسجّل في السوق بنملاهم له ونربط الطلب بحسابه.
"""
from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from clients.catalog_seed import PARTS_CATEGORIES, PRINT_CATEGORIES
from clients.models import CatalogItem, CatalogQuoteRequest, CatalogStore
from clients.permissions import platform_owner_required

from ._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')

SECTORS = {
    'printing': {
        'title': 'سوق الطباعة والتصميم',
        'subtitle': 'كروت، كوبايات، أقلام، هدايا، يونيفورم، بنرات، تغليف وتصميم — اطلب سعر وإحنا نرد عليك',
        'categories': PRINT_CATEGORIES, 'icon': 'fa-palette', 'color': '#1E2A57',
    },
    'automotive': {
        'title': 'سوق قطع الغيار',
        'subtitle': 'قطع لكل العربيات — صيني وكوري وياباني وألماني وفرنساوي. قول لنا عربيتك وإحنا نرد بالسعر',
        'categories': PARTS_CATEGORIES, 'icon': 'fa-gears', 'color': '#9A3412',
    },
}
PER_PAGE = 36
QUOTE_LIMIT_PER_HOUR = 15


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
    # ترتيب ثابت بيخلط المتاجر (أول منتج من كل متجر، بعدين التاني…) — والصفحات مابتتكررش
    items = items.order_by('sort', 'store__sort', 'id')
    page = Paginator(items, PER_PAGE).get_page(request.GET.get('page'))
    stores = (CatalogStore.objects.filter(sector=sector, is_active=True)
              .annotate(n=Count('items', filter=Q(items__is_active=True))).order_by('sort'))
    makes = []
    if sector == 'automotive':
        makes = sorted(set(CatalogItem.objects.filter(store__sector='automotive', is_active=True)
                           .exclude(car_make='').values_list('car_make', flat=True)))
    return render(request, 'clients/marketplace/catalog.html', {
        'sector': sector, 'meta': meta, 'page': page, 'stores': stores,
        'categories': [(k, v[0], v[1]) for k, v in meta['categories'].items()],
        'cat': cat, 'q': q, 'make': make, 'makes': makes,
        'customer': _marketplace_auth(request),
    })


def catalog_store(request, slug):
    store = get_object_or_404(CatalogStore, slug=slug, is_active=True)
    items = store.items.filter(is_active=True).order_by('sort', 'id')
    return render(request, 'clients/marketplace/catalog.html', {
        'sector': store.sector, 'meta': SECTORS[store.sector], 'store': store,
        'page': Paginator(items, 100).get_page(1),
        'categories': [], 'customer': _marketplace_auth(request),
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
    if request.method == 'POST':
        form = {k: (request.POST.get(k) or '').strip() for k in ('name', 'phone', 'quantity', 'details', 'car_info')}
        digits = ''.join(ch for ch in form['phone'] if ch.isdigit())
        if len(form['name']) < 2:
            errors.append('اكتب اسمك عشان نعرف نكلمك.')
        if not 8 <= len(digits) <= 15:
            errors.append('رقم الموبايل مش مظبوط — اكتبه زي 01012345678.')
        from erp_core.client_ip import client_ip
        ip = client_ip(request)
        key = f'catalog_quote:{ip or "x"}'
        if cache.get(key, 0) >= QUOTE_LIMIT_PER_HOUR:
            errors.append('بعتّ طلبات كتير في وقت قصير — استنى شوية وجرّب تاني.')
        if not errors:
            cache.set(key, cache.get(key, 0) + 1, 3600)
            req = CatalogQuoteRequest.objects.create(
                item=item, item_title=item.title[:200], store_name=item.store.name[:120],
                sector=item.store.sector, customer=customer,
                name=form['name'][:120], phone=form['phone'][:30], quantity=form['quantity'][:60],
                details=form['details'][:3000], car_info=form['car_info'][:200], ip_address=ip,
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
            return redirect(f'/marketplace/catalog/item/{item.item_code}/?sent=1#quote')
    related = (CatalogItem.objects.filter(store=item.store, is_active=True).exclude(pk=item.pk)
               .order_by('sort')[:8])
    return render(request, 'clients/marketplace/catalog_item.html', {
        'item': item, 'store': item.store, 'sector': item.store.sector, 'meta': SECTORS[item.store.sector],
        'form': form, 'errors': errors, 'related': related, 'customer': customer,
        'sent': request.GET.get('sent') == '1',
        'category_label': SECTORS[item.store.sector]['categories'].get(item.category, ('', ''))[0],
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
    return render(request, 'clients/saas_admin/catalog_requests.html', {
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
    req.save(update_fields=['status', 'admin_notes', 'updated_at'])
    back = request.POST.get('next') or '/superadmin/catalog/requests/'
    return redirect(back if back.startswith('/superadmin/catalog/') else '/superadmin/catalog/requests/')
