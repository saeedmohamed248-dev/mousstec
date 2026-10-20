"""🔁 التجديد التلقائي بالكارت المحفوظ (clients/services/auto_renew.py).

الدورة: «احفظ الكارت» في صفحة Paymob ⇒ type=TOKEN ⇒ العميل يفعّل التجديد بنفسه ⇒
كل يوم قبل نهاية الاشتراك بيوم: فاتورة «مصدرة» بمرجع الطلب ⇒ خصم من الكارت ⇒ تسوية.
الاختبارات بتثبت: مفيش خصم من غير موافقة، مفيش خصمين في يوم، مفيش تمديد مرتين لما
الـ webhook يوصل بعد التسوية، والرد الضايع وسط الخصم مايتعادش عليه.
"""
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone

from billing.services.paymob import _PAYMOB_TOKEN_HMAC_FIELDS, _hmac_value
from billing.tests.test_paymob_subscription_checkout import PAYMOB, SECRET, _Resp, signed_callback
from clients.models import (
    PlatformEvent, PlatformInvoice, Plan, SavedPaymentCard, TenantSubscription,
)
from clients.services import auto_renew
from clients.services.platform_billing import issue_subscription_invoice
from inventory.tests.base import ERPTenantTestCase

AUTO = dict(PAYMOB, PAYMOB_AUTO_RENEW=True, PAYMOB_MOTO_INTEGRATION_ID='999')


def token_callback(order_id, token='tok_4242', masked='xxxx-xxxx-xxxx-4242', secret=SECRET):
    obj = {'id': 55, 'token': token, 'masked_pan': masked, 'merchant_id': 1,
           'card_subtype': 'MasterCard', 'created_at': '2026-10-19T10:00:00',
           'email': 'owner@shop.test', 'order_id': str(order_id)}
    sig = hmac.new(secret.encode(), ''.join(_hmac_value(obj[k]) for k in _PAYMOB_TOKEN_HMAC_FIELDS).encode(),
                   hashlib.sha512).hexdigest()
    return json.dumps({'type': 'TOKEN', 'obj': obj, 'hmac': sig})


class FakeGateway:
    """Paymob مزيّف: بيسجّل الطلبات ويرد بنتيجة الخصم المطلوبة."""

    def __init__(self, order_id, pay=('ok',)):
        self.order_id, self.pay_results, self.calls = order_id, list(pay), []

    def __call__(self, url, json=None, timeout=None):  # noqa: A002 — نفس توقيع requests.post
        self.calls.append((url, json))
        if url.endswith('/auth/tokens'):
            return _Resp({'token': 'auth'})
        if url.endswith('/ecommerce/orders'):
            return _Resp({'id': self.order_id})
        if url.endswith('/payment_keys'):
            return _Resp({'token': 'pay-key'})
        if url.endswith('/payments/pay'):
            result = self.pay_results.pop(0) if self.pay_results else 'ok'
            if result == 'lost':
                raise requests.ConnectionError('reset by peer')
            return _Resp({'id': 991, 'success': result == 'ok'})
        raise AssertionError(url)

    def sent(self, suffix):
        return [body for url, body in self.calls if url.endswith(suffix)]


