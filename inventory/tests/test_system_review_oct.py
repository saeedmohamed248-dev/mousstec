"""🧪 مراجعة أكتوبر الشاملة — تصليحات اتلقت وأنا بجرّب الشاشات بنفسي.

* «خصم على المتبقّي» في نافذة التحصيل على فاتورة عليها ضريبة: كان بيتضاف لخصم
  الفاتورة قبل الضريبة، فـ «خصم ١٣» بيشيل 14.82، والتحصيل اللي بعده بيتقصّ على
  المتبقّي الجديد — الكاشير ياخد الفلوس كاملة والخزنة تتسجّل أقل منها.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client, override_settings

from inventory.models import FinancialTransaction, SaleInvoice, ServiceCatalog
from inventory.services.inventory_service import set_branch_stock
from inventory.services.books_audit import books_problems
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_product, make_treasury

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class DiscountOnRemainingWithTaxTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch('الفرع الرئيسي')
        self.cash = make_treasury(self.branch, name='كاش', balance='0.00')
        self.customer = make_customer('أحمد علي', phone='01011112222')
        self.oil = make_product(part_number='REV-OIL', name='فلتر زيت', retail_price='250.00',
                                purchase_price='0', average_cost='0')
        with transaction.atomic():
            set_branch_stock(self.oil, self.branch, 10, unit_cost=D('120'))
        self.owner = User.objects.create_user('rev_owner', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.owner)

    def post_json(self, url, payload):
        return self.c.post(url, data=json.dumps(payload), content_type='application/json', HTTP_HOST=self.host)

    def _delivered_job(self):
        """أمر شغل: قطعة 250 + مصنعية 200 + ضريبة ١٤٪ = 513."""
        r = self.post_json('/system/job-card/save/', {
            'branch_id': self.branch.pk, 'customer_id': self.customer.pk, 'invoice_type': 'maintenance',
            'labor_cost_manual': '200', 'tax_percentage': '14',
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 250}], 'services': [],
        })
        self.assertEqual(r.status_code, 200, r.content)
        job = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertEqual(job.total_amount, D('513.00'))
        self.c.post(f'/system/invoices/{job.pk}/deliver/', HTTP_HOST=self.host)
        job.refresh_from_db()
        self.assertEqual(job.status, 'posted')
        return job

    def _cash_in(self, job):
        return sum((t.amount for t in FinancialTransaction.objects.filter(
            sale_invoice=job, transaction_type='in')), D(0))

    def test_discount_cuts_the_remaining_by_exactly_the_typed_amount(self):
        job = self._delivered_job()
        r = self.post_json(f'/system/invoices/{job.pk}/pay/',
                           {'treasury_id': self.cash.pk, 'amount': '400', 'discount': '13'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()['total'], r.json()['paid'], r.json()['due']), (500.0, 400.0, 100.0))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('100.00'))
        self.assertEqual(self._cash_in(job), D('400.00'))
        self.assertEqual(books_problems(), [])

    def test_cash_taken_is_the_cash_recorded(self):
        """«حصّل المتبقّي − ١٠ وخصم ١٠» = الدرج فيه المبلغ المكتوب بالظبط."""
        job = self._delivered_job()
        r = self.post_json(f'/system/invoices/{job.pk}/pay/',
                           {'treasury_id': self.cash.pk, 'amount': '503', 'discount': '10'})
        self.assertEqual(r.status_code, 200, r.content)
        job.refresh_from_db()
        self.assertEqual(job.open_due, D('0.00'))
        self.assertEqual(self._cash_in(job), D('503.00'))
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('503.00'))
        self.assertEqual(books_problems(), [])

    def test_full_remaining_discount_closes_the_invoice_without_going_negative(self):
        job = self._delivered_job()
        r = self.post_json(f'/system/invoices/{job.pk}/pay/', {'discount': '513'})
        self.assertEqual(r.status_code, 200, r.content)
        job.refresh_from_db()
        self.assertEqual(job.total_amount, D('0.00'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('0.00'))
        self.assertEqual(books_problems(), [])

    def test_invoice_without_tax_is_unchanged(self):
        ServiceCatalog.objects.create(name='فحص', labor_price=D('0'))
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.branch.pk, 'customer_id': self.customer.pk,
            'items': [{'product_id': self.oil.pk, 'qty': 2, 'price': 250}], 'payments': []})
        inv = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        r = self.post_json(f'/system/invoices/{inv.pk}/pay/',
                           {'treasury_id': self.cash.pk, 'amount': '450', 'discount': '50'})
        self.assertEqual((r.json()['total'], r.json()['paid'], r.json()['due']), (450.0, 450.0, 0.0))
        self.assertEqual(books_problems(), [])
