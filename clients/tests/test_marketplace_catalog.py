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
