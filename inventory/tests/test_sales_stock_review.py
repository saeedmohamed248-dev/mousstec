"""
🔍 Sales paths that touch stock (found in the inventory review).

* A website (Wix) order was created «posted» before its lines, so posting ran
  on an empty invoice: the ledger booked the sale but stock never went down.
* «Add to Job Card» from the AI suggestions added lines to a job card whose
  stock was already taken — booked in the ledger, never out of the store.
* POS / job card with the same part on two lines read the stock once per line
  and each wrote «before − qty», so the second line undid the first.
"""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Inventory, InventoryMovement, SaleInvoice
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_treasury, make_vendor
from .test_purchase_lifecycle_audit import books_problems

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class SalesStockTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='100000.00')
        self.opening = {self.cash.pk: D('100000.00')}
        self.part = make_product(part_number='SS-1', name='فلتر', retail_price='250',
                                 purchase_price='0', average_cost='0')
        self.c = Client()
        self.c.force_login(User.objects.create_user('ss_boss', password='x', is_staff=True, is_superuser=True))
        body = {'vendor_id': make_vendor(name='مورد').pk, 'branch_id': self.branch.pk, 'treasury_id': None,
                'paid_amount': 0, 'items': [{'product_id': self.part.pk, 'qty': 10, 'cost': 100}],
                'extra_costs': []}
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)

    def stock(self):
        return Inventory.objects.get(product=self.part, branch=self.branch).quantity

    def ok(self, step):
        self.assertEqual(books_problems(self.opening), [], f"after: {step}")

    def pos(self, lines):
        return self.c.post('/system/lightning-pos/checkout/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_name': 'عميل', 'customer_phone': '01011112222',
            'payments': [{'treasury_id': self.cash.pk, 'amount': 250 * sum(lines)}],
            'items': [{'product_id': self.part.pk, 'qty': q, 'price': 250, 'discount': 0} for q in lines]}),
            content_type='application/json', HTTP_HOST=self.host)

    def test_pos_same_part_on_two_lines(self):
        r = self.pos([3, 3])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.stock(), 4)                       # مش 7
        self.ok('two lines of the same part')
        r = self.pos([3, 3])                                    # الموجود 4 بس
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.stock(), 4)

    def test_suggested_part_cannot_be_added_to_a_saved_job_card(self):
        self.assertEqual(self.pos([1]).status_code, 200)
        inv = SaleInvoice.objects.latest('id')
        self.assertTrue(inv.is_applied)
        token = 'A' * 32                               # شكل توكن CSRF صالح
        self.c.cookies['mt_csrf'] = token
        r = self.c.post(f'/system/invoice/{inv.pk}/suggested-parts/add/',
                        json.dumps({'product_id': self.part.pk, 'quantity': 2}),
                        content_type='application/json', HTTP_X_CSRFTOKEN=token, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual((inv.items.count(), self.stock()), (1, 9))
        self.ok('suggested part refused')

    def _pull(self, qty):
        from clients.services import wix_sync
        conn = SimpleNamespace(client_id=self.tenant.pk, last_order_sync_at=None, default_branch_id=self.branch.pk,
                               orders_imported=0, save=lambda **kw: None, mark_error=lambda e: None)
        order = {'id': f'W-{qty}', 'number': 1001, 'buyerInfo': {'phone': '01022223333'},
                 'lineItems': [{'sku': 'SS-1', 'quantity': qty, 'price': {'amount': '250'}}]}
        fake = mock.Mock()
        fake.search_orders.return_value = SimpleNamespace(ok=True, data={'orders': [order]}, error='')
        with mock.patch.object(wix_sync, '_client', return_value=fake):
            return wix_sync.pull_orders(conn)

    def test_website_order_takes_stock_out(self):
        summary = self._pull(2)
        self.assertEqual(summary.get('imported'), 1, summary)
        inv = SaleInvoice.objects.get(wix_order_id='W-2')
        self.assertEqual((inv.status, inv.is_applied, self.stock()), ('posted', True, 8))
        self.assertTrue(InventoryMovement.objects.filter(reference_type='SaleInvoice', reference_id=inv.pk).exists())
        self.ok('wix order')
        self._pull(50)                                          # أكتر من الموجود
        big = SaleInvoice.objects.get(wix_order_id='W-50')
        self.assertEqual((big.status, big.is_applied, self.stock()), ('quotation', False, 8))
        self.assertIn('مااتعتمدتش', big.notes)
        self.ok('wix order without stock')
