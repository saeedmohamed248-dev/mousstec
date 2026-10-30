"""💳 إشعارات Paymob بتوصل كلها على /payment/paymob/callback/ (الرابط الوحيد المضبوط في
الداشبورد — PAYMOB_SETUP.md)، و create_iframe_url بيتجاهل callback_url.

قبل التصليح: دفع القطع وترقية التشخيص بالكارت كان بيوصل للرابط ده ويرجع «اترفض» —
الفلوس تتخصم والطلب/الباقة مايتفعّلوش. وإشعار التشخيص نفسه كان بيدوّر على الباقة في
payment_key_claims.extra (مابتتبعتش أصلاً)، ومن غير ما يتأكد إن المبلغ يغطي الباقة،
وكل إعادة للإشعار كانت بتمدّ ٣٠ يوم. وزرار «اشترك بالفيزا» كان بيفعّل الباقة ببلاش
في أي سيرفر مفيهوش PAYMOB_API_KEY.
"""
import json
from unittest import mock

from django.core.cache import cache
from django.test import Client as DjangoClient, TestCase, override_settings

from clients.models import CustomerDiagnosticsSubscription
from marketplace_c2c.tests.test_parts_order_lifecycle import (
    _PublicDomainMixin, _customer, _listing, _login, _pending_order,
)

CALLBACK = '/payment/paymob/callback/'


def _txn(order_id, amount_cents, txn_id=501, success=True):
    return {'type': 'TRANSACTION', 'obj': {
        'id': txn_id, 'success': success, 'amount_cents': amount_cents,
        'order': {'id': order_id},
    }}


@mock.patch('clients.services.paymob.verify_paymob_hmac', return_value=(True, 'ok'))
class PaymobCallbackRoutingTests(_PublicDomainMixin, TestCase):

    def setUp(self):
        cache.clear()

    def _post(self, payload):
        return DjangoClient().post(CALLBACK, data=json.dumps(payload), content_type='application/json')

    def _diag_order(self, order_id, tier='pro'):
        customer = _customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        cache.set(f'paymob_diag_{order_id}', {'customer_pk': customer.pk, 'tier': tier}, 7200)
        return sub

    # ── قطع الغيار ────────────────────────────────────────────────────
    def test_parts_payment_on_main_callback_marks_order_paid(self, _hmac):
        order = _pending_order(_listing(_customer()), _customer(), paymob_order_id='PM-R1')
        res = self._post(_txn('PM-R1', 100000))
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid_held')

    def test_parts_browser_return_goes_to_orders_page(self, _hmac):
        _pending_order(_listing(_customer()), _customer(), paymob_order_id='PM-R2')
        res = DjangoClient().get(CALLBACK, {'success': 'true', 'order': 'PM-R2', 'amount_cents': '100000'})
        self.assertEqual(res.status_code, 302)
        self.assertIn('/marketplace/parts/orders/', res['Location'])

    def test_parts_underpayment_is_not_applied(self, _hmac):
        order = _pending_order(_listing(_customer()), _customer(), paymob_order_id='PM-R3')
        self._post(_txn('PM-R3', 100))
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending_payment')

    # ── التشخيص ───────────────────────────────────────────────────────
    def test_diag_payment_on_main_callback_upgrades_tier(self, _hmac):
        sub = self._diag_order('PM-D1', 'pro')
        res = self._post(_txn('PM-D1', 19900, txn_id=901))
        self.assertEqual(res.status_code, 200, res.content)
        sub.refresh_from_db()
        self.assertEqual(sub.tier, 'pro')
        self.assertEqual(sub.last_payment_ref, 'paymob-901')

    def test_diag_underpayment_does_not_upgrade(self, _hmac):
        sub = self._diag_order('PM-D2', 'empire')
        res = self._post(_txn('PM-D2', 9900))   # دفع سعر Basic وطالب Empire
        self.assertEqual(res.status_code, 400)
        sub.refresh_from_db()
        self.assertEqual(sub.tier, 'trial')

    def test_diag_repeated_notification_extends_once(self, _hmac):
        sub = self._diag_order('PM-D3', 'basic')
        self._post(_txn('PM-D3', 9900, txn_id=903))
        sub.refresh_from_db()
        first_until = sub.paid_until
        res = self._post(_txn('PM-D3', 9900, txn_id=903))
        self.assertEqual(res.json()['status'], 'duplicate')
        sub.refresh_from_db()
        self.assertEqual(sub.paid_until, first_until)

    def test_diag_browser_return_goes_to_diagnostics_page(self, _hmac):
        self._diag_order('PM-D4')
        res = DjangoClient().get(CALLBACK, {'success': 'true', 'order': 'PM-D4'})
        self.assertEqual(res.status_code, 302)
        self.assertIn('/marketplace/diagnostics/', res['Location'])


class DiagnosticsCardWithoutGatewayTests(_PublicDomainMixin, TestCase):

    @override_settings(PAYMOB_API_KEY='', DEBUG=False)
    def test_no_gateway_in_production_does_not_activate_for_free(self):
        customer = _customer()
        CustomerDiagnosticsSubscription.grant_trial(customer)
        res = _login(customer).get('/marketplace/diagnostics/upgrade/empire/')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/payment/manual/diagnostics/empire/start/', res['Location'])
        sub = CustomerDiagnosticsSubscription.objects.get(customer=customer)
        self.assertEqual(sub.tier, 'trial')

    @override_settings(PAYMOB_API_KEY='', DEBUG=False)
    def test_no_gateway_post_returns_manual_redirect(self):
        customer = _customer()
        CustomerDiagnosticsSubscription.grant_trial(customer)
        res = _login(customer).post('/marketplace/diagnostics/upgrade/pro/')
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()['redirect'], '/payment/manual/diagnostics/pro/start/')
        self.assertEqual(CustomerDiagnosticsSubscription.objects.get(customer=customer).tier, 'trial')
