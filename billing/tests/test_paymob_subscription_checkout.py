"""💳 الاشتراك بالكارت (Paymob) — الفاتورة بتتسجّل وقت الـ checkout مش في الكاش بس.

قبل كده بيانات الطلب (الشركة/الباقة/المدة) كانت في Redis ساعتين بس: لو اتمسح
أو إشعار Paymob اتأخر، العميل يدفع والاشتراك مايتفعّلش ومافيش أثر للدفعة.
وكمان الـ checkout من غير شركة كان بيدفّع والـ callback مالوش حاجة يفعّلها، وصفحة
الأسعار كانت بتعرض خصومات غير اللي بتتحصّل.
"""
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone

from billing.services.paymob import _PAYMOB_HMAC_FIELDS, _extract_paymob_fields, _hmac_value
from clients.models import Plan, PlatformInvoice, TenantSubscription
from inventory.tests.base import ERPTenantTestCase

SECRET = 'paymob-checkout-test-secret'
PAYMOB = dict(PAYMOB_API_KEY='k', PAYMOB_INTEGRATION_ID='123', PAYMOB_IFRAME_ID='456',
              PAYMOB_HMAC_SECRET=SECRET, PAYMOB_CARD_SUBSCRIPTIONS=True)


class _Resp:
    def __init__(self, payload):
        self.status_code, self._payload, self.text = 201, payload, json.dumps(payload)

    def json(self):
        return self._payload


def fake_paymob(order_id):
    def post(url, json=None, timeout=None):  # noqa: A002 — نفس توقيع requests.post
        if url.endswith('/auth/tokens'):
            return _Resp({'token': 'auth'})
        if url.endswith('/ecommerce/orders'):
            return _Resp({'id': order_id})
        if url.endswith('/payment_keys'):
            return _Resp({'token': 'pay-key'})
        raise AssertionError(url)
    return post


def signed_callback(order_id, amount_cents, success=True):
    body = {'type': 'TRANSACTION', 'obj': {
        'id': 777, 'amount_cents': amount_cents, 'created_at': '2026-10-19T10:00:00',
        'currency': 'EGP', 'error_occured': False, 'has_parent_transaction': False,
        'integration_id': 123, 'is_3d_secure': True, 'is_auth': False, 'is_capture': False,
        'is_refunded': False, 'is_standalone_payment': True, 'is_voided': False,
        'order': {'id': order_id}, 'owner': 1, 'pending': False,
        'source_data': {'pan': '1234', 'sub_type': 'Visa', 'type': 'card'}, 'success': success,
    }}
    fields = _extract_paymob_fields(body, {})
    body['hmac'] = hmac.new(SECRET.encode(), ''.join(_hmac_value(fields[k]) for k in _PAYMOB_HMAC_FIELDS)
                            .encode(), hashlib.sha512).hexdigest()
    return json.dumps(body)


