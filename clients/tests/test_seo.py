"""
🔎 SEO للموقع كله (erp_core/seo.py):
* robots.txt و sitemap.xml موجودين (كانوا بيتحوّلوا للرئيسية) — ودومين الشركة Disallow: /.
* الـ sitemap فيه الصفحات الثابتة ومنتجات/متاجر الكتالوج وسوق المطابع، على الدومين الرئيسي https.
* كل صفحة عامة فيها canonical (من غير فلاتر البحث) + Open Graph + صورة مشاركة — ومايتكررش لو الصفحة حاطاه.
* الصفحات الخاصة عليها X-Robots-Tag: noindex.
* رابط مش موجود على الدومين الرئيسي = 404 حقيقي (مش 302 للرئيسية).
* JSON-LD صالح: Organization في الرئيسية، LocalBusiness + تقييم للمطبعة، Product + سعر لمنتج المطبعة.
"""
import json
import re

from django.core.cache import cache
from django.test import Client as HttpClient, TestCase, override_settings

from clients.tests.test_printing_customer_flow import _TenantDomainMixin


def _ld(html):
    return [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


@override_settings(ALLOWED_HOSTS=['*'], BASE_DOMAIN='mousstec.com')
class SeoTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.c = HttpClient()

    def test_robots_txt(self):
        r = self.c.get('/robots.txt')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'text/plain')
        body = r.content.decode()
        self.assertIn('Disallow: /secure-portal', body)
        self.assertIn('Disallow: /marketplace/print-market/order/', body)
        self.assertIn('Sitemap: https://mousstec.com/sitemap.xml', body)
        self.assertNotIn('Disallow: /\n', body)

    def test_sitemap_lists_public_pages_and_products(self):
        from print_market.models import MarketAccount, MarketProduct
        shop = MarketAccount(role='shop', name='مطبعة SEO', email='seo@shop.test', city='القاهرة')
        shop.set_password('x-Strong-2026-x')
        shop.save()
        prod = MarketProduct.objects.create(shop=shop, title='كروت', price_from=300)
        idx = self.c.get('/sitemap.xml')
        self.assertEqual(idx.status_code, 200)
        self.assertIn('sitemap-print-products.xml', idx.content.decode())
        pages = self.c.get('/sitemap-pages.xml').content.decode()
        for path in ('https://mousstec.com/', 'https://mousstec.com/pricing/',
                     'https://mousstec.com/marketplace/print-market/',
                     'https://mousstec.com/marketplace/catalog/printing/'):
            self.assertIn(f'<loc>{path}</loc>', pages)
        products = self.c.get('/sitemap-print-products.xml').content.decode()
        self.assertIn(f'https://mousstec.com/marketplace/print-market/product/{prod.pk}/', products)
        shops = self.c.get('/sitemap-print-shops.xml').content.decode()
        self.assertIn(f'/marketplace/print-market/shop/{shop.pk}/', shops)

    def test_public_page_gets_canonical_and_open_graph_once(self):
        html = self.c.get('/marketplace/print-market/?q=كروت&cat=cards').content.decode()
        self.assertEqual(html.count('rel="canonical"'), 1)
        self.assertIn('<link rel="canonical" href="https://mousstec.com/marketplace/print-market/">', html)
        self.assertIn('property="og:title"', html)
        self.assertIn('og-print-market.jpg', html)
        self.assertIn('name="twitter:card"', html)
        self.assertIn('name="description"', html)
        # ترقيم الصفحات بيفضل في الـ canonical
        html2 = self.c.get('/marketplace/print-market/?page=2').content.decode()
        self.assertIn('href="https://mousstec.com/marketplace/print-market/?page=2"', html2)

    def test_private_pages_are_noindex(self):
        for path in ('/marketplace/dashboard/', '/marketplace/print-market/my-orders/', '/superadmin/live/'):
            r = self.c.get(path)
            self.assertEqual(r.get('X-Robots-Tag'), 'noindex, nofollow', path)
        self.assertIsNone(self.c.get('/pricing/').get('X-Robots-Tag'))

    def test_missing_page_is_real_404(self):
        r = self.c.get('/this-page-does-not-exist-xyz/')
        self.assertEqual(r.status_code, 404)
        self.assertIn('404', r.content.decode())
        self.assertIn('/marketplace/print-market/', r.content.decode())

    def test_unknown_host_still_redirects_for_health_checks(self):
        # فحص الصحة في CI/النشر بيضرب 127.0.0.1 (مش دومين متسجّل) ومستني رد مش 4xx
        r = self.c.get('/', HTTP_HOST='127.0.0.1')
        self.assertIn(r.status_code, (200, 302))

    def test_structured_data(self):
        from print_market.models import MarketAccount, MarketOrder, MarketProduct, MarketReview
        home = _ld(self.c.get('/').content.decode())
        self.assertIn('Organization', [g['@type'] for d in home for g in d.get('@graph', [])])
        shop = MarketAccount(role='shop', name='مطبعة النجوم', email='ld@shop.test', city='الجيزة')
        shop.set_password('x-Strong-2026-x')
        shop.save()
        cust = MarketAccount(role='customer', name='عميل', email='ld@cust.test')
        cust.set_password('x-Strong-2026-x')
        cust.save()
        order = MarketOrder.objects.create(customer=cust, shop=shop, title='x', details='x', status='completed')
        MarketReview.objects.create(order=order, shop=shop, customer=cust, rating=4, comment='كويس')
        prod = MarketProduct.objects.create(shop=shop, title='بنر <script>', price_from=150)
        shop_ld = _ld(self.c.get(f'/marketplace/print-market/shop/{shop.pk}/').content.decode())
        biz = next(d for d in shop_ld if d['@type'] == 'LocalBusiness')
        self.assertEqual(biz['aggregateRating']['ratingValue'], 4.0)
        self.assertEqual(biz['address']['addressRegion'], 'الجيزة')
        html = self.c.get(f'/marketplace/print-market/product/{prod.pk}/').content.decode()
        self.assertNotIn('بنر <script>', html.split('application/ld+json')[1])   # مفيش حقن جوه الـ JSON
        item = next(d for d in _ld(html) if d['@type'] == 'Product')
        self.assertEqual(item['name'], 'بنر <script>')
        self.assertEqual(item['offers']['lowPrice'], '150.00')
        self.assertEqual(item['offers']['priceCurrency'], 'EGP')


