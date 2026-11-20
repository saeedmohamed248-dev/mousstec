"""💸 أي تفعيل اشتراك من لوحة Django = فاتورة منصة مدفوعة ⇒ بيظهر في «حسابات المنصة».

🐛 قبل كده أزرار «✅ تفعيل اشتراك — شهر/٣/٦/١٢» و«تفعيل فوري» كانت بتمدّ التاريخ بس.
"""
import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import Client, Plan, PlatformInvoice, TenantSubscription
from clients.services import platform_finance as pf
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin


@override_settings(ALLOWED_HOSTS=['*'])
class AdminActivationCreatesInvoiceTests(_PublicDomainMixin, TestCase):
    def setUp(self):
        self.plan = Plan.objects.update_or_create(slug='auto-silver', defaults=dict(
            name='Silver', industry='automotive', monthly_price=Decimal('550'), is_active=True))[0]
        self.owner = get_user_model().objects.create_user('act_owner', password='x', is_superuser=True,
                                                          is_staff=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)

    def _shop(self, schema, plan=True):
        t = Client(name=schema, owner_name='x', phone='0100', schema_name=schema, is_active=True)
        t.auto_create_schema = False
        t.save()
        return t, TenantSubscription.objects.create(tenant=t, plan=self.plan if plan else None)

    def test_subscription_admin_action_issues_paid_invoice_visible_in_finance(self):
        t, sub = self._shop('act_a')
        r = self.c.post('/secure-portal/clients/tenantsubscription/', {
            'action': 'activate_subscription_3_months', '_selected_action': [sub.pk]})
        self.assertEqual(r.status_code, 302)
        inv = PlatformInvoice.objects.get(tenant=t)
        self.assertEqual((inv.status, inv.billing_cycle_months, inv.payment_provider), ('paid', 3, 'manual_admin'))
        self.assertGreater(inv.total, 0)
        t.refresh_from_db()
        self.assertGreaterEqual(t.subscription_end_date, datetime.date.today() + datetime.timedelta(days=80))
        today = datetime.date.today()
        self.assertEqual(pf.revenue(today, today, pf.CASH)['subscriptions'], inv.total)
        self.assertGreater(pf.kpis(today)['mrr'], 0)

    def test_quick_activate_from_companies_and_no_plan_fallback(self):
        t, _ = self._shop('act_b')
        self.c.post('/secure-portal/clients/client/', {'action': 'quick_activate_1_month', '_selected_action': [t.pk]})
        self.assertEqual(PlatformInvoice.objects.filter(tenant=t, status='paid').count(), 1)
        t2, sub2 = self._shop('act_c', plan=False)
        Client.objects.filter(pk=t2.pk).update(plan='')
        self.c.post('/secure-portal/clients/tenantsubscription/', {
            'action': 'activate_subscription_1_month', '_selected_action': [sub2.pk]})
        t2.refresh_from_db()
        self.assertFalse(PlatformInvoice.objects.filter(tenant=t2).exists())   # مفيش باقة ⇒ تمديد بس زي الأول
        self.assertEqual(t2.status, 'active')
