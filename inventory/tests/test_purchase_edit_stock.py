"""
🩹 Editing a purchase invoice kept its goods out of stock.

Editing reverses the invoice (takes its quantities out of stock and its due
off the supplier), deletes the old lines and posts it again. Deleting the
lines re-saved the invoice through the total signal while it was still
«posted» but already reversed, so it got posted *empty* and flagged applied;
the real posting at the end was then skipped. Result: the invoice looked
posted, every item showed 0 in stock and the supplier balance dropped by the
whole bill. Deleting an invoice with a treasury-paid expense charged the
treasury again the same way.

Reversal now leaves the invoice a draft, an invoice with no lines is never
applied, and a repair (page button + management command) re-posts invoices
that were already broken.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Inventory, Product, PurchaseInvoice, Treasury, Vendor
from inventory.services.purchase_repair import broken_purchase_edits
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_purchase_invoice, make_treasury, make_vendor
from .test_landed_cost import ledger_is_balanced

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class PurchaseEditStockTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.vendor = make_vendor(name='FIXIT UAE')
        self.e60 = make_product(part_number='ED-1', name='اسكتال e60', average_cost='0', purchase_price='0')
        self.r56 = make_product(part_number='ED-2', name='اسكتال r56', average_cost='0', purchase_price='0')
        self.c = Client()
        self.c.force_login(User.objects.create_user('ed_boss', password='x', is_staff=True, is_superuser=True))

    def _save(self, **extra):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk, 'treasury_id': None, 'paid_amount': 0,
                'items': [{'product_id': self.e60.pk, 'qty': 1, 'cost': 4400},
                          {'product_id': self.r56.pk, 'qty': 5, 'cost': 5000}], 'extra_costs': []}
        body.update(extra)
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        return PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])

    def stock(self, p):
        return sum(Inventory.objects.filter(product=p).values_list('quantity', flat=True))

    def test_edit_keeps_stock_and_supplier_and_adds_the_expense(self):
        inv = self._save()
        self._save(invoice_id=inv.pk,
                   extra_costs=[{'kind': 'shipping', 'behavior': 'landed', 'amount': 1000}])
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.is_applied, inv.total_amount), ('posted', True, D('29400.00')))
        self.assertEqual((self.stock(self.e60), self.stock(self.r56)), (1, 5))
        self.e60.refresh_from_db()
        self.r56.refresh_from_db()
        self.assertEqual((self.e60.average_cost, self.r56.average_cost), (D('4549.66'), D('5170.07')))
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, D('29400.00'))
        self.assertEqual(broken_purchase_edits(), [])
        self.assertTrue(ledger_is_balanced())

    def test_delete_with_a_paid_expense_refunds_the_treasury_once(self):
        cash = make_treasury(self.branch, balance='10000')
        inv = self._save(extra_costs=[{'kind': 'shipping', 'behavior': 'landed', 'amount': 500,
                                       'treasury_id': cash.pk}])
        self.assertEqual(Treasury.objects.get(pk=cash.pk).balance, D('9500.00'))
        r = self.c.post(f'/system/purchases/{inv.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('ok=deleted', r['Location'])
        self.assertEqual(Treasury.objects.get(pk=cash.pk).balance, D('10000.00'))
        self.assertEqual((self.stock(self.e60), self.stock(self.r56)), (0, 0))
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, D('0.00'))
        self.assertTrue(ledger_is_balanced())

    def test_repair_brings_back_an_invoice_broken_by_the_old_edit(self):
        from inventory.views_lightning import _reverse_purchase_posting
        inv = self._save(extra_costs=[{'kind': 'shipping', 'behavior': 'landed', 'amount': 1000}])
        # الحالة اللي كان التعديل القديم بيسيبها: اتعكست ومتعلّمة معتمدة
        from django.db import transaction
        with transaction.atomic():
            _reverse_purchase_posting(inv)
            PurchaseInvoice.objects.filter(pk=inv.pk).update(status='posted', is_applied=True)
        self.assertEqual((self.stock(self.e60), Vendor.objects.get(pk=self.vendor.pk).balance), (0, D('0.00')))
        rows = broken_purchase_edits()
        self.assertEqual([(r['invoice'].pk, r['missing']) for r in rows], [(inv.pk, 6)])
        page = self.c.get('/system/purchases/', HTTP_HOST=self.host).content.decode()
        self.assertIn('فواتير اتعدّلت وبضاعتها مارجعتش للمخزن', page)

        r = self.c.post(f'/system/purchases/{inv.pk}/repair/', HTTP_HOST=self.host)
        self.assertIn('ok=repaired', r['Location'])
        self.assertEqual((self.stock(self.e60), self.stock(self.r56)), (1, 5))
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, D('29400.00'))
        self.assertEqual(Product.objects.get(pk=self.e60.pk).average_cost, D('4549.66'))
        self.assertEqual(broken_purchase_edits(), [])
        self.assertTrue(ledger_is_balanced())
        # مرة واحدة بس
        r = self.c.post(f'/system/purchases/{inv.pk}/repair/', HTTP_HOST=self.host)
        self.assertIn('err=', r['Location'])
        self.assertEqual(self.stock(self.r56), 5)
