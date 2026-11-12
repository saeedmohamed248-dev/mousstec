"""
🔎 SEO للموقع كله — مكان واحد:

* ``SeoMiddleware``:
    - صفحات عامة (الدومين الرئيسي / بوابات auto. و print.): بيضيف اللي ناقص بس —
      canonical (دايماً على الدومين الرئيسي، من غير فلاتر البحث)، Open Graph، Twitter Card،
      وصورة مشاركة مناسبة لكل سوق. أي صفحة حاطة الوسم بنفسها مابيتلمسش.
    - صفحات خاصة (أدمن / سوبر أدمن / داشبورد / طلبات / حسابات) ودومينات الشركات كلها:
      ``X-Robots-Tag: noindex`` — مابتظهرش في جوجل.
* ``robots_txt``: تعليمات الزحف + رابط الـ sitemap (دومين الشركة ⇒ Disallow: /).
* ``SITEMAPS``: الصفحات الثابتة + متاجر ومنتجات الكتالوج + سوق المطابع + قطع الغيار المعتمدة.

🐛 [FIX — SEO]: كان مفيش robots.txt ولا sitemap.xml (الاتنين كانوا بيتحوّلوا للرئيسية)،
ولا canonical ولا Open Graph في أي صفحة، وصفحات السيستم مش متعلّمة noindex.
"""
from __future__ import annotations

import html
import json
import re

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.db import connection
from django.http import HttpResponse
from django.urls import reverse
from django.utils.safestring import mark_safe

BASE_DOMAIN = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
SITE_NAME = 'Mouss Tec'

# بوابات القطاعات: نفس محتوى صفحات الدومين الرئيسي ⇒ الـ canonical عليه
PORTAL_ROOTS = {
    f'auto.{BASE_DOMAIN}': '/automotive/',
    f'print.{BASE_DOMAIN}': '/printing/',
}

# صفحات ماينفعش تتفهرس (حسابات / لوحات / طلبات / دفع / API)
PRIVATE_PREFIXES = (
    '/secure-portal', '/superadmin', '/admin-tools', '/api/', '/account/', '/accounts/', '/payment/',
    '/subscription/', '/system/', '/i18n/', '/healthz', '/support/', '/screen-rec/',
    '/marketplace/dashboard', '/marketplace/request/', '/marketplace/merchant/', '/marketplace/offer/',
    '/marketplace/notifications', '/marketplace/admin/', '/marketplace/profile',
    '/marketplace/parts/my-', '/marketplace/parts/wallet', '/marketplace/parts/saved', '/marketplace/parts/order',
    '/marketplace/parts/wanted/', '/marketplace/design-store/my', '/marketplace/design-store/print-orders',
    '/marketplace/catalog/review/',
    '/marketplace/print-market/dashboard', '/marketplace/print-market/settings',
    '/marketplace/print-market/my-orders', '/marketplace/print-market/order/',
    '/marketplace/print-market/notifications', '/marketplace/print-market/file/',
    '/marketplace/print-market/products/', '/marketplace/print-market/login',
    '/marketplace/print-market/password/', '/marketplace/print-market/go/',
)
# صفحات عامة بس لو اتفتحت بباراميتر (بحث/فلتر) ⇒ canonical من غيره
KEEP_QUERY = ('page',)

# صورة المشاركة حسب القسم (أول تطابق)
OG_IMAGES = (
    ('/marketplace/print-market', 'seo/og-print-market.jpg'),
    ('/marketplace/catalog/automotive', 'seo/og-automotive.jpg'),
    ('/marketplace/automotive', 'seo/og-automotive.jpg'),
    ('/marketplace/parts', 'seo/og-automotive.jpg'),
    ('/automotive', 'seo/og-automotive.jpg'),
    ('/marketplace', 'seo/og-printing.jpg'),
    ('/printing', 'seo/og-printing.jpg'),
    ('', 'seo/og-default.jpg'),
)


def public_base():
    return f'https://{BASE_DOMAIN}'


def is_private_path(path):
    return path.startswith(PRIVATE_PREFIXES)


def canonical_url(request):
    host = request.get_host().split(':')[0].lower()
    path = request.path
    if host in PORTAL_ROOTS and path == '/':
        path = PORTAL_ROOTS[host]
    q = [(k, v) for k in KEEP_QUERY for v in request.GET.getlist(k) if v and v != '1']
    qs = ('?' + '&'.join(f'{k}={v}' for k, v in q)) if q else ''
    return f'{public_base()}{path}{qs}'


def _static_url(rel):
    from django.templatetags.static import static
    url = static(rel)
    return url if url.startswith('http') else f'{public_base()}{url}'


def _og_image(path):
    for prefix, img in OG_IMAGES:
        if path.startswith(prefix):
            return _static_url(img)
    return _static_url('seo/og-default.jpg')


