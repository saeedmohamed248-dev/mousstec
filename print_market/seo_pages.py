"""
🗺️ صفحات SEO: «الطباعة حسب الخدمة والمحافظة».

    /print/                              كل الخدمات والمحافظات
    /print/<service>/                    خدمة في كل مصر
    /print/in/<gov>/                     مطابع محافظة
    /print/<service>/<gov>/              خدمة في محافظة

المحتوى من بيانات حقيقية: مطابع سوق المطابع ومتاجر الكتالوج في المحافظة (بتقييماتها)، المنتجات،
وأقل سعر «يبدأ من» فعلي. صفحة محافظة مافيهاش ولا مطبعة ⇒ noindex ومش في الـ sitemap.
"""
from django.db.models import F, Min, Q
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse

from . import auth
from .models import MarketProduct
from .seo_data import GOVERNORATES, SERVICES, gov_of


def _gov(slug):
    if slug not in GOVERNORATES:
        raise Http404
    return GOVERNORATES[slug]


def _service(slug):
    if slug not in SERVICES:
        raise Http404
    return SERVICES[slug]


def _pm_shops(gov_name=None):
    from .views import _rated_shops
    qs = _rated_shops()
    if gov_name:
        qs = qs.filter(city=gov_name)
    return qs


def _catalog_stores(gov_slug=None):
    from clients.models import CatalogStore
    stores = list(CatalogStore.objects.filter(sector='printing', is_active=True).order_by('sort', 'name'))
    if gov_slug:
        stores = [s for s in stores if gov_of(s.city) == gov_slug]
    return stores


def gov_has_content(gov_slug):
    g = GOVERNORATES[gov_slug]
    return _pm_shops(g[1]).exists() or bool(_catalog_stores(gov_slug))


def _products(service, gov_name=None, limit=12):
    qs = MarketProduct.objects.filter(is_active=True, shop__is_active=True, category=service['pm_category'])
    if gov_name:
        qs = qs.filter(shop__city=gov_name)
    return list(qs.select_related('shop').order_by('-created_at')[:limit])


def _catalog_items(service, gov_slug=None, limit=12):
    from clients.models import CatalogItem
    qs = CatalogItem.objects.filter(is_active=True, store__is_active=True, store__sector='printing',
                                    category__in=service['catalog']).select_related('store')
    items = list(qs.order_by('sort', 'id')[:200])
    if gov_slug:
        local = [i for i in items if gov_of(i.store.city) == gov_slug]
        items = local + [i for i in items if i not in local]
    with_photo = [i for i in items if i.image] + [i for i in items if not i.image]
    return with_photo[:limit]


def _min_price(service, gov_name=None):
    qs = MarketProduct.objects.filter(is_active=True, shop__is_active=True, category=service['pm_category'],
                                      price_from__isnull=False)
    if gov_name:
        local = qs.filter(shop__city=gov_name).aggregate(m=Min('price_from'))['m']
        if local is not None:
            return local
    return qs.aggregate(m=Min('price_from'))['m']


def _jsonld(crumbs, faqs, shops=()):
    from erp_core.seo import breadcrumbs_ld, jsonld, public_base
    out = jsonld(breadcrumbs_ld(crumbs))
    if faqs:
        out += jsonld({'@context': 'https://schema.org', '@type': 'FAQPage', 'mainEntity': [
            {'@type': 'Question', 'name': q, 'acceptedAnswer': {'@type': 'Answer', 'text': a}} for q, a in faqs]})
    if shops:
        out += jsonld({'@context': 'https://schema.org', '@type': 'ItemList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'name': s.name,
             'url': public_base() + reverse('print_market:shop', args=[s.pk])} for i, s in enumerate(shops, 1)]})
    return out


def _ctx(request, **kw):
    kw.setdefault('account', auth.current_account(request))
    kw['services'] = list(SERVICES.values())
    kw['governorates'] = list(GOVERNORATES.values())
    kw['request_url'] = '/marketplace/dashboard/?new=1'
    kw['total'] = len(kw.get('shops') or []) + len(kw.get('stores') or [])
    return kw


_HOME = ('Mouss Tec', '/')
_ROOT = ('خدمات الطباعة', '/print/')


def index(request):
    counts = {}
    for s in _pm_shops().values_list('city', flat=True):
        slug = gov_of(s)
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    for st in _catalog_stores():
        slug = gov_of(st.city)
        if slug:
            counts[slug] = counts.get(slug, 0) + 1
    govs = [(g, counts.get(slug, 0)) for slug, g in GOVERNORATES.items()]
    return render(request, 'print_market/seo/index.html', _ctx(
        request, gov_counts=govs, seo_jsonld=_jsonld([_HOME, _ROOT], [])))


def service_page(request, service):
    sv = _service(service)
    shops = list(_pm_shops().filter(products__category=sv['pm_category'], products__is_active=True).distinct()
                 .order_by(F('rating_avg').desc(nulls_last=True), '-created_at')[:12])
    crumbs = [_HOME, _ROOT, (sv['h1'], request.path)]
    return render(request, 'print_market/seo/page.html', _ctx(
        request, sv=sv, gov=None, shops=shops, stores=_catalog_stores()[:8],
        products=_products(sv), items=_catalog_items(sv), min_price=_min_price(sv),
        title=f"{sv['h1']} في مصر", indexable=True, crumbs=crumbs,
        seo_jsonld=_jsonld(crumbs, sv['faq'], shops)))


def gov_page(request, gov):
    g = _gov(gov)
    shops = list(_pm_shops(g[1]).order_by(F('rating_avg').desc(nulls_last=True), '-created_at')[:24])
    stores = _catalog_stores(gov)
    crumbs = [_HOME, _ROOT, (f'مطابع في {g[1]}', request.path)]
    return render(request, 'print_market/seo/page.html', _ctx(
        request, sv=None, gov=g, shops=shops, stores=stores, products=[], items=[], min_price=None,
        title=f'مطابع في {g[1]}', indexable=bool(shops or stores), crumbs=crumbs,
        seo_jsonld=_jsonld(crumbs, [], shops)))


def service_gov_page(request, service, gov):
    sv, g = _service(service), _gov(gov)
    shops = list(_pm_shops(g[1]).filter(Q(products__category=sv['pm_category'], products__is_active=True)
                                        | Q(products__isnull=True)).distinct().order_by(F('rating_avg').desc(nulls_last=True), '-created_at')[:12])
    all_local = _pm_shops(g[1]).exists()
    stores = _catalog_stores(gov)
    title = f"{sv['h1']} في {g[1]}"
    faqs = list(sv['faq']) + [(
        f"فيه مطابع في {g[1]} على Mouss Tec؟",
        (f"أيوه — فيه {len(shops) + len(stores)} مطبعة ومتجر في {g[1]} ظاهرين في الصفحة دي بتقييماتهم. "
         if (shops or stores) else
         f"لسه بنضيف مطابع في {g[1]}. ") + 'وتقدر تكتب طلبك والمطابع اللي بتوصل لمحافظتك تبعتلك عروضها.',
    )]
    crumbs = [_HOME, _ROOT, (sv['h1'], reverse('print_seo:service', args=[service])), (title, request.path)]
    return render(request, 'print_market/seo/page.html', _ctx(
        request, sv=sv, gov=g, shops=shops, stores=stores,
        products=_products(sv, g[1]) or _products(sv), items=_catalog_items(sv, gov), min_price=_min_price(sv, g[1]),
        title=title, indexable=bool(all_local or stores), crumbs=crumbs, faqs=faqs,
        seo_jsonld=_jsonld(crumbs, faqs, shops)))