@override_settings(ALLOWED_HOSTS=['*'], BASE_DOMAIN='mousstec.com')
class PrintGovernoratePagesTests(_TenantDomainMixin, TestCase):
    """🗺️ /print/<خدمة>/<محافظة>/: محتوى حقيقي، والمحافظة الفاضية noindex ومش في الـ sitemap."""

    def setUp(self):
        cache.clear()
        from print_market.models import MarketAccount, MarketProduct
        self.shop = MarketAccount(role='shop', name='مطبعة الدقي', email='gov@shop.test', city='الجيزة')
        self.shop.set_password('x-Strong-2026-x')
        self.shop.save()
        MarketProduct.objects.create(shop=self.shop, title='كروت مط', category='cards', price_from=275)
        self.c = HttpClient()

    def test_governorate_names_match_market_and_districts_map(self):
        from print_market.seo_data import GOVERNORATES, gov_of
        from print_market.views import GOVERNORATES as MARKET_GOVS
        self.assertEqual({g[1] for g in GOVERNORATES.values()}, set(MARKET_GOVS))
        self.assertEqual(gov_of('المعادي'), 'cairo')
        self.assertEqual(gov_of('الشيخ زايد'), 'giza')
        self.assertEqual(gov_of('طنطا'), 'gharbia')
        self.assertIsNone(gov_of('أونلاين'))

    def test_pages_render_with_real_data(self):
        html = self.c.get('/print/business-cards/giza/').content.decode()
        self.assertIn('طباعة كروت شخصية في الجيزة', html)
        self.assertIn('مطبعة الدقي', html)
        self.assertIn('الأسعار تبدأ من', html)
        self.assertIn('275', html)
        self.assertNotIn('noindex', html)
        self.assertIn('<link rel="canonical" href="https://mousstec.com/print/business-cards/giza/">', html)
        types = [d['@type'] for d in _ld(html)]
        self.assertIn('FAQPage', types)
        self.assertIn('BreadcrumbList', types)
        for url in ('/print/', '/print/business-cards/', '/print/in/giza/'):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertContains(self.c.get('/print/in/giza/'), 'مطبعة الدقي')

    def test_empty_governorate_is_noindex_and_not_in_sitemap(self):
        self.assertContains(self.c.get('/print/business-cards/aswan/'), 'content="noindex, follow"')
        sm = self.c.get('/sitemap-print-services.xml').content.decode()
        self.assertIn('https://mousstec.com/print/business-cards/giza/', sm)
        self.assertIn('https://mousstec.com/print/in/giza/', sm)
        self.assertNotIn('/print/in/aswan/', sm)
        self.assertNotIn('/print/business-cards/aswan/', sm)
        self.assertIn('https://mousstec.com/print/flyers-brochures/', sm)   # الخدمة نفسها دايماً

    def test_unknown_slugs_404(self):
        self.assertEqual(self.c.get('/print/not-a-service/').status_code, 404)
        self.assertEqual(self.c.get('/print/business-cards/not-a-gov/').status_code, 404)
        self.assertEqual(self.c.get('/print/in/not-a-gov/').status_code, 404)