_RE_TITLE = re.compile(rb'<title[^>]*>(.*?)</title>', re.I | re.S)
_RE_DESC = re.compile(rb'<meta[^>]+name=["\']description["\'][^>]*>', re.I)
_RE_CONTENT = re.compile(rb'content=["\']([^"\']*)["\']', re.I)


def _has(body, needle):
    return needle in body


def _meta_tags(request, body):
    """الوسوم الناقصة بس — كـ bytes جاهزة للحقن قبل </head>."""
    out = []
    esc = lambda s: html.escape(s, quote=True)  # noqa: E731
    title_m = _RE_TITLE.search(body)
    title = html.unescape(title_m.group(1).decode('utf-8', 'replace').strip()) if title_m else SITE_NAME
    title = re.sub(r'\s+', ' ', title)
    desc = ''
    m = _RE_DESC.search(body)
    if m:
        c = _RE_CONTENT.search(m.group(0))
        desc = html.unescape(c.group(1).decode('utf-8', 'replace')) if c else ''
    url = canonical_url(request)
    lang = (getattr(request, 'LANGUAGE_CODE', '') or 'ar')[:2]
    if not _has(body, b'rel="canonical"') and not _has(body, b"rel='canonical'"):
        out.append(f'<link rel="canonical" href="{esc(url)}">')
    if not _has(body, b'property="og:title"'):
        out += [
            f'<meta property="og:type" content="website">',
            f'<meta property="og:site_name" content="{SITE_NAME}">',
            f'<meta property="og:locale" content="{"en_US" if lang == "en" else "ar_EG"}">',
            f'<meta property="og:title" content="{esc(title)}">',
            f'<meta property="og:url" content="{esc(url)}">',
        ]
        if desc:
            out.append(f'<meta property="og:description" content="{esc(desc)}">')
    if not _has(body, b'property="og:image"'):
        out += [f'<meta property="og:image" content="{esc(_og_image(request.path))}">',
                '<meta property="og:image:width" content="1200">',
                '<meta property="og:image:height" content="630">']
    if not _has(body, b'name="twitter:card"'):
        out.append('<meta name="twitter:card" content="summary_large_image">')
    return ('\n'.join(out) + '\n').encode('utf-8') if out else b''


class SeoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            return self._process(request, response)
        except Exception:  # noqa: BLE001 — الـ SEO مايوقّعش صفحة أبداً
            import logging
            logging.getLogger(__name__).exception('SEO middleware failed')
            return response

    def _process(self, request, response):
        schema = getattr(connection, 'schema_name', 'public')
        host = request.get_host().split(':')[0].lower()
        public = schema == 'public' or host in PORTAL_ROOTS
        if not public or is_private_path(request.path):
            # دومين شركة أو صفحة خاصة ⇒ مابتظهرش في جوجل
            if not response.has_header('X-Robots-Tag'):
                response['X-Robots-Tag'] = 'noindex, nofollow'
            return response
        if (request.method != 'GET' or response.status_code != 200 or getattr(response, 'streaming', False)
                or 'text/html' not in response.get('Content-Type', '')):
            return response
        body = response.content
        idx = body.lower().find(b'</head>')
        if idx < 0:
            return response
        tags = _meta_tags(request, body)
        if tags:
            response.content = body[:idx] + tags + body[idx:]
            if response.has_header('Content-Length'):
                response['Content-Length'] = str(len(response.content))
        return response


# ───────────────────────────── robots.txt ─────────────────────────────

def robots_txt(request):
    schema = getattr(connection, 'schema_name', 'public')
    host = request.get_host().split(':')[0].lower()
    if schema != 'public' and host not in PORTAL_ROOTS:
        return HttpResponse('User-agent: *\nDisallow: /\n', content_type='text/plain')
    lines = ['User-agent: *', 'Allow: /']
    lines += [f'Disallow: {p}' for p in PRIVATE_PREFIXES]
    lines += ['Disallow: /*?*q=', '', f'Sitemap: {public_base()}/sitemap.xml', '']
    return HttpResponse('\n'.join(lines), content_type='text/plain')


# ───────────────────────────── sitemap.xml ─────────────────────────────

class _Base(Sitemap):
    protocol = 'https'

    def get_domain(self, site=None):
        return BASE_DOMAIN


class StaticSitemap(_Base):
    changefreq = 'weekly'
    PAGES = (
        ('smart_root', 1.0), ('automotive_landing', 0.9), ('printing_landing', 0.9), ('saas_pricing', 0.9),
        ('features_page', 0.8), ('marketplace_home', 0.8), ('marketplace_printing', 0.8),
        ('marketplace_automotive', 0.8), ('catalog_sell', 0.7), ('parts_feed', 0.8),
        ('design_store_home', 0.7), ('print_market:home', 0.9), ('print_market:signup', 0.5),
        ('legal_privacy', 0.2), ('legal_terms', 0.2),
    )

    def items(self):
        out = []
        for name, prio in self.PAGES:
            try:
                out.append((reverse(name), prio))
            except Exception:  # noqa: BLE001 — صفحة اتشالت مايوقّعش الـ sitemap
                continue
        out += [(reverse('catalog_home', args=[s]), 0.9) for s in ('printing', 'automotive')]
        return out

    def location(self, item):
        return item[0]

    def priority(self, item):
        return item[1]


