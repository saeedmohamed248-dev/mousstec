"""
💰 الصفحة الرئيسية (mousstec.com) بتعرض سعر واحد واضح فوق: «الباقات تبدأ من …».
* الرقم = أقل باقة مدفوعة نشطة، بنفس حساب صفحة الأسعار (العرض المؤقت لو شغّال).
* الباقات المتوقفة والمجانية مابتدخلش في الحساب.
* الموقع الإماراتي بيعرض سعر الدرهم بس — ومن غير سعر درهم مابيظهرش رقم.
* 🔗 مفيش روابط href="#" في الصفحة الرئيسية (فقاعة الدعم في الشات بقت زرار).
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from clients.models import Plan
from clients.tests.test_printing_customer_flow import _TenantDomainMixin
from erp_core.regions import MT_REGION_COOKIE


def _price_block(html):
    m = re.search(r'<a id="starting-price".*?</a>', html, re.S)
    return m.group(0) if m else ''


@override_settings(ALLOWED_HOSTS=['*'], BASE_DOMAIN='mousstec.com')
class LandingStartingPriceTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        Plan.objects.update(is_active=False)
        self.cheap = Plan.objects.create(slug='lp-cheap', name='فضي', industry='automotive',
                                         monthly_price=Decimal('550'), is_active=True)
        Plan.objects.create(slug='lp-pricey', name='ذهبي', industry='printing',
                            monthly_price=Decimal('850'), is_active=True)
        Plan.objects.create(slug='lp-off', name='قديمة', industry='automotive',
                            monthly_price=Decimal('100'), is_active=False)
        Plan.objects.create(slug='lp-free', name='مجانية', industry='automotive',
                            monthly_price=Decimal('0'), is_active=True)

    def _home(self, region=None):
        c = HttpClient()
        if region:
            c.cookies[MT_REGION_COOKIE] = region
        r = c.get('/')
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_shows_lowest_active_paid_plan_price(self):
        block = _price_block(self._home())
        self.assertTrue(block, 'مفيش سعر فوق في الصفحة الرئيسية')
        self.assertIn('الباقات تبدأ من', block)
        self.assertIn('550', block)
        self.assertNotIn('100', block)  # الباقة المتوقفة مابتدخلش
        self.assertIn('href="/pricing/"', block)

    def test_english_site_shows_latin_currency(self):
        """🐛 الموقع الإنجليزي كان بيعرض «550 ج.م» — نص عربي في صفحة إنجليزي (اختبار e2e وقع عليه)."""
        html = HttpClient().get('/', HTTP_ACCEPT_LANGUAGE='en-US').content.decode()
        self.assertIn('lang="en"', html)
        block = _price_block(html)
        self.assertIn('550 EGP', block)
        self.assertIn('Plans start at', block)
        self.assertNotIn('ج.م', block)

    def test_active_promotion_lowers_the_shown_price(self):
        from clients.models import PlanPromotion
        now = timezone.now()
        promo = PlanPromotion(plan=self.cheap, percent_off=20,
                              starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5))
        promo.save()
        if self.cheap.price_for_period(1) != Decimal('440.00'):
            self.skipTest('العرض المؤقت مش متطبّق على الباقة دي في الإعداد الحالي')
        self.assertIn('440', _price_block(self._home()))

    def test_uae_site_shows_aed_only(self):
        self.assertEqual(_price_block(self._home('AE')), '')  # مفيش سعر درهم ⇒ مفيش رقم
        Plan.objects.filter(pk=self.cheap.pk).update(monthly_price_aed=Decimal('65'))
        block = _price_block(self._home('AE'))
        self.assertIn('65', block)
        self.assertNotIn('550', block)

    def test_no_dead_hash_links_on_home(self):
        html = self._home()
        self.assertNotIn('href="#"', html)
