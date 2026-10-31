"""
🎛️ Platform owner panel — complete billing cycles and owner controls.

* Manual activation / renewal / plan change go through a PlatformInvoice
  (paid or gift) like a Paymob payment; invoices can be issued, collected,
  voided and refunded from the tenant 360 control panel.
* «Activate» no longer means free forever; a rejected receipt voids its
  invoice; add-ons are charged from the wallet; the Paymob amount is computed
  on the server and checked on the callback.
* Staff roles only reach their widgets; support can reply to the customer.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone

from clients.models import (
    Client, Domain, ManualPaymentReceipt, Plan, PlatformInvoice, StaffRole, SupportTicket,
)
from clients.services.platform_billing import issue_subscription_invoice
from .base import ERPTenantTestCase

PLATFORM_HOST = 'platform.test.com'


@override_settings(ALLOWED_HOSTS=['*'], EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PlatformAdminTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_schema_to_public()
        pub = Client.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save()
        Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
        self.owner, _ = User.objects.get_or_create(username='pa_owner@test.com',
                                                   defaults={'is_staff': True, 'is_superuser': True})
        self.c = HttpClient()
        self.c.force_login(self.owner)
        self.plan, _ = Plan.objects.get_or_create(
            slug='silver', defaults={'name': 'Silver', 'industry': 'automotive',
                                     'monthly_price': Decimal('550.00'), 'is_active': True})
        self.tenant.refresh_from_db()

    def tearDown(self):
        # ⚠️ TransactionTestCase بيفضّي جداول السكيمة الحالية بعد كل اختبار —
        #    لازم نرجع لسكيمة الشركة، وإلا بيمسح جداول المنصة (الشركات/الباقات).
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _ctl(self, **data):
        r = self.c.post(f'/superadmin/tenant/{self.tenant.pk}/control/', data, HTTP_HOST=PLATFORM_HOST)
        return r, r.json()

    def test_control_panel_billing_cycle(self):
        before = self.tenant.subscription_end_date
        r, body = self._ctl(action='change_plan', plan=self.plan.slug, months='1', payment_mode='gift')
        self.assertTrue(body['ok'], body)
        gift = PlatformInvoice.objects.filter(tenant=self.tenant).order_by('-pk').first()
        self.assertEqual((gift.status, gift.total, gift.payment_provider), ('paid', Decimal('0.00'), 'gift'))
        self.tenant.refresh_from_db()
        self.assertGreater(self.tenant.subscription_end_date, before or timezone.localdate())

        r, body = self._ctl(action='issue_invoice', plan=self.plan.slug, months='3')
        inv = PlatformInvoice.objects.filter(tenant=self.tenant, status='issued').get()
        # الباقة + الإضافات المشتراة (موظف/فرع/خزينة) بتتجدد معاها
        from clients.services.platform_billing import renewal_amount
        self.tenant.refresh_from_db()
        self.assertEqual(inv.total, renewal_amount(self.tenant, self.plan, 3))
        r, body = self._ctl(action='mark_paid', invoice=inv.invoice_number, reference='TXN-1')
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.payment_reference), ('paid', 'TXN-1'))
        r, body = self._ctl(action='refund_invoice', invoice=inv.invoice_number, cut_service='1', reason='x')
        inv.refresh_from_db()
        self.tenant.refresh_from_db()
        self.assertEqual(inv.status, 'refunded')
        self.assertEqual(self.tenant.subscription_end_date, timezone.localdate())

        issue_subscription_invoice(self.tenant, self.plan, 1, paid=False)
        open_inv = PlatformInvoice.objects.get(tenant=self.tenant, status='issued')
        self._ctl(action='void_invoice', invoice=open_inv.invoice_number, reason='غلط')
        open_inv.refresh_from_db()
        self.assertEqual(open_inv.status, 'void')

    def test_control_limits_wallet_and_owner_link(self):
        r, body = self._ctl(action='set_limits', extra_users_purchased='3')
        self.assertTrue(body['ok'])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.extra_users_purchased, 3)
        start = Decimal(str(self.tenant.wallet_balance))
        self._ctl(action='wallet_adjust', direction='credit', amount='500', reason='تعويض')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.wallet_balance, start + Decimal('500'))
        r, body = self._ctl(action='wallet_adjust', direction='debit', amount='999999')
        self.assertFalse(body['ok'])
        connection.set_tenant(self.tenant)
        User.objects.get_or_create(username='shop@x.com', defaults={'email': 'shop@x.com', 'is_superuser': True})
        connection.set_schema_to_public()
        r, body = self._ctl(action='owner_reset_link')
        self.assertTrue(body['ok'])
        self.assertIn('/account/set-password/?token=', body['link'])

    def test_activate_is_not_free_forever_and_trial_extends_from_today(self):
        Client.all_objects.filter(pk=self.tenant.pk).update(
            status='suspended', is_active=False, subscription_end_date=None,
            trial_ends_at=timezone.localdate() - timedelta(days=30))
        self.c.post('/superadmin/', {'action': 'activate', 'tenant_id': self.tenant.pk}, HTTP_HOST=PLATFORM_HOST)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, 'active')
        from clients.models.tenancy import TRIAL_DAYS
        self.assertEqual(self.tenant.subscription_end_date, timezone.localdate() + timedelta(days=TRIAL_DAYS))
        self.c.post('/superadmin/', {'action': 'extend_trial', 'tenant_id': self.tenant.pk}, HTTP_HOST=PLATFORM_HOST)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.trial_ends_at, timezone.localdate() + timedelta(days=TRIAL_DAYS))

    def test_rejected_receipt_voids_its_invoice(self):
        inv = issue_subscription_invoice(self.tenant, self.plan, 1, paid=False)
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='subscription', purchase_id=inv.pk, amount=inv.total,
            sender_phone='010', txn_reference='T', tenant=self.tenant)
        r = self.c.post(f'/payment/manual/admin/review/{receipt.receipt_code}/', {'action': 'reject'},
                        HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'void')

    def test_staff_roles_only_reach_their_widgets(self):
        staff, _ = User.objects.get_or_create(username='pa_support@test.com', defaults={'is_staff': True})
        StaffRole.objects.get_or_create(user=staff, defaults={'role': 'support'})
        c = HttpClient()
        c.force_login(staff)
        self.assertEqual(c.get('/superadmin/plans/', HTTP_HOST=PLATFORM_HOST).status_code, 403)
        self.assertEqual(c.get('/superadmin/parts/payouts/', HTTP_HOST=PLATFORM_HOST).status_code, 403)
        self.assertEqual(c.get('/superadmin/support/', HTTP_HOST=PLATFORM_HOST).status_code, 200)
        r = c.post(f'/superadmin/tenant/{self.tenant.pk}/control/', {'action': 'note', 'note': 'x'},
                   HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 403)

    def test_support_can_reply_to_the_customer(self):
        t = SupportTicket.objects.create(name='عميل', email='cust@example.com', subject='مشكلة', message='النظام بطيء')
        mail.outbox.clear()
        self.c.post(f'/superadmin/support/{t.pk}/', {'action': 'reply', 'reply': 'اتصلحت ✅'}, HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['cust@example.com'])
        t.refresh_from_db()
        self.assertEqual(t.status, 'waiting')
        # الرد بقى في محادثة التذكرة (العميل بيشوفه في «تذاكري») — مش في الملاحظات الداخلية
        self.assertEqual(t.replies.get().body, 'اتصلحت ✅')
        self.assertTrue(t.customer_unread)
        self.assertNotIn('اتصلحت', t.admin_notes)

    def test_paymob_callback_rejects_an_underpaid_order(self):
        cache.set('paymob_order_9911', {'plan': self.plan.slug, 'shop': self.tenant.schema_name,
                                        'amount': '550.00', 'billing_period': 'monthly'}, 300)
        with mock.patch('clients.services.paymob.verify_paymob_hmac', return_value=(True, '')):
            r = self.c.get('/payment/paymob/callback/',
                           {'success': 'true', 'order': '9911', 'amount_cents': '100'}, HTTP_HOST=PLATFORM_HOST)
        self.assertEqual(r.status_code, 302)
        self.assertIn('amount_mismatch', r['Location'])
        self.assertFalse(PlatformInvoice.objects.filter(payment_reference='9911').exists())


@override_settings(ALLOWED_HOSTS=['*'])
class AddonChargeTests(ERPTenantTestCase):
    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_addon_is_charged_from_the_wallet(self):
        connection.set_tenant(self.tenant)
        owner, _ = User.objects.get_or_create(username='addon_owner', defaults={'is_superuser': True, 'is_staff': True})
        c = HttpClient()
        c.force_login(owner)
        url = '/api/v1/subscription/addon/'
        body = json.dumps({'addon_type': 'employee', 'quantity': 1})
        Client.all_objects.filter(pk=self.tenant.pk).update(wallet_balance=Decimal('0'))
        r = c.post(url, body, content_type='application/json', HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 402)
        connection.set_schema_to_public()
        self.assertEqual(Client.all_objects.get(pk=self.tenant.pk).extra_users_purchased, 0)
        Client.all_objects.filter(pk=self.tenant.pk).update(wallet_balance=Decimal('1000'))
        connection.set_tenant(self.tenant)
        r = c.post(url, body, content_type='application/json', HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200, r.content)
        cost = Decimal(str(r.json()['total_cost']))
        connection.set_schema_to_public()
        t = Client.all_objects.get(pk=self.tenant.pk)
        self.assertEqual(t.extra_users_purchased, 1)
        self.assertEqual(t.wallet_balance, Decimal('1000') - cost)
