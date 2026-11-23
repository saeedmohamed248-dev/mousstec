"""
📣 Feed منتجات المطابع لكتالوج ميتا (فيسبوك/إنستجرام) — `/feeds/meta/printing.csv`.

ميتا بتسحب الرابط ده كل فترة (Data source ← Data feed ← Scheduled feed) وتبني منه الكتالوج
اللي بتتعمل عليه إعلانات المنتجات. الأعمدة على مواصفات ميتا:
id, title, description, availability, condition, price, link, image_link, brand, google_product_category

المنتجات:
  * كتالوج السوق (قطاع الطباعة) — اللي ليها صورة و«السعر يبدأ من» (بيتكتب من الأدمن).
  * سوق المطابع — منتجات المطابع اللي ليها صورة وسعر.
ميتا بترفض أي منتج من غير سعر أو صورة، فبنسيبهم برّه بدل ما الكتالوج كله يطلع أخطاء.
"""
import csv
import io

from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_GET

FIELDS = ('id', 'title', 'description', 'availability', 'condition', 'price', 'link', 'image_link', 'brand',
          'google_product_category')
# Business & Industrial > Advertising & Marketing (تصنيف جوجل اللي ميتا بتقبله)
_PRINT_CATEGORY = '5863'


def _text(value, limit):
    return ' '.join((value or '').split())[:limit]


def _price(value):
    return f'{value:.2f} EGP'


def _rows(request):
    from clients.models import CatalogItem
    from print_market.models import MarketProduct

    abs_url = request.build_absolute_uri
    items = (CatalogItem.objects.filter(store__sector='printing', store__is_active=True, is_active=True,
                                        price_from__gt=0)
             .exclude(image='').exclude(image__isnull=True).select_related('store').order_by('id'))
    for it in items:
        yield {
            'id': f'cat-{it.item_code}',
            'title': _text(it.title, 150),
            'description': _text(it.description or it.title, 5000),
            'availability': 'in stock', 'condition': 'new',
            'price': _price(it.price_from),
            'link': abs_url(reverse('catalog_item', args=[it.item_code])),
            'image_link': abs_url(it.image.url),
            'brand': _text(it.store.name, 100) or 'Mouss Tec',
            'google_product_category': _PRINT_CATEGORY,
        }
    products = (MarketProduct.objects.filter(is_active=True, shop__is_active=True, price_from__gt=0)
                .exclude(image='').select_related('shop').order_by('id'))
    for p in products:
        yield {
            'id': f'pm-{p.pk}',
            'title': _text(p.title, 150),
            'description': _text(p.description or p.title, 5000),
            'availability': 'in stock', 'condition': 'new',
            'price': _price(p.price_from),
            'link': abs_url(reverse('print_market:product', args=[p.pk])),
            'image_link': abs_url(p.image.url),
            'brand': _text(p.shop.name, 100) or 'Mouss Tec',
            'google_product_category': _PRINT_CATEGORY,
        }


@require_GET
@cache_page(60 * 30)
def meta_printing_feed(request):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    for row in _rows(request):
        w.writerow(row)
    # BOM عشان العربي يتقري صح لو اتفتح في إكسل — ميتا بتقبله
    resp = HttpResponse('﻿' + buf.getvalue(), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'inline; filename="mousstec-printing.csv"'
    return resp
