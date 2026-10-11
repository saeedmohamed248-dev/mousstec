"""
↩️ Returns pages review — customer returns (sale) + vendor returns (purchase).

* The return page shows the invoice number (blocktrans with a dotted var
  rendered it empty: «مرتجع فاتورة #»).
* The cash refund leaves the treasury the user picks, never more than it
  holds; on any error nothing is written and the form keeps what was typed.
* Pressing «تنفيذ» twice can't return the same quantity twice.
* The invoices list shows a return as a minus amount linked to its invoice,
  not as money the customer still owes.
* A printed return says «إشعار مرتجع», not «فاتورة».
* Thermal printing is branch-scoped like A4.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import FinancialTransaction, Inventory, SaleInvoice, Treasury
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_employee, make_inventory, make_product,
    make_purchase_invoice, make_sale_invoice, make_treasury, make_vendor,
)


@override_settings(ALLOWED_HOSTS=['*'])
class SaleReturnPageTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, name='درج الكاشير', balance='5000.00')
        self.customer = make_customer()
        self.product = make_product(part_number='RR-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=10)
        # 2 × 100 بخصم فاتورة 20 وضريبة 14% = 205.20 مدفوعة كاش
        self.inv = make_sale_invoice(self.customer, self.branch, treasury=self.till,
                                     items=[(self.product, 2, '100.00')])
        SaleInvoice.objects.filter(pk=self.inv.pk).update(discount=Decimal('20'), tax_percentage=Decimal('14'))
        self.inv.refresh_from_db()
        self.inv.update_total()
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.total_amount, Decimal('205.20'))
        SaleInvoice.objects.filter(pk=self.inv.pk).update(paid_amount=self.inv.total_amount)
        self.inv.refresh_from_db()
        self.inv.status = 'posted'
        self.inv.save()
        self.item = self.inv.items.get()
        self.boss = User.objects.create_user('rr_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)
        self.url = f'/system/invoices/{self.inv.pk}/return/'

    def _stock(self):
        return Inventory.objects.get(product=self.product, branch=self.branch).quantity

    def test_page_shows_number_and_refund_estimate(self):
        r = self.c.get(self.url, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'مرتجع فاتورة #{self.inv.pk}')
        # 100 × (1 − 20/200) × 1.14 = 102.60 للقطعة
        self.assertEqual(r.context['rows'][0]['unit_refund'], Decimal('102.60'))
        self.assertEqual(r.context['refundable_cash'], Decimal('205.20'))

    def test_refund_from_chosen_treasury_and_flash(self):
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):  # حد الخزائن في الباقة
            other = make_treasury(self.branch, name='خزنة المحل', balance='1000.00')
        before_till = Treasury.objects.get(pk=self.till.pk).balance
        r = self.c.post(self.url, {f'qty_{self.item.pk}': '1', 'treasury_id': other.pk,
                                   'note': 'مش مناسبة'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context.get('error'))
        self.assertIn('refund=102.60', r['Location'])
        ret = SaleInvoice.objects.get(original_invoice=self.inv)
        self.assertEqual(ret.treasury_id, other.pk)
        self.assertIn('مش مناسبة', ret.notes)
        self.assertEqual(Treasury.objects.get(pk=other.pk).balance, Decimal('897.40'))
        self.assertEqual(Treasury.objects.get(pk=self.till.pk).balance, before_till)
        self.assertEqual(self._stock(), 9)

        # القائمة: المرتجع بالسالب ومربوط بالفاتورة، مش «متبقي» على العميل
        page = self.c.get(r['Location'], HTTP_HOST=self.host)
        html = page.content.decode()
        self.assertRegex(html, r'اترد للعميل نقداً 102[.,]60')
        self.assertIn(f'عن #{self.inv.pk}', html)
        self.assertRegex(html, r'−102[.,]60')

        # الطباعة: إشعار مرتجع مش فاتورة
        a4 = self.c.get(f'/system/invoice/{ret.pk}/print/a4/', HTTP_HOST=self.host)
        self.assertContains(a4, 'إشعار مرتجع رقم')
        self.assertContains(a4, 'اترد للعميل نقداً')
        th = self.c.get(f'/system/invoice/{ret.pk}/print/thermal/', HTTP_HOST=self.host)
        self.assertContains(th, 'إشعار مرتجع')

    def test_short_treasury_blocks_and_writes_nothing(self):
        Treasury.objects.filter(pk=self.till.pk).update(balance=Decimal('50'))
        r = self.c.post(self.url, {f'qty_{self.item.pk}': '1', 'treasury_id': self.till.pk},
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertIn('مش كفاية', r.context['error'])
        self.assertEqual(r.context['rows'][0]['entered'], '1')        # اللي اتكتب فضل
        self.assertFalse(SaleInvoice.objects.filter(original_invoice=self.inv).exists())
        self.assertEqual(self._stock(), 8)
        self.assertEqual(Treasury.objects.get(pk=self.till.pk).balance, Decimal('50'))

    def test_second_press_cannot_return_twice(self):
        self.assertEqual(self.c.post(self.url, {'return_all': '1'}, HTTP_HOST=self.host).status_code, 302)
        r = self.c.post(self.url, {'return_all': '1'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertIn('مُرتجعة بالكامل', r.context['error'])
        self.assertEqual(SaleInvoice.objects.filter(original_invoice=self.inv).count(), 1)
        self.assertEqual(self._stock(), 10)
        self.assertEqual(FinancialTransaction.objects.filter(
            sale_invoice__original_invoice=self.inv, transaction_type='out').count(), 1)

    def test_credit_sale_return_has_no_cash_and_needs_no_treasury(self):
        credit = make_sale_invoice(self.customer, self.branch, items=[(self.product, 1, '100.00')])
        credit.status = 'posted'
        credit.save()
        it = credit.items.get()
        r = self.c.post(f'/system/invoices/{credit.pk}/return/', {f'qty_{it.pk}': '1'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.assertIn('refund=0.00', r['Location'])
        self.assertIn('credit=100.00', r['Location'])

    def test_thermal_print_is_branch_scoped(self):
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):
            other = make_branch(name='فرع تاني')
        clerk, _p = make_employee(username='rr_clerk', role='cashier', branch=other)
        c = Client()
        c.force_login(clerk)
        r = c.get(f'/system/invoice/{self.inv.pk}/print/thermal/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)


@override_settings(ALLOWED_HOSTS=['*'])
class PurchaseReturnPageTests(ERPTenantTestCase):
    def test_error_keeps_what_was_typed(self):
        connection.set_tenant(self.tenant)
        host = self.domain.domain
        branch = make_branch()
        product = make_product(part_number='PR-1', purchase_price='50.00', average_cost='50.00')
        inv = make_purchase_invoice(make_vendor(), branch, items=[(product, 3, '50.00')], status='draft')
        inv.status = 'posted'
        inv.save()
        item = inv.items.get()
        boss = User.objects.create_user('pr_boss', password='x', is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(boss)
        # استرداد أكبر من المسموح (مفيش مدفوع) → خطأ، والكمية والسبب يفضلوا
        r = c.post(f'/system/purchases/{inv.pk}/return/',
                   {f'qty_{item.pk}': '2', 'refund_amount': '999', 'note': 'معيبة'}, HTTP_HOST=host)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['error'])
        self.assertEqual(r.context['items'][0]['entered'], '2')
        self.assertContains(r, 'value="معيبة"')
