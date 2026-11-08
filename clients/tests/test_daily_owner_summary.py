"""📲 ملخص اليوم للمالك على واتساب: الأرقام صح، مابيتبعتش لو مفيش شغل، والمالك يقدر يقفله."""
from decimal import Decimal
from unittest import mock

from django.db import connection

from clients.services.daily_owner_summary import build_summary
from clients.tasks import send_daily_owner_summaries
from inventory.models import Customer, FinancialTransaction, SaleInvoice
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_customer, make_treasury


class DailyOwnerSummaryTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            industry='automotive', status='active', phone='01011112222', daily_summary_whatsapp=True)
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        SaleInvoice.objects.all().delete()
        FinancialTransaction.objects.all().delete()
        Customer.objects.all().delete()

    def _sale(self):
        branch = make_branch()
        till = make_treasury(branch, balance='1000.00')
        cust = make_customer('شركة الوادي', phone='01077776666')
        SaleInvoice.objects.create(customer=cust, branch=branch, invoice_type='sale', status='posted',
                                   total_amount=Decimal('750.00'))
        Customer.objects.filter(pk=cust.pk).update(balance=Decimal('250.00'))
        return till

    def test_no_activity_means_no_message(self):
        self.assertIsNone(build_summary(self.tenant))

    def test_summary_numbers(self):
        self._sale()
        d = build_summary(self.tenant)
        self.assertEqual((d['count'], d['sales'], d['debt'], d['debtors']),
                         (1, Decimal('750.00'), Decimal('250.00'), 1))
        self.assertIn('750', d['text'])
        self.assertIn('الخزنة دلوقتي: 1,000', d['text'])

    def test_task_sends_to_owner_and_respects_opt_out(self):
        self._sale()
        connection.set_schema_to_public()
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as send:
            send_daily_owner_summaries()
        calls = [c for c in send.call_args_list if c.args[0] == '01011112222']
        self.assertEqual(len(calls), 1)
        self.assertIn('ملخص النهارده', calls[0].kwargs['text'])
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(daily_summary_whatsapp=False)
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as send:
            send_daily_owner_summaries()
        self.assertFalse([c for c in send.call_args_list if c.args[0] == '01011112222'])
        connection.set_tenant(self.tenant)