@override_settings(ALLOWED_HOSTS=['*'], **PAYMOB)
class PaymobSubscriptionCheckoutTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        # TransactionTestCase بيفضّي الجداول بين الاختبارات — الباقة اللي المهاجرة زرعتها ممكن تكون اتمسحت
        self.plan, _ = Plan.objects.get_or_create(slug='auto-gold', defaults={
            'name': 'Gold', 'industry': 'automotive', 'monthly_price': Decimal('850.00'), 'is_active': True})
        self.c = HttpClient()
        self.today = timezone.localdate()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(subscription_end_date=self.today)
        self.tenant.refresh_from_db()

    def tearDown(self):
        connection.set_schema_to_public()
        PlatformInvoice.objects.filter(tenant=self.tenant).delete()
        TenantSubscription.objects.filter(tenant=self.tenant).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def checkout(self, order_id, period='quarterly', shop=None, token=True):
        from clients.views.subscription_views import pay_token_for
        shop = self.tenant.schema_name if shop is None else shop
        with mock.patch('requests.post', side_effect=fake_paymob(order_id)) as post:
            r = self.c.post('/payment/paymob/checkout/', {
                'plan': 'gold', 'billing_period': period, 'amount': '1', 'shop': shop,
                # توكن التسجيل (صاحب الشركة مش داخل على الدومين بتاعها)
                **({'pt': pay_token_for(shop)} if token and shop else {}),
            }, HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        return r, post

    def pay(self, order_id, amount_cents):
        r = self.c.post('/payment/paymob/callback/', data=signed_callback(order_id, amount_cents),
                        content_type='application/json', HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        return r

    def test_checkout_records_an_issued_invoice_for_the_order(self):
        r, _ = self.checkout(5001)
        self.assertIn('accept.paymob.com/api/acceptance/iframes/456?payment_token=pay-key', r['Location'])
        inv = PlatformInvoice.objects.get(payment_provider='paymob', payment_reference='5001')
        self.assertEqual(inv.status, 'issued')
        self.assertEqual(inv.tenant_id, self.tenant.pk)
        self.assertEqual(inv.billing_cycle_months, 3)
        self.assertEqual(inv.total, self.plan.price_for_period(3))   # مش المبلغ اللي جه من المتصفح

    def test_payment_activates_even_after_the_cache_is_gone(self):
        self.checkout(5002)
        cache.clear()   # Redis اتعمله restart / الإشعار اتأخر أكتر من ساعتين
        total = self.plan.price_for_period(3)
        r = self.pay(5002, int(total * 100))
        self.assertIn('/payment/success/', r['Location'])
        inv = PlatformInvoice.objects.get(payment_reference='5002')
        self.assertEqual(inv.status, 'paid')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.subscription_end_date, self.today + timedelta(days=90))
        self.assertEqual(self.tenant.status, 'active')
        self.assertEqual(self.tenant.plan, 'gold')
        sub = TenantSubscription.objects.get(tenant=self.tenant)
        self.assertTrue(sub.is_active)
        self.assertEqual(sub.plan_id, self.plan.pk)

        # Paymob بيبعت الإشعار مرتين (سيرفر + متصفح) — مايتمدّش مرتين
        self.pay(5002, int(total * 100))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.subscription_end_date, self.today + timedelta(days=90))
        self.assertEqual(PlatformInvoice.objects.filter(tenant=self.tenant, status='paid').count(), 1)

    def test_underpayment_is_not_applied(self):
        self.checkout(5003)
        r = self.pay(5003, 100)   # جنيه واحد
        self.assertIn('reason=amount_mismatch', r['Location'])
        self.assertEqual(PlatformInvoice.objects.get(payment_reference='5003').status, 'issued')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.subscription_end_date, self.today)

    def test_checkout_without_a_company_never_reaches_the_gateway(self):
        r, post = self.checkout(5004, shop='')
        post.assert_not_called()
        self.assertNotIn('paymob.com', r['Location'])
        self.assertFalse(PlatformInvoice.objects.filter(payment_reference='5004').exists())

    def test_a_new_checkout_voids_the_abandoned_one_but_a_late_payment_still_counts(self):
        self.checkout(5005)
        self.checkout(5006, period='monthly')
        self.assertEqual(PlatformInvoice.objects.get(payment_reference='5005').status, 'void')
        self.assertEqual(PlatformInvoice.objects.get(payment_reference='5006').status, 'issued')
        # العميل كمّل الدفع في التاب القديمة — الفلوس وصلت، يبقى يتفعّل
        self.pay(5005, int(self.plan.price_for_period(3) * 100))
        self.assertEqual(PlatformInvoice.objects.get(payment_reference='5005').status, 'paid')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.subscription_end_date, self.today + timedelta(days=90))

    def test_checkout_is_closed_while_card_subscriptions_are_off(self):
        with self.settings(PAYMOB_CARD_SUBSCRIPTIONS=False):
            r, post = self.checkout(5007)
        post.assert_not_called()
        self.assertNotIn('paymob.com', r['Location'])

    def test_pricing_page_shows_what_will_be_charged(self):
        r = self.c.get('/pricing/', {'shop': self.tenant.schema_name}, HTTP_HOST=self.domain.domain)
        html = r.content.decode()
        self.assertIn(f'data-p-annual="{self.plan.price_for_period(12)}"', html)
        self.assertIn(f'data-d-annual="{self.plan.annual_discount}"', html)
        self.assertIn('id="payCardBtn"', html)
        # مفتاح ناقص (أو HMAC — من غيره الإشعارات بتترفض) أو الزرار مقفول ⇒ «قريباً»
        for off in ({'PAYMOB_API_KEY': ''}, {'PAYMOB_HMAC_SECRET': ''}, {'PAYMOB_CARD_SUBSCRIPTIONS': False}):
            with self.settings(**off), mock.patch.dict('os.environ', {'PAYMOB_HMAC_SECRET': '', 'PAYMOB_API_KEY': ''}):
                r = self.c.get('/pricing/', {'shop': self.tenant.schema_name}, HTTP_HOST=self.domain.domain)
            self.assertNotIn('id="payCardBtn"', r.content.decode(), off)

    def test_price_for_period_uses_the_plan_discounts(self):
        self.assertEqual(self.plan.price_for_period(12),
                         (self.plan.monthly_price * 12 * (100 - self.plan.annual_discount) / 100)
                         .quantize(Decimal('0.01')))
        self.assertEqual(self.plan.price_for_period(1, Decimal('100')), Decimal('100.00'))

    def test_checkout_for_someone_elses_shop_is_refused(self):
        r, post = self.checkout(5101, token=False)
        self.assertEqual(r.status_code, 302)
        post.assert_not_called()
        self.assertFalse(PlatformInvoice.objects.filter(tenant=self.tenant).exists())
