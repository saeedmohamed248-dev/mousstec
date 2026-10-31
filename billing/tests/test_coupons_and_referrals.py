"""🎟️ أكواد الخصم + 🤝 برنامج الإحالة.

قبل كده مفيش أي طريقة تدّي عميل/مسوّق/إعلان كود خصم، ولا تكافئ شركة جابت شركة تانية —
الخصم الوحيد كان إن المالك يغيّر سعر الباقة نفسها للكل.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from billing.tests.test_promotions_and_plan_admin import _plan, _tenant
from clients.models import Coupon, ManualPaymentReceipt, PlatformInvoice, ReferralReward
from clients.services import platform_billing as pb
from clients.views.subscription_views import pay_token_for
from inventory.tests.base import ERPTenantTestCase
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()


def _shop(schema, **kw):
    return _tenant(schema, max_users=2, max_branches=1, max_treasuries=1, **kw)


class _NoSchemaMixin:
    """الشركات هنا صفوف في public بس — أي حفظ لشركة مالهاش سكيمة (تفعيل الاشتراك) بيخلّي
    django-tenants يبني لها سكيمة كاملة (دقيقة ونص لكل اختبار). في الإنتاج كل شركة ليها سكيمتها."""

    def setUp(self):
        patcher = mock.patch('django_tenants.models.schema_exists', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()


def _coupon(code='SAVE20', **kw):
    defaults = dict(percent_off=20)
    defaults.update(kw)
    return Coupon.objects.create(code=code, **defaults)


class CouponRulesTests(_NoSchemaMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.silver = _plan('auto-silver', '550')
        self.gold = _plan('auto-gold', '850')
        self.shop = _shop('cp_shop')

    def test_valid_coupon_discounts_the_invoice_once_and_is_recorded(self):
        c = _coupon()
        coupon, err = pb.resolve_coupon('save20 ', self.shop, self.silver)      # مسافات وحروف صغيرة عادي
        self.assertEqual((coupon, err), (c, None))
        inv = pb.open_plan_change_invoice(self.shop, self.silver, 1, provider='vodafone_cash', coupon=coupon)
        self.assertEqual((inv.total, inv.coupon_discount, inv.coupon), (Decimal('440.00'), Decimal('110.00'), c))
        self.assertEqual(inv.discount_amount, Decimal('110.00'))
        pb.settle_issued_invoice(inv, provider='vodafone_cash', reference='TX-1')
        self.assertEqual(pb.resolve_coupon('SAVE20', self.shop, self.silver)[1], 'استخدمت الكود ده قبل كده.')

    def test_fixed_amount_never_goes_below_zero_and_full_cover_activates_without_payment(self):
        _coupon('FREEMONTH', percent_off=0, amount_off=Decimal('999'))
        coupon, _ = pb.resolve_coupon('FREEMONTH', self.shop, self.silver)
        inv = pb.open_plan_change_invoice(self.shop, self.silver, 1, provider='vodafone_cash', coupon=coupon)
        self.assertEqual((inv.status, inv.total, inv.payment_provider), ('paid', Decimal('0.00'), 'coupon'))
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.status, 'active')

    def test_refusals(self):
        now = timezone.now()
        _coupon('OFF', is_active=False)
        _coupon('OLD', ends_at=now - timedelta(days=1), starts_at=now - timedelta(days=5))
        _coupon('SOON', starts_at=now + timedelta(days=1))
        _coupon('GOLDONLY', plan=self.gold)
        _coupon('PRINT', industry='printing')
        _coupon('ONE', max_redemptions=1)
        for code in ('NOPE', 'OFF', 'OLD', 'SOON', 'GOLDONLY', 'PRINT'):
            self.assertIsNotNone(pb.resolve_coupon(code, self.shop, self.silver)[1], code)
        other = _shop('cp_other')
        inv = pb.open_plan_change_invoice(other, self.silver, 1, provider='instapay',
                                          coupon=pb.resolve_coupon('ONE', other, self.silver)[0])
        pb.settle_issued_invoice(inv, provider='instapay', reference='TX-ONE')
        self.assertEqual(pb.resolve_coupon('ONE', self.shop, self.silver)[1], 'الكود ده خلص عدد استخداماته.')

    def test_new_customers_only(self):
        _coupon('WELCOME', new_customers_only=True)
        pb.issue_subscription_invoice(self.shop, self.silver, 1)                  # دفع قبل كده
        self.assertEqual(pb.resolve_coupon('WELCOME', self.shop, self.silver)[1], 'الكود ده لأول اشتراك بس.')
        gifted = _shop('cp_gift')
        pb.issue_subscription_invoice(gifted, self.silver, 1, gift=True)          # هدية من الإدارة مش «دفع»
        self.assertIsNone(pb.resolve_coupon('WELCOME', gifted, self.silver)[1])

    def test_signup_code_is_applied_automatically_on_first_payment(self):
        c = _coupon('ADS30', percent_off=30)
        self.shop.signup_coupon = 'ADS30'
        self.assertEqual(pb.checkout_coupon(self.shop, self.silver), (c, None))
        self.shop.signup_coupon = 'GONE'                                           # كود مابقاش صالح: من غير خطأ
        self.assertEqual(pb.checkout_coupon(self.shop, self.silver), (None, None))


class ReferralTests(_NoSchemaMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.silver = _plan('auto-silver', '550')
        self.referrer = _shop('ref_owner')
        pb.issue_subscription_invoice(self.referrer, self.silver, 1)
        self.referrer.refresh_from_db()
        self.code = pb.referral_coupon_for(self.referrer)

    def test_friend_gets_discount_and_referrer_gets_a_free_month_once(self):
        self.assertEqual(pb.referral_coupon_for(self.referrer), self.code)          # نفس الكود كل مرة
        end_before = self.referrer.subscription_end_date
        friend = _shop('ref_friend', signup_coupon=self.code.code)
        coupon, err = pb.checkout_coupon(friend, self.silver)
        self.assertIsNone(err)
        inv = pb.open_plan_change_invoice(friend, self.silver, 1, provider='vodafone_cash', coupon=coupon)
        self.assertEqual(inv.total, Decimal('495.00'))                               # 10%
        pb.settle_issued_invoice(inv, provider='vodafone_cash', reference='TX-F1')
        reward = ReferralReward.objects.get(referred=friend)
        self.assertEqual(reward.referrer, self.referrer)
        self.assertEqual(reward.gift_invoice.total, Decimal('0.00'))
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.subscription_end_date, end_before + timedelta(days=30))
        # تجديد الصاحب بعد كده مايدّيش مكافأة تانية، والكود مايشتغلش تاني عليه
        self.assertIsNotNone(pb.resolve_coupon(self.code.code, friend, self.silver)[1])
        self.assertEqual(ReferralReward.objects.count(), 1)

    def test_cannot_use_own_referral_code(self):
        self.assertEqual(pb.resolve_coupon(self.code.code, self.referrer, self.silver)[1],
                         'مينفعش تستخدم كود الإحالة بتاعك — ابعته لصحابك.')

    def test_referrer_still_on_trial_gets_a_longer_trial(self):
        trial_owner = _shop('ref_trial')
        before = trial_owner.trial_ends_at
        code = pb.referral_coupon_for(trial_owner)
        friend = _shop('ref_friend2')
        inv = pb.open_plan_change_invoice(friend, self.silver, 1, provider='instapay', coupon=code)
        pb.settle_issued_invoice(inv, provider='instapay', reference='TX-F2')
        trial_owner.refresh_from_db()
        self.assertEqual(trial_owner.trial_ends_at, max(before, timezone.localdate()) + timedelta(days=30))


@override_settings(ALLOWED_HOSTS=['*'])
class CouponCheckoutPagesTests(ERPTenantTestCase):
    """شركة بسكيمة حقيقية (التسعيرة بتعدّ مستخدمين/فروع الشركة جوه سكيمتها)."""

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        self.silver = _plan('auto-silver', '550')
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='automotive', country='EG')
        self.shop = type(self.tenant).objects.get(pk=self.tenant.pk)
        self.schema = self.shop.schema_name
        Coupon.objects.filter(code__in=['WEB20', 'FB-OCT', 'BAD']).delete()
        _coupon('WEB20')
        connection.set_tenant(self.tenant)

    def tearDown(self):
        connection.set_schema_to_public()
        PlatformInvoice.objects.filter(tenant_id=self.tenant.pk).delete()
        ManualPaymentReceipt.objects.filter(tenant_id=self.tenant.pk).delete()
        Coupon.objects.all().delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _host(self):
        return {'HTTP_HOST': self.domain.domain}

    def test_apply_button_returns_discounted_price(self):
        c = HttpClient()
        d = c.get('/pricing/coupon/', {'code': 'web20', 'plan': 'silver', 'period': 'monthly'},
                  **self._host()).json()
        self.assertEqual((d['ok'], d.get('total')), (True, '440.00'), d)
        d = c.get('/pricing/coupon/', {'code': 'web20', 'plan': 'silver', 'period': 'annual',
                                       'shop': self.schema, 'pt': pay_token_for(self.schema)}, **self._host()).json()
        self.assertEqual(d.get('total'), '4224.00', d)                               # 550×12×0.8×0.8
        self.assertFalse(c.get('/pricing/coupon/', {'code': 'NOPE', 'plan': 'silver'}, **self._host()).json()['ok'])

    def test_transfer_receipt_is_for_the_discounted_amount(self):
        r = HttpClient().post('/payment/manual/subscription/start/', {
            'plan': 'silver', 'shop': self.schema, 'billing_period': 'monthly', 'payment_method': 'instapay',
            'amount': '550', 'coupon': 'WEB20', 'pt': pay_token_for(self.schema)}, **self._host())
        self.assertEqual(r.status_code, 302)
        receipt = ManualPaymentReceipt.objects.get(tenant=self.shop)
        self.assertEqual(receipt.amount, Decimal('440.00'))
        self.assertEqual(PlatformInvoice.objects.get(pk=receipt.purchase_id).coupon.code, 'WEB20')

    def test_bad_code_at_transfer_is_refused_without_an_invoice(self):
        HttpClient().post('/payment/manual/subscription/start/', {
            'plan': 'silver', 'shop': self.schema, 'billing_period': 'monthly', 'payment_method': 'instapay',
            'amount': '550', 'coupon': 'NOPE', 'pt': pay_token_for(self.schema)}, **self._host())
        self.assertFalse(PlatformInvoice.objects.filter(tenant=self.shop).exists())

    @override_settings(PAYMOB_CARD_SUBSCRIPTIONS=True, PAYMOB_API_KEY='k', PAYMOB_IFRAME_ID='1',
                       PAYMOB_HMAC_SECRET='h', PAYMOB_INTEGRATION_ID='2')
    def test_card_amount_sent_to_paymob_equals_the_discounted_invoice(self):
        def fake_post(url, json=None, timeout=None):
            body = {'token': 't', 'id': 77001} if 'orders' in url or 'tokens' in url else {'token': 'pk'}
            return mock.Mock(status_code=201, json=lambda: body, text='')
        with mock.patch('requests.post', side_effect=fake_post) as post:
            HttpClient().post('/payment/paymob/checkout/', {
                'plan': 'silver', 'shop': self.schema, 'billing_period': 'monthly', 'amount': '550',
                'coupon': 'WEB20', 'pt': pay_token_for(self.schema)}, **self._host())
        order_call = next(c for c in post.call_args_list if 'orders' in c.args[0])
        self.assertEqual(order_call.kwargs['json']['amount_cents'], 44000)
        inv = PlatformInvoice.objects.get(tenant=self.shop, payment_reference='77001')
        self.assertEqual(inv.total, Decimal('440.00'))                               # الـ callback هيلاقيها مطابقة


@override_settings(ALLOWED_HOSTS=['*'])
class OwnerCouponsPageTests(_PublicDomainMixin, TestCase):

    def test_owner_creates_and_stops_a_coupon(self):
        owner = User.objects.create_user('owner@plat.test', password='x', is_superuser=True, is_staff=True)
        c = HttpClient()
        c.force_login(owner)
        c.post('/superadmin/coupons/', {'code': 'fb-oct', 'kind': 'percent', 'value': '15', 'target': 'all',
                                        'max_redemptions': '100', 'new_customers_only': 'on'})
        coupon = Coupon.objects.get(code='FB-OCT')
        self.assertEqual((coupon.percent_off, coupon.max_redemptions, coupon.new_customers_only), (15, 100, True))
        self.assertContains(c.get('/superadmin/coupons/'), 'FB-OCT')
        c.post('/superadmin/coupons/', {'action': 'toggle', 'coupon_id': coupon.pk})
        coupon.refresh_from_db()
        self.assertFalse(coupon.is_active)
        c.post('/superadmin/coupons/', {'code': 'BAD', 'kind': 'percent', 'value': '0', 'target': 'all'})
        self.assertFalse(Coupon.objects.filter(code='BAD').exists())