@override_settings(ALLOWED_HOSTS=['*'], **AUTO)
class AutoRenewTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        self.plan, _ = Plan.objects.get_or_create(slug='auto-gold', defaults={
            'name': 'Gold', 'industry': 'automotive', 'monthly_price': Decimal('850.00'), 'is_active': True})
        self.today = timezone.localdate()
        self.c = HttpClient()
        # مشترك ٣ شهور واشتراكه بيخلص بكرة
        issue_subscription_invoice(self.tenant, self.plan, 3)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            subscription_end_date=self.today + timedelta(days=1), status='active')
        self.tenant.refresh_from_db()
        self.sub = TenantSubscription.objects.get(tenant=self.tenant)

    def tearDown(self):
        connection.set_schema_to_public()
        SavedPaymentCard.objects.filter(tenant=self.tenant).delete()
        PlatformInvoice.objects.filter(tenant=self.tenant).delete()
        TenantSubscription.objects.filter(tenant=self.tenant).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    # ── helpers ───────────────────────────────────────────────────────
    def save_card(self, order_id=7001, body=None):
        PlatformInvoice.objects.get_or_create(
            tenant=self.tenant, payment_provider='paymob', payment_reference=str(order_id),
            defaults=dict(subscription=self.sub, plan_revision=PlatformInvoice.objects.filter(
                tenant=self.tenant).first().plan_revision, period_start=self.today, period_end=self.today,
                billing_cycle_months=3, subtotal=Decimal('1'), total=Decimal('1'), status='paid'))
        r = self.c.post('/payment/paymob/callback/', data=body or token_callback(order_id),
                        content_type='application/json', HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        return r

    def turn_on(self):
        self.save_card()
        self.assertTrue(auto_renew.set_auto_renew(self.tenant, True))

    def run_renewals(self, gateway, day=None):
        with mock.patch('requests.post', side_effect=gateway):
            return auto_renew.renew_due_subscriptions(day or self.today)

    def end_date(self):
        self.tenant.refresh_from_db()
        return self.tenant.subscription_end_date

    # ── حفظ الكارت ────────────────────────────────────────────────────
    def test_saved_card_belongs_to_the_tenant_of_the_paid_order(self):
        r = self.save_card(7001)
        self.assertEqual(r.json(), {'status': 'saved'})
        card = SavedPaymentCard.objects.get(tenant=self.tenant)
        self.assertEqual((card.last4, card.token, card.is_active), ('4242', 'tok_4242', True))
        # كارت جديد بيحل محل القديم
        self.save_card(7002, token_callback(7002, token='tok_1111', masked='xxxx-1111'))
        self.assertEqual(auto_renew.active_card(self.tenant).last4, '1111')
        self.assertEqual(SavedPaymentCard.objects.filter(tenant=self.tenant, is_active=True).count(), 1)

    def test_token_callback_with_a_forged_signature_is_rejected(self):
        r = self.save_card(7001, token_callback(7001, secret='not-the-secret'))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(SavedPaymentCard.objects.exists())

    def test_card_for_an_unknown_order_is_not_saved(self):
        r = self.c.post('/payment/paymob/callback/', data=token_callback(424242),
                        content_type='application/json', HTTP_HOST=self.domain.domain)
        self.assertEqual(r.json(), {'status': 'not_found'})
        self.assertFalse(SavedPaymentCard.objects.exists())

    @override_settings(PAYMOB_AUTO_RENEW=False)
    def test_switch_off_keeps_no_cards_and_never_charges(self):
        self.assertEqual(self.save_card().json(), {'status': 'ignored'})
        TenantSubscription.objects.filter(pk=self.sub.pk).update(auto_renew=True)
        gw = FakeGateway(8001)
        self.assertEqual(self.run_renewals(gw), {})
        self.assertEqual(gw.calls, [])

    # ── الموافقة ──────────────────────────────────────────────────────
    def test_no_charge_without_the_owner_turning_it_on(self):
        self.save_card()
        gw = FakeGateway(8001)
        self.assertEqual(self.run_renewals(gw), {})
        self.assertEqual(gw.calls, [])

    def test_owner_turns_it_on_and_off_from_the_subscription_page(self):
        from django.contrib.auth import get_user_model
        self.save_card()
        connection.set_tenant(self.tenant)
        # مستخدم واحد (حد الباقة): موظف عادي الأول، وبعدين صاحب الشركة
        user = get_user_model().objects.create_user('ar_owner', password='x')
        c = HttpClient()
        c.force_login(user)
        c.post('/subscription/manage/', {'action': 'auto_renew_on'}, HTTP_HOST=self.domain.domain)
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.auto_renew)               # موظف عادي مايقدرش
        type(user).objects.filter(pk=user.pk).update(is_superuser=True, is_staff=True)
        r = c.get('/subscription/manage/', HTTP_HOST=self.domain.domain)
        self.assertContains(r, '4242')
        c.post('/subscription/manage/', {'action': 'auto_renew_on'}, HTTP_HOST=self.domain.domain)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.auto_renew)
        c.post('/subscription/manage/', {'action': 'remove_card'}, HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.auto_renew)
        self.assertIsNone(auto_renew.active_card(self.tenant))
        self.assertFalse(auto_renew.set_auto_renew(self.tenant, True))   # من غير كارت لأ

    # ── الخصم ─────────────────────────────────────────────────────────
    def test_renewal_charges_the_saved_card_and_extends_once(self):
        self.turn_on()
        old_end = self.end_date()
        gw = FakeGateway(8001)
        self.assertEqual(self.run_renewals(gw), {'renewed': 1})

        price = self.plan.price_for_period(3)
        key = gw.sent('/payment_keys')[0]
        self.assertEqual(key['amount_cents'], int(price * 100))     # السعر من السيرفر
        self.assertEqual(key['integration_id'], 999)                # MOTO
        self.assertEqual(gw.sent('/payments/pay')[0]['source'], {'identifier': 'tok_4242', 'subtype': 'TOKEN'})
        inv = PlatformInvoice.objects.get(payment_provider='paymob', payment_reference='8001')
        self.assertEqual((inv.status, inv.total, inv.billing_cycle_months), ('paid', price, 3))
        self.assertEqual(self.end_date(), old_end + timedelta(days=90))
        self.assertTrue(PlatformEvent.objects.filter(tenant_schema=self.tenant.schema_name,
                                                     event_type='payment').exists())

        # نفس اليوم تاني ⇒ مفيش خصم تاني
        gw2 = FakeGateway(8002)
        self.assertEqual(self.run_renewals(gw2), {})
        self.assertEqual(gw2.calls, [])
        # إشعار Paymob للعملية دي بيوصل بعدين ⇒ مايتمدّش تاني
        self.c.post('/payment/paymob/callback/', data=signed_callback(8001, int(price * 100)),
                    content_type='application/json', HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        self.assertEqual(self.end_date(), old_end + timedelta(days=90))
        self.assertEqual(PlatformInvoice.objects.filter(tenant=self.tenant, status='paid').count(), 3)

    def test_not_charged_before_the_renewal_window(self):
        self.turn_on()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            subscription_end_date=self.today + timedelta(days=10))
        gw = FakeGateway(8001)
        self.assertEqual(self.run_renewals(gw), {})
        self.assertEqual(gw.calls, [])

    def test_declined_card_retries_once_a_day_then_stops(self):
        self.turn_on()
        old_end = self.end_date()
        for n, day in enumerate((self.today, self.today + timedelta(days=1)), start=1):
            gw = FakeGateway(8100 + n, pay=('declined',))
            self.assertEqual(self.run_renewals(gw, day), {'declined': 1})
            self.assertEqual(self.run_renewals(FakeGateway(9999), day), {})   # مرة واحدة في اليوم
            self.sub.refresh_from_db()
            self.assertEqual((self.sub.auto_renew_failures, self.sub.auto_renew), (n, True))
            self.assertEqual(PlatformInvoice.objects.get(payment_reference=str(8100 + n)).status, 'void')
        self.assertEqual(self.run_renewals(FakeGateway(8103, pay=('declined',)), self.today + timedelta(days=2)),
                         {'disabled': 1})
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.auto_renew)
        self.assertEqual(self.end_date(), old_end)                   # مفيش تمديد من غير فلوس
        self.assertEqual(self.run_renewals(FakeGateway(8104), self.today + timedelta(days=3)), {})

    def test_lost_gateway_answer_is_never_retried_but_settles_if_paid(self):
        self.turn_on()
        old_end = self.end_date()
        self.assertEqual(self.run_renewals(FakeGateway(8200, pay=('lost',))), {'unknown': 1})
        inv = PlatformInvoice.objects.get(payment_reference='8200')
        self.assertEqual(inv.status, 'issued')                       # مستنية الـ webhook
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.auto_renew)                        # مفيش خصم تاني على العمى
        self.assertEqual(self.run_renewals(FakeGateway(8201), self.today + timedelta(days=1)), {})
        # Paymob أكّد إن الخصم تم ⇒ الفاتورة تتسوّى والاشتراك يتمد
        self.c.post('/payment/paymob/callback/', data=signed_callback(8200, int(inv.total * 100)),
                    content_type='application/json', HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(self.end_date(), old_end + timedelta(days=90))

    def test_upcoming_charge_email_replaces_the_renew_reminder(self):
        from django.core import mail
        from clients.tasks import _send_subscription_warning
        from django.core.cache import cache
        self.turn_on()
        cache.delete(f"dunning_sent:{self.tenant.schema_name}:3:{timezone.now().date()}")
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(email='owner@shop.test')
        self.tenant.refresh_from_db()
        with override_settings(EMAIL_HOST='smtp.test',
                               EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            _send_subscription_warning(self.tenant, days_left=3, is_trial=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('تلقائياً', mail.outbox[0].subject)
        self.assertIn('4242', mail.outbox[0].body)
        self.assertIn(str(self.plan.price_for_period(3)), mail.outbox[0].body)
