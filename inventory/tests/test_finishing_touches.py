"""
🧰 Finishing touches after the final review.

* Old multi-line returns (made while partial returns still returned every
  line) can be reviewed and a wrongly-returned line corrected in one click.
* Duplicate seeded expense categories are merged without losing expenses.
* PDF export works without weasyprint (real PDF via reportlab).
* A dead Redis no longer hangs every sale: the marketplace sync dispatch
  backs off after the first failure.
"""
import importlib
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import (
    Customer, ExpenseCategory, FinancialTransaction, Inventory, SaleInvoice,
)
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_sale_invoice, make_treasury,
)


@override_settings(ALLOWED_HOSTS=['*'])
class FinishingTouchesTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='5000.00')
        self.customer = make_customer(name='عميل مراجعة', phone='01077770000')
        self.pads = make_product(part_number='FT-PAD', retail_price='1400.00', average_cost='900.00')
        self.filt = make_product(part_number='FT-FLT', retail_price='250.00', average_cost='150.00')
        make_inventory(self.pads, self.branch, quantity=10)
        make_inventory(self.filt, self.branch, quantity=10)
        self.boss = User.objects.create_user('ft_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)

    def _stock(self, p):
        return Inventory.objects.get(product=p, branch=self.branch).quantity

    def _old_style_return(self):
        """مرتجع رجّع الصنفين (زي اللي كان بيحصل قبل التصليح) على فاتورة آجل."""
        from inventory.services.invoice_service import InvoiceService
        inv = make_sale_invoice(self.customer, self.branch,
                                items=[(self.pads, 1, '1400.00'), (self.filt, 1, '250.00')])
        inv.status = 'posted'
        inv.save()
        ret = InvoiceService.create_return_invoice(inv)
        ret.status = 'posted'
        ret.save()
        return inv, ret

    def test_returns_audit_lists_and_corrects_a_line_once(self):
        inv, ret = self._old_style_return()
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).balance, Decimal('0.00'))
        self.assertEqual(self._stock(self.filt), 10)
        page = self.c.get('/system/reports/returns-audit/', HTTP_HOST=self.host)
        self.assertEqual(page.status_code, 200)
        self.assertEqual([r['ret'].pk for r in page.context['rows']], [ret.pk])

        flt_line = ret.items.get(product=self.filt)
        url = f'/system/reports/returns-audit/{ret.pk}/{flt_line.pk}/correct/'
        r = self.c.post(url, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.assertIn('corrected=', r['Location'])
        fix = SaleInvoice.objects.get(notes__startswith=f'تصحيح مرتجع #{ret.pk}')
        self.assertEqual(fix.status, 'posted')
        self.assertEqual(fix.total_amount, Decimal('250.00'))
        self.assertEqual(self._stock(self.filt), 9)                       # الفلتر خرج تاني
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).balance, Decimal('250.00'))

        r = self.c.post(url, HTTP_HOST=self.host)                          # مرة واحدة بس
        self.assertIn('err=done', r['Location'])
        self.assertEqual(self._stock(self.filt), 9)
        page = self.c.get('/system/reports/returns-audit/', HTTP_HOST=self.host)
        corrected = {l['line'].pk: l['corrected'] for l in page.context['rows'][0]['lines']}
        self.assertTrue(corrected[flt_line.pk])

    def test_correction_can_put_cash_back_in_the_till(self):
        _inv, ret = self._old_style_return()
        line = ret.items.get(product=self.filt)
        before = self.till.__class__.objects.get(pk=self.till.pk).balance
        self.c.post(f'/system/reports/returns-audit/{ret.pk}/{line.pk}/correct/',
                    {'treasury_id': self.till.pk}, HTTP_HOST=self.host)
        self.assertEqual(self.till.__class__.objects.get(pk=self.till.pk).balance, before + Decimal('250.00'))
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).balance, Decimal('0.00'))

    def test_duplicate_expense_categories_are_merged(self):
        mig = importlib.import_module('inventory.migrations.0058_merge_duplicate_expense_categories')
        long_cat, _ = ExpenseCategory.objects.get_or_create(name='إيجار المحل / الورشة')
        short_cat, _ = ExpenseCategory.objects.get_or_create(name='إيجار المحل')
        ft = FinancialTransaction.objects.create(treasury=self.till, transaction_type='out',
                                                 amount=Decimal('10'), description='إيجار', category=long_cat)
        mig.merge(django_apps, None)
        self.assertFalse(ExpenseCategory.objects.filter(name='إيجار المحل / الورشة').exists())
        ft.refresh_from_db()
        self.assertEqual(ft.category_id, short_cat.pk)

    def test_pdf_export_is_a_real_pdf_without_weasyprint(self):
        import builtins
        real_import = builtins.__import__

        def no_weasy(name, *a, **kw):
            if name.startswith('weasyprint'):
                raise ImportError('no weasyprint')
            return real_import(name, *a, **kw)
        with mock.patch('builtins.__import__', side_effect=no_weasy):
            r = self.c.get('/system/products/export/?fmt=pdf&all=1', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_dead_broker_is_tried_once_not_per_item(self):
        from inventory.services import inventory_service as svc
        svc._BROKER_DOWN_UNTIL = 0.0
        inv_row = Inventory.objects.get(product=self.filt, branch=self.branch)
        with mock.patch.object(svc.current_app, 'send_task', side_effect=OSError('redis down')) as send, \
                mock.patch.object(svc.transaction, 'on_commit', side_effect=lambda fn: fn()):
            for _ in range(5):
                svc.InventoryService.dispatch_b2b_sync(inv_row)
        self.assertEqual(send.call_count, 1)
        svc._BROKER_DOWN_UNTIL = 0.0