class CatalogStoreSitemap(_Base):
    changefreq = 'weekly'
    priority = 0.7

    def items(self):
        from clients.models import CatalogStore
        return CatalogStore.objects.filter(is_active=True).order_by('pk')

    def location(self, obj):
        return reverse('catalog_store', args=[obj.slug])


class CatalogItemSitemap(_Base):
    changefreq = 'weekly'
    priority = 0.6
    limit = 5000

    def items(self):
        from clients.models import CatalogItem
        return (CatalogItem.objects.filter(is_active=True, store__is_active=True)
                .only('item_code', 'created_at').order_by('pk'))

    def location(self, obj):
        return reverse('catalog_item', args=[obj.item_code])

    def lastmod(self, obj):
        return obj.created_at


class PrintShopSitemap(_Base):
    changefreq = 'weekly'
    priority = 0.7

    def items(self):
        from print_market.models import MarketAccount
        return MarketAccount.objects.filter(role='shop', is_active=True).only('pk').order_by('pk')

    def location(self, obj):
        return reverse('print_market:shop', args=[obj.pk])


class PrintProductSitemap(_Base):
    changefreq = 'weekly'
    priority = 0.6

    def items(self):
        from print_market.models import MarketProduct
        return (MarketProduct.objects.filter(is_active=True, shop__is_active=True)
                .only('pk', 'created_at').order_by('pk'))

    def location(self, obj):
        return reverse('print_market:product', args=[obj.pk])

    def lastmod(self, obj):
        return obj.created_at


class PartListingSitemap(_Base):
    changefreq = 'daily'
    priority = 0.6

    def items(self):
        from clients.models import PartListing
        return (PartListing.objects.filter(status='active', moderation_status='approved')
                .only('listing_code', 'updated_at').order_by('pk'))

    def location(self, obj):
        return reverse('parts_detail', args=[obj.listing_code])

    def lastmod(self, obj):
        return obj.updated_at


class PrintSeoSitemap(_Base):
    """صفحات الطباعة حسب الخدمة والمحافظة — المحافظة اللي مافيهاش مطابع مش بتدخل (noindex)."""
    changefreq = 'weekly'

    def items(self):
        from print_market.seo_data import GOVERNORATES, SERVICES
        from print_market.seo_pages import gov_has_content
        out = [(reverse('print_seo:index'), 0.8)]
        out += [(reverse('print_seo:service', args=[s]), 0.8) for s in SERVICES]
        for g in GOVERNORATES:
            if gov_has_content(g):
                out.append((reverse('print_seo:gov', args=[g]), 0.7))
                out += [(reverse('print_seo:service_gov', args=[s, g]), 0.6) for s in SERVICES]
        return out

    def location(self, item):
        return item[0]

    def priority(self, item):
        return item[1]


SITEMAPS = {
    'pages': StaticSitemap, 'catalog-stores': CatalogStoreSitemap, 'catalog-items': CatalogItemSitemap,
    'print-shops': PrintShopSitemap, 'print-products': PrintProductSitemap, 'parts': PartListingSitemap,
    'print-services': PrintSeoSitemap,
}


# ───────────────────────────── JSON-LD ─────────────────────────────

def jsonld(data):
    """<script type="application/ld+json"> آمن (مفيش </script> يكسر الصفحة)."""
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)
    raw = raw.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return mark_safe(f'<script type="application/ld+json">{raw}</script>')


def organization_ld():
    return {
        '@context': 'https://schema.org', '@graph': [
            {'@type': 'Organization', '@id': f'{public_base()}/#org', 'name': SITE_NAME, 'url': public_base(),
             'logo': _static_url('icon-512.png'),
             'contactPoint': {'@type': 'ContactPoint', 'contactType': 'customer support',
                              'telephone': '+' + str(getattr(settings, 'SUPPORT_WHATSAPP', '')).lstrip('+'),
                              'areaServed': 'EG', 'availableLanguage': ['Arabic', 'English']}},
            {'@type': 'WebSite', '@id': f'{public_base()}/#website', 'url': public_base(), 'name': SITE_NAME,
             'inLanguage': 'ar', 'publisher': {'@id': f'{public_base()}/#org'},
             'potentialAction': {'@type': 'SearchAction',
                                 'target': f'{public_base()}/marketplace/catalog/printing/?q={{search_term_string}}',
                                 'query-input': 'required name=search_term_string'}},
        ]}


def breadcrumbs_ld(items):
    """items: [(الاسم, المسار)]"""
    return {'@context': 'https://schema.org', '@type': 'BreadcrumbList', 'itemListElement': [
        {'@type': 'ListItem', 'position': i, 'name': name, 'item': f'{public_base()}{path}'}
        for i, (name, path) in enumerate(items, 1)]}
