"""
⭐ ثقة ونشاط حقيقي في كتالوج السوق:
* «اتطلب سعره N مرة»، «بيحصل دلوقتي»، «الأكتر طلباً» — من طلبات حقيقية بس، ومن غير اسم/رقم العميل.
* تقييم العملاء: بييجي من لينك بعد ما الطلب يتقفل «اتنفّذ» بس، تقييم واحد لكل طلب، والمتوسط بيظهر
  بعد ٣ تقييمات ظاهرة؛ قبل كده «جديد على السوق». المخفي مابيتحسبش.
* «اختيار Mouss Tec» و«تقييم Mouss Tec» بيتعرضوا بأسمائهم، والمتاجر المميزة بتطلع الأول.
* صفحة التجار بأرقام حقيقية.
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client as HttpClient, TestCase, override_settings

from clients.catalog_seed import seed_catalog
from clients.models import CatalogItem, CatalogQuoteRequest, CatalogReview, CatalogStore
from clients.services import catalog_stats
from clients.tests.test_printing_customer_flow import _TenantDomainMixin


@override_settings(ALLOWED_HOSTS=['*'], CATALOG_NOTIFY_SYNC=True, CATALOG_NOTIFY_EMAIL='',
                   CATALOG_NOTIFY_WHATSAPP='', SUPPORT_INBOX_EMAIL='', SUPPORT_WHATSAPP='')
class CatalogTrustTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)
        self.store = CatalogStore.objects.filter(sector='printing').first()
        self.item = self.store.items.first()
        self.owner = User.objects.create(username='trust_owner@test.com', is_staff=True, is_superuser=True)

    def _quote(self, name='سارة محمود', item=None):
        return CatalogQuoteRequest.objects.create(
            item=item or self.item, item_title=(item or self.item).title, store_name=self.store.name,
            sector='printing', name=name, phone='01099998888')

    def _done_with_review(self, rating, visible=True):
        q = self._quote()
        c = HttpClient(); c.force_login(self.owner)
        c.post(f'/superadmin/catalog/requests/{q.pk}/update/', {'status': 'done'})
        q.refresh_from_db()
        HttpClient().post(f'/marketplace/catalog/review/{q.review_token}/', {'rating': rating, 'comment': 'كويس', 'name': 'سارة محمود'})
        r = CatalogReview.objects.get(quote_request=q)
        if not visible:
            r.is_visible = False; r.save()
        return q, r

    def test_activity_is_real_and_anonymous(self):
        html = HttpClient().get('/marketplace/catalog/printing/').content.decode()
        self.assertNotIn('بيحصل دلوقتي', html)
        self.assertNotIn('الأكتر طلباً', html)
        self._quote(); self._quote()
        html = HttpClient().get('/marketplace/catalog/printing/').content.decode()
        self.assertIn('بيحصل دلوقتي', html)
        self.assertIn('الأكتر طلباً', html)
        self.assertNotIn('سارة', html)          # مفيش اسم عميل
        self.assertNotIn('01099998888', html)   # ولا رقمه
        item_html = HttpClient().get(f'/marketplace/catalog/item/{self.item.item_code}/').content.decode()
        self.assertIn('اتطلب سعره 2 مرات', item_html)
        self.assertEqual(catalog_stats.item_activity([self.item.pk])[self.item.pk]['week'], 2)

    def test_review_only_after_done_once(self):
        q = self._quote()
        c = HttpClient(); c.force_login(self.owner)
        c.post(f'/superadmin/catalog/requests/{q.pk}/update/', {'status': 'contacted'})
        q.refresh_from_db()
        self.assertIsNone(q.review_token)
        c.post(f'/superadmin/catalog/requests/{q.pk}/update/', {'status': 'done'})
        q.refresh_from_db()
        self.assertIsNotNone(q.review_token)
        page = c.get('/superadmin/catalog/requests/?status=done').content.decode()
        self.assertIn(f'/marketplace/catalog/review/{q.review_token}/', page)
        u = f'/marketplace/catalog/review/{q.review_token}/'
        self.assertEqual(HttpClient().post(u, {'rating': 9}).status_code, 200)
        self.assertFalse(CatalogReview.objects.exists())
        HttpClient().post(u, {'rating': 4, 'comment': 'شغل نضيف', 'name': 'سارة محمود'})
        HttpClient().post(u, {'rating': 1})                     # تاني مرة مابتعدّش
        r = CatalogReview.objects.get()
        self.assertEqual((r.rating, r.display_name), (4, 'سارة م.'))
        # طلب لسه مااتنفّذش ⇒ اللينك مايفتحش
        q.status = 'contacted'; q.save()
        self.assertEqual(HttpClient().get(u).status_code, 404)

    def test_rating_shows_after_three_visible_reviews(self):
        self.assertIn('جديد على السوق', HttpClient().get(f'/marketplace/catalog/store/{self.store.slug}/').content.decode())
        self._done_with_review(5); self._done_with_review(4)
        self._done_with_review(1, visible=False)   # مخفي (سبام) ⇒ مابيتحسبش
        html = HttpClient().get(f'/marketplace/catalog/store/{self.store.slug}/').content.decode()
        self.assertIn('جديد على السوق', html)
        self._done_with_review(3)
        self.assertEqual(catalog_stats.store_ratings([self.store.pk]), {self.store.pk: (4.0, 3)})
        html = HttpClient().get(f'/marketplace/catalog/store/{self.store.slug}/').content.decode()
        self.assertIn('★ 4.0', html)
        self.assertNotIn('جديد على السوق', html)

    def test_featured_and_platform_score_are_labelled_and_first(self):
        last = CatalogStore.objects.filter(sector='printing').order_by('-sort').first()
        last.is_featured = True; last.platform_score = '4.5'; last.save()
        html = HttpClient().get('/marketplace/catalog/printing/').content.decode()
        self.assertIn('اختيار Mouss Tec', html)
        self.assertIn('تقييم Mouss Tec', html)
        self.assertIn('4.5/5', html)
        stores_section = html.split('class="stores"')[1]
        self.assertLess(stores_section.find(last.name), stores_section.find(self.store.name))

    def test_sell_page_real_numbers(self):
        self._quote()
        r = HttpClient().get('/marketplace/sell/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['stats']['quotes'], 1)
        self.assertIn('/connect/signup/', r.content.decode())
        en = HttpClient(); en.cookies['django_language'] = 'en'
        self.assertEqual(en.get('/marketplace/sell/').status_code, 200)

    def test_admin_cannot_add_or_edit_review_stars(self):
        from clients.admin import CatalogReviewAdmin
        self.assertIn('rating', CatalogReviewAdmin.readonly_fields)
        self.assertIn('comment', CatalogReviewAdmin.readonly_fields)
        c = HttpClient(); c.force_login(self.owner)
        self.assertEqual(c.get('/secure-portal/clients/catalogreview/add/').status_code, 403)
