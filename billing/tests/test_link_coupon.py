"""🎟️ كود خصم الإعلان — «50% على كل الباقات ليوم واحد» زي MUSSTEC2026 بالظبط.

1) الكود نفسه: بيتطبّق على باقات السيارات والمطابع، الفاتورة بنص التمن، مرة لكل شركة، وبيقف
   بعد الـ 24 ساعة — والفاتورة اللي اتفتحت جوه اليوم بتفضل بالنص حتى لو اتراجعت بعده.
2) رابط الإعلان (?coupon=): شريط الكود + العدّاد، والكروت بالسعر المشطوب والسعر بعد الكود،
   والكود بيتحفظ ويتكتب لوحده في التسجيل والدفع.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from billing.tests.test_coupons_and_referrals import _NoSchemaMixin, _coupon, _shop
from billing.tests.test_promotions_and_plan_admin import _plan
from clients.models import Coupon, PlatformInvoice
from clients.services import platform_billing as pb
from clients.services.link_coupon import COOKIE
from clients.models import ManualPaymentReceipt
from clients.views.subscription_views import pay_token_for
from inventory.tests.base import ERPTenantTestCase
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

CODE = 'MUSSTEC2026'


def _one_day_half_off(**kw):
    now = timezone.now()
    return _coupon(CODE, percent_off=50, starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(hours=24),
                   label='عرض يوم واحد', **kw)


class OneDayHalfOffCouponTests(_NoSchemaMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.gold = _plan('auto-gold', '800')
        self.pro = _plan('print-pro', '1250', industry='printing')
        self.coupon = _one_day_half_off()

    def test_half_price_on_car_and_print_plans(self):
        car, printer = _shop('half_car'), _shop('half_print')
        type(printer).objects.filter(pk=printer.pk).update(industry='printing')
        printer.industry = 'printing'
        for shop, plan, half in ((car, self.gold, '400.00'), (printer, self.pro, '625.00')):
            coupon, err = pb.resolve_coupon(' musstec2026 ', shop, plan)         # حروف صغيرة ومسافات عادي
            self.assertEqual((coupon, err), (self.coupon, None))
            inv = pb.open_plan_change_invoice(shop, plan, 1, provider='vodafone_cash', coupon=coupon)
            self.assertEqual(inv.total, Decimal(half))

    def test_annual_takes_half_off_the_whole_year(self):
        """الكود بيتخصم من إجمالي الفاتورة — سنوي = 800×12 بخصم السنوي 20% وبعدين النص."""
        inv = pb.open_plan_change_invoice(_shop('half_year'), self.gold, 12, provider='instapay', coupon=self.coupon)
        self.assertEqual(inv.total, Decimal('3840.00'))

    def test_trial_company_paying_today_keeps_its_free_days(self):
        shop = _shop('half_trial')
        self.assertEqual(shop.status, 'trial')
        inv = pb.open_plan_change_invoice(shop, self.gold, 1, provider='instapay', coupon=self.coupon)
        self.assertEqual(inv.period_start, shop.trial_ends_at)                  # المدفوع بيبدأ بعد التجربة
        self.assertEqual(inv.total, Decimal('400.00'))

    def test_once_per_company(self):
        shop = _shop('half_once')
        inv = pb.open_plan_change_invoice(shop, self.gold, 1, provider='instapay', coupon=self.coupon)
        pb.settle_issued_invoice(inv, provider='instapay', reference='TX-H1')
        self.assertEqual(pb.resolve_coupon(CODE, shop, self.gold)[1], 'استخدمت الكود ده قبل كده.')

    def test_stops_after_the_day_but_an_invoice_opened_inside_it_stays_half(self):
        shop = _shop('half_late')
        inv = pb.open_plan_change_invoice(shop, self.gold, 1, provider='vodafone_cash', coupon=self.coupon)
        tomorrow = timezone.now() + timedelta(hours=25)
        with mock.patch('django.utils.timezone.now', return_value=tomorrow):
            self.assertEqual(pb.resolve_coupon(CODE, _shop('half_new'), self.gold)[1], 'الكود ده مش شغّال دلوقتي.')
            # التحويل اتراجع بكرة: الفاتورة اتقفلت على النص وقت ما اتفتحت
            pb.settle_issued_invoice(inv, provider='vodafone_cash', reference='TX-H2')
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.total), ('paid', Decimal('400.00')))

    def test_signup_code_after_the_day_falls_back_to_full_price_quietly(self):
        """سجّل النهارده بالكود ودفع بعد ما الكود خلص ⇒ من غير خصم (عشان كده الشريط بيقول «لو دفعت قبل»)."""
        shop = _shop('half_signup', signup_coupon=CODE)
        self.assertEqual(pb.checkout_coupon(shop, self.gold), (self.coupon, None))
        with mock.patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(hours=25)):
            self.assertEqual(pb.checkout_coupon(shop, self.gold), (None, None))


@override_settings(ALLOWED_HOSTS=['*'])
class LinkCouponPagesTests(_PublicDomainMixin, TestCase):

    def setUp(self):
        self.gold = _plan('auto-gold', '800')
        self.pro = _plan('print-pro', '1250', industry='printing')
        Coupon.objects.filter(code__in=[CODE, 'PRINTONLY', 'OLDONE']).delete()
        self.coupon = _one_day_half_off()

    def tearDown(self):
        PlatformInvoice.objects.all().delete()
        Coupon.objects.all().delete()

    def test_pricing_link_shows_banner_struck_price_and_price_after_code(self):
        r = HttpClient().get('/pricing/', {'coupon': 'musstec2026'})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('id="mtCouponBar"', html)
        self.assertIn(f'<span class="mt-cpn-code">{CODE}</span>', html)
        self.assertIn('id="mtCouponLeft"', html)                                 # عدّاد لحد نهاية الكود
        self.assertIn('data-c-monthly="400.00"', html)                           # جولد 800 ⇒ 400
        self.assertIn('data-c-monthly="625.00"', html)                           # برو مطابع 1250 ⇒ 625
        self.assertIn('data-c-annual="3840.00"', html)
        self.assertIn('plan-old-price', html)                                    # السعر القديم مشطوب
        self.assertIn("document.cookie = 'mt_coupon='", html)                    # بيتحفظ للزيارة الجاية

    def test_remembered_code_shows_on_the_next_visit(self):
        c = HttpClient()
        c.cookies[COOKIE] = CODE
        html = c.get('/pricing/').content.decode()
        self.assertIn('data-c-monthly="400.00"', html)
        self.assertNotIn("document.cookie = 'mt_coupon=' + encodeURIComponent", html)   # مش من الرابط ⇒ مابيتجددش

    def test_home_page_banner_links_to_pricing_with_the_code(self):
        r = HttpClient().get('/', {'coupon': CODE}, follow=True)
        self.assertContains(r, f'/pricing/?coupon={CODE}')

    def test_code_for_print_plans_only_changes_print_cards(self):
        Coupon.objects.filter(code=CODE).delete()
        _coupon('PRINTONLY', percent_off=50, industry='printing')
        html = HttpClient().get('/pricing/', {'coupon': 'PRINTONLY'}).content.decode()
        self.assertIn('data-c-monthly="625.00"', html)
        self.assertNotIn('data-c-monthly="400.00"', html)

    def test_expired_or_unknown_code_shows_nothing_and_clears_the_cookie(self):
        now = timezone.now()
        _coupon('OLDONE', percent_off=50, starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1))
        for code in ('OLDONE', 'NOPE'):
            c = HttpClient()
            c.cookies[COOKIE] = code
            html = c.get('/pricing/').content.decode()
            self.assertNotIn('id="mtCouponBar"', html)
            self.assertNotIn('data-c-monthly', html)
            self.assertIn("mt_coupon=; path=/; max-age=0", html)

    def test_uae_site_has_no_banner(self):
        html = HttpClient().get('/pricing/', {'coupon': CODE, 'region': 'AE'}).content.decode()
        self.assertNotIn('id="mtCouponBar"', html)
        self.assertNotIn('data-c-monthly', html)

    def test_signup_carries_the_remembered_code_and_says_until_when(self):
        c = HttpClient()
        c.cookies[COOKIE] = CODE
        r = c.get('/connect/signup/')
        self.assertContains(r, f'<input type="hidden" name="ref" value="{CODE}">')
        self.assertContains(r, 'لو دفعت قبل')


# ═════════ الكود على الاشتراك الطويل بس (3/6/12 شهر) — طلب المالك لـ MUSSTEC2026 ═════════

class LongPlansOnlyCouponTests(_NoSchemaMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.gold = _plan('auto-gold', '800')
        self.coupon = _one_day_half_off(min_months=3)
        self.shop = _shop('long_only')

    def test_monthly_is_refused_three_months_and_longer_get_half(self):
        self.assertEqual(pb.resolve_coupon(CODE, self.shop, self.gold, 1)[1],
                         'الكود ده على الاشتراك 3 شهور أو 6 شهور أو سنة — مش الشهري.')
        for months, half in ((3, '1080.00'), (6, '2040.00'), (12, '3840.00')):   # 800×م×(خصم المدة)÷2
            coupon, err = pb.resolve_coupon(CODE, self.shop, self.gold, months)
            self.assertIsNone(err)
            inv = pb.open_plan_change_invoice(_shop(f'long_{months}'), self.gold, months, provider='instapay',
                                              coupon=coupon)
            self.assertEqual(inv.total, Decimal(half))

    def test_signup_code_skips_monthly_quietly(self):
        self.shop.signup_coupon = CODE
        self.assertEqual(pb.checkout_coupon(self.shop, self.gold, months=1), (None, None))
        self.assertEqual(pb.checkout_coupon(self.shop, self.gold, months=3), (self.coupon, None))

    def test_any_period_is_still_the_default(self):
        c = _coupon('ANYTIME', percent_off=10)
        self.assertEqual(c.min_months, 1)
        self.assertEqual(pb.resolve_coupon('ANYTIME', self.shop, self.gold, 1), (c, None))


@override_settings(ALLOWED_HOSTS=['*'])
class LongPlansOnlyPagesTests(_PublicDomainMixin, TestCase):

    def setUp(self):
        self.gold = _plan('auto-gold', '800')
        Coupon.objects.filter(code=CODE).delete()
        _one_day_half_off(min_months=3)

    def tearDown(self):
        PlatformInvoice.objects.all().delete()
        Coupon.objects.all().delete()

    def test_apply_button_refuses_monthly_and_discounts_quarterly(self):
        c = HttpClient()
        d = c.get('/pricing/coupon/', {'code': CODE, 'plan': 'gold', 'period': 'monthly'}).json()
        self.assertFalse(d['ok'])
        self.assertIn('مش الشهري', d['error'])
        d = c.get('/pricing/coupon/', {'code': CODE, 'plan': 'gold', 'period': 'quarterly'}).json()
        self.assertEqual((d['ok'], d.get('total')), (True, '1080.00'), d)

    def test_cards_show_the_code_on_long_periods_only_and_open_on_quarterly(self):
        html = HttpClient().get('/pricing/', {'coupon': CODE}).content.decode()
        self.assertNotIn('data-c-monthly', html)
        self.assertIn('data-c-quarterly="1080.00"', html)
        self.assertIn('data-c-annual="3840.00"', html)
        self.assertIn('style="display:none"', html)                  # الشهري من غير سعر مشطوب
        self.assertIn('لاشتراك 3 شهور أو أكتر', html)                # في الشريط
        self.assertIn('window.MT_LINK_COUPON_MIN = 3;', html)         # الصفحة تفتح على ربع سنوي

    def test_owner_sets_the_minimum_period(self):
        from django.contrib.auth import get_user_model
        owner = get_user_model().objects.create_user('owner2@plat.test', password='x', is_superuser=True, is_staff=True)
        c = HttpClient()
        c.force_login(owner)
        c.post('/superadmin/coupons/', {'code': 'longone', 'kind': 'percent', 'value': '30', 'target': 'all',
                                        'min_months': '6'})
        self.assertEqual(Coupon.objects.get(code='LONGONE').min_months, 6)
        launch = Coupon.objects.get(code=CODE)
        c.post('/superadmin/coupons/', {'action': 'min_months', 'coupon_id': launch.pk, 'min_months': '12'})
        launch.refresh_from_db()
        self.assertEqual(launch.min_months, 12)
        c.post('/superadmin/coupons/', {'action': 'min_months', 'coupon_id': launch.pk, 'min_months': '5'})
        launch.refresh_from_db()
        self.assertEqual(launch.min_months, 1)                         # قيمة غريبة ⇒ أي مدة


@override_settings(ALLOWED_HOSTS=['*'])
class LongPlansOnlyTransferTests(ERPTenantTestCase):
    """فودافون كاش/إنستاباي من صفحة الأسعار: نفس شرط المدة على الفاتورة اللي بتتفتح."""

    def setUp(self):
        super().setUp()
        from django.db import connection
        connection.set_schema_to_public()
        self.gold = _plan('auto-gold', '800')
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='automotive', country='EG')
        self.schema = self.tenant.schema_name
        Coupon.objects.filter(code=CODE).delete()
        _one_day_half_off(min_months=3)
        connection.set_tenant(self.tenant)

    def tearDown(self):
        from django.db import connection
        connection.set_schema_to_public()
        PlatformInvoice.objects.filter(tenant_id=self.tenant.pk).delete()
        ManualPaymentReceipt.objects.filter(tenant_id=self.tenant.pk).delete()
        Coupon.objects.all().delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _pay(self, period):
        return HttpClient().post('/payment/manual/subscription/start/', {
            'plan': 'gold', 'shop': self.schema, 'billing_period': period, 'payment_method': 'instapay',
            'amount': '2400', 'coupon': CODE, 'pt': pay_token_for(self.schema)}, HTTP_HOST=self.domain.domain)

    def test_monthly_refused_quarterly_half(self):
        from django.db import connection
        self._pay('monthly')
        connection.set_schema_to_public()
        self.assertFalse(PlatformInvoice.objects.filter(tenant_id=self.tenant.pk).exists())
        connection.set_tenant(self.tenant)
        self._pay('quarterly')
        connection.set_schema_to_public()
        receipt = ManualPaymentReceipt.objects.get(tenant_id=self.tenant.pk)
        self.assertEqual(receipt.amount, Decimal('1080.00'))                     # 800×3×0.9 ÷ 2
