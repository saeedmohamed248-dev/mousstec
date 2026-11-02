"""🏷️ عرض المدد: خصم خاص لـ 3/6/12 شهر على كل الباقات لمدة محددة، والشهري بسعره.

- النسبة بتحل محل خصم المدة العادي (مش فوقه): سيلفر 550 ⇒ 3 شهور 1,155 · 6 شهور 1,980 · سنة 3,300.
- العرض بيخلص لوحده: بعد ميعاده الأسعار ترجع 10/15/20%.
- فاتورة الاشتراك بتطلع بنفس سعر الصفحة.
- ``--pause-coupon`` بيوقف الكود طول العرض (مايتجمعش 50% + 50%).
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from billing.tests.test_coupons_and_referrals import _coupon
from billing.tests.test_promotions_and_plan_admin import _plan
from clients.models import Coupon, PlanPromotion
from clients.services import platform_billing as pb
from clients.services.link_coupon import live_coupon
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin


class PeriodOfferPricingTests(TestCase):

    def setUp(self):
        self.silver = _plan('auto-silver', '550')
        self.print_plan = _plan('print-basic', '400', industry='printing')

    def test_offer_replaces_period_discount_and_keeps_monthly(self):
        call_command('start_period_offer', quarterly=30, semi=40, annual=50, days=7, stdout=None)
        p = self.silver
        self.assertEqual(p.price_for_period(1), Decimal('550.00'))
        self.assertEqual(p.price_for_period(3), Decimal('1155.00'))
        self.assertEqual(p.price_for_period(6), Decimal('1980.00'))
        self.assertEqual(p.price_for_period(12), Decimal('3300.00'))
        # المشطوب = سعر الباقة العادي (10/15/20%)
        self.assertEqual(p.price_for_period(3, promo=False), Decimal('1485.00'))
        self.assertEqual(p.price_for_period(12, promo=False), Decimal('5280.00'))
        # الطباعة كمان
        self.assertEqual(self.print_plan.price_for_period(12), Decimal('2400.00'))

    def test_offer_ends_by_itself(self):
        call_command('start_period_offer', days=7, stdout=None)
        PlanPromotion.objects.update(ends_at=timezone.now() - timedelta(seconds=1))
        fresh = type(self.silver).objects.get(pk=self.silver.pk)
        self.assertEqual(fresh.price_for_period(3), Decimal('1485.00'))
        self.assertEqual(fresh.price_for_period(1), Decimal('550.00'))

    def test_stop(self):
        call_command('start_period_offer', days=7, stdout=None)
        call_command('start_period_offer', stop=True, stdout=None)
        fresh = type(self.silver).objects.get(pk=self.silver.pk)
        self.assertEqual(fresh.price_for_period(12), Decimal('5280.00'))

    def test_new_offer_replaces_running_one(self):
        call_command('start_period_offer', annual=50, days=7, stdout=None)
        call_command('start_period_offer', annual=45, days=7, stdout=None)
        fresh = type(self.silver).objects.get(pk=self.silver.pk)
        self.assertEqual(fresh.price_for_period(12), Decimal('3630.00'))

    def test_invoice_uses_offer_price(self):
        from billing.tests.test_coupons_and_referrals import _shop
        call_command('start_period_offer', days=7, stdout=None)
        shop = _shop('offer_shop')
        inv = pb.open_plan_change_invoice(shop, self.silver, 12, provider='instapay')
        self.assertEqual(inv.total, Decimal('3300.00'))

    def test_pause_coupon(self):
        _coupon('MUSSTEC2026', percent_off=50)
        call_command('start_period_offer', days=7, pause=['musstec2026'], stdout=None)
        self.assertIsNone(live_coupon('MUSSTEC2026'))
        c = Coupon.objects.get(code='MUSSTEC2026')
        self.assertGreater(c.starts_at, timezone.now() + timedelta(days=6))


@override_settings(ALLOWED_HOSTS=['*'])
class PeriodOfferPageTests(_PublicDomainMixin, TestCase):

    def test_pricing_page_shows_offer(self):
        _plan('auto-silver', '550')
        call_command('start_period_offer', quarterly=30, semi=40, annual=50, days=7, stdout=None)
        html = HttpClient().get('/pricing/').content.decode()
        self.assertIn('خصم لحد 50%', html)
        self.assertIn('data-p-annual="3300.00"', html)
        self.assertIn('data-o-annual="5280.00"', html)
        self.assertIn('data-p-monthly="550.00"', html)
