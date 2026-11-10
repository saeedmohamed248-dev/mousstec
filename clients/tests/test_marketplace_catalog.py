"""
🛍️ كتالوج السوق (متاجر Mouss Tec):
* المحتوى المبدئي: ٢٥+ متجر لكل قطاع، كل متجر ١٠–٢٠ منتج، والكلام بالعامية ومفيش أسعار.
* الصفحات بتفتح (قطاع، قسم، بحث، ماركة، متجر، منتج) والشفافية «بيتنفّذ عن طريق Mouss Tec» ظاهرة.
* «اطلب سعر» بيسجّل طلب لصاحب المنصة، وبيرفض بيانات ناقصة، وفيه حد للطلبات.
* صفحة الطلبات لصاحب المنصة بس، وتغيير الحالة شغّال، والعداد في «محتاج قرارك».
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Count
from django.test import Client as HttpClient, TestCase, override_settings

from clients.catalog_seed import seed_catalog
from clients.models import CatalogItem, CatalogQuoteRequest, CatalogStore
from clients.tests.test_printing_customer_flow import _TenantDomainMixin


@override_settings(ALLOWED_HOSTS=['*'])
class CatalogTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)
        self.item = CatalogItem.objects.filter(store__sector='automotive').first()

    def test_seed_content(self):
        for sector in ('printing', 'automotive'):
            self.assertGreaterEqual(CatalogStore.objects.filter(sector=sector).count(), 25)
        counts = CatalogStore.objects.annotate(n=Count('items')).values_list('n', flat=True)
        self.assertTrue(all(10 <= n <= 20 for n in counts), sorted(counts))
        # الماركات الصيني والألماني موجودة
        makes = set(CatalogItem.objects.values_list('car_make', flat=True))
        for m in ('شيري', 'BMW', 'مرسيدس', 'MG', 'هيونداي'):
            self.assertIn(m, makes)
        titles = ' '.join(CatalogItem.objects.values_list('title', flat=True))
        for word in ('كوباية', 'أقلام', 'يونيفورم', 'بوجيهات', 'كاوتش', 'دفاتر'):
            self.assertIn(word, titles)
        # الإعادة مابتكرّرش
        before = CatalogItem.objects.count()
        seed_catalog(CatalogStore, CatalogItem)
        self.assertEqual(CatalogItem.objects.count(), before)

    def test_pages_render(self):
        c = HttpClient()
        store = CatalogStore.objects.filter(sector='printing').first()
        for url in ('/marketplace/catalog/printing/', '/marketplace/catalog/automotive/',
                    '/marketplace/catalog/printing/?cat=mugs', '/marketplace/catalog/automotive/?make=BMW&q=بوجيهات',
                    '/marketplace/catalog/printing/?page=3', f'/marketplace/catalog/store/{store.slug}/',
                    f'/marketplace/catalog/item/{self.item.item_code}/', '/marketplace/'):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
        html = c.get(f'/marketplace/catalog/item/{self.item.item_code}/').content.decode()
        self.assertIn('بيتنفّذ عن طريق Mouss Tec', html)
        self.assertIn('اطلب سعر', html)
        self.assertEqual(c.get('/marketplace/catalog/nothing/').status_code, 404)

    def test_quote_request_created_and_validated(self):
        c = HttpClient()
        url = f'/marketplace/catalog/item/{self.item.item_code}/'
        r = c.post(url, {'name': 'م', 'phone': '12'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CatalogQuoteRequest.objects.exists())
        r = c.post(url, {'name': 'أحمد', 'phone': '01011112222', 'car_info': 'إلنترا ٢٠١٨', 'details': 'أصلي'})
        self.assertEqual(r.status_code, 302)
        q = CatalogQuoteRequest.objects.get()
        self.assertEqual((q.item, q.status, q.sector, q.car_info), (self.item, 'new', 'automotive', 'إلنترا ٢٠١٨'))
        self.assertEqual(q.store_name, self.item.store.name)

    def test_quote_rate_limit(self):
        c = HttpClient()
        url = f'/marketplace/catalog/item/{self.item.item_code}/'
        for _ in range(15):
            c.post(url, {'name': 'أحمد', 'phone': '01011112222'})
        r = c.post(url, {'name': 'أحمد', 'phone': '01011112222'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(CatalogQuoteRequest.objects.count(), 15)

    def test_admin_requests_page_owner_only_and_status_update(self):
        q = CatalogQuoteRequest.objects.create(item=self.item, item_title=self.item.title, sector='automotive',
                                               name='أحمد', phone='01011112222')
        self.assertNotEqual(HttpClient().get('/superadmin/catalog/requests/').status_code, 200)
        owner = User.objects.create(username='cat_owner@test.com', is_staff=True, is_superuser=True)
        c = HttpClient(); c.force_login(owner)
        r = c.get('/superadmin/catalog/requests/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('أحمد', html)
        self.assertIn('https://wa.me/201011112222', html)
        c.post(f'/superadmin/catalog/requests/{q.pk}/update/', {'status': 'contacted', 'admin_notes': 'قلتله ٣٥٠'})
        q.refresh_from_db()
        self.assertEqual((q.status, q.admin_notes), ('contacted', 'قلتله ٣٥٠'))
        dash = c.get('/superadmin/')
        if dash.status_code == 200:
            self.assertNotIn('طلبات أسعار جديدة من الكتالوج', dash.content.decode())  # مفيش «جديد» دلوقتي


@override_settings(ALLOWED_HOSTS=['*'], CATALOG_NOTIFY_SYNC=True, CATALOG_NOTIFY_EMAIL='owner@mousstec.test',
                   CATALOG_NOTIFY_WHATSAPP='01094850763',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CatalogNotifyAndEnglishTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)
        self.item = CatalogItem.objects.filter(store__sector='automotive').exclude(car_make='').first()

    def test_owner_gets_email_and_whatsapp(self):
        from unittest import mock
        from django.core import mail
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa, \
                self.captureOnCommitCallbacks(execute=True):
            HttpClient().post(f'/marketplace/catalog/item/{self.item.item_code}/',
                              {'name': 'أحمد', 'phone': '01011112222', 'car_info': 'إلنترا ٢٠١٨', 'quantity': '٢'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@mousstec.test'])
        self.assertIn('01011112222', mail.outbox[0].body)
        self.assertIn(self.item.title, mail.outbox[0].body)
        wa.assert_called_once()
        self.assertEqual(wa.call_args[0][0], '01094850763')
        q = CatalogQuoteRequest.objects.get()
        self.assertEqual((q.notified_email, q.notified_whatsapp), (True, True))

    def test_failed_whatsapp_is_recorded_and_shown(self):
        from unittest import mock
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=False), \
                self.captureOnCommitCallbacks(execute=True):
            HttpClient().post(f'/marketplace/catalog/item/{self.item.item_code}/', {'name': 'أحمد', 'phone': '01011112222'})
        q = CatalogQuoteRequest.objects.get()
        self.assertEqual((q.notified_email, q.notified_whatsapp), (True, False))
        owner = User.objects.create(username='cat_owner2@test.com', is_staff=True, is_superuser=True)
        c = HttpClient(); c.force_login(owner)
        self.assertIn('الإشعار ماوصلش', c.get('/superadmin/catalog/requests/').content.decode())

    def test_english_pages(self):
        c = HttpClient()
        c.cookies['django_language'] = 'en'
        html = c.get('/marketplace/catalog/automotive/?make=' + self.item.car_make).content.decode()
        self.assertIn('lang="en"', html)
        self.assertIn('dir="ltr"', html)
        self.assertIn('Spare parts', html)
        self.assertIn(self.item.title_en, html)
        self.assertIn(f'>{self.item.car_make_en}<', html)
        html = c.get(f'/marketplace/catalog/item/{self.item.item_code}/').content.decode()
        for s in (self.item.title_en, 'Request a quote', 'Fulfilled by Mouss Tec', self.item.store.name_en):
            self.assertIn(s, html)
        self.assertNotIn(self.item.title + '<', html)
        html = c.get('/marketplace/catalog/printing/?cat=mugs').content.decode()
        self.assertIn('Mugs', html)
        r = c.post(f'/marketplace/catalog/item/{self.item.item_code}/', {'name': 'A', 'phone': '1'})
        self.assertIn('Write your name', r.content.decode())


@override_settings(ALLOWED_HOSTS=['*'])
class DashboardMarketEntryTests(_TenantDomainMixin, TestCase):
    """🛍️ العميل أول ما يدخل لوحته يلاقي زرار السوق بارز (قطاعه + لينك للقطاع التاني)."""

    def setUp(self):
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)

    def _dash(self, sector):
        import uuid
        from clients.models import MarketplaceCustomer
        cust = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='عميل', phone=f'+2010{uuid.uuid4().int % 10**8:08d}',
            sector=sector, is_verified=True)
        c = HttpClient(); c.cookies['mp_session'] = str(cust.session_token)
        return c.get('/marketplace/dashboard/').content.decode()

    def test_market_button_first_and_points_to_customer_sector(self):
        for sector, other in (('printing', 'automotive'), ('automotive', 'printing')):
            html = self._dash(sector)
            n = CatalogItem.objects.filter(store__sector=sector, is_active=True).count()
            self.assertIn('class="market-card"', html)
            self.assertIn(f'href="/marketplace/catalog/{sector}/" class="market-card"', html)
            self.assertIn(f'href="/marketplace/catalog/{sector}/" class="market-btn"', html)
            self.assertIn(f'/marketplace/catalog/{other}/', html)
            self.assertIn(f'{n} منتج', html)
            # الكارت قبل «أهلاً» (أول حاجة في الصفحة)
            self.assertLess(html.find('class="market-card"'), html.find('<section class="hero">'))
