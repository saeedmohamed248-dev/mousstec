"""
📈 Profit reports net of returns at the real sale cost · 🚚 branch transfers.

Profit reports (found in the review):
* product profitability costed every sale at TODAY's average cost and
  counted returns as sales; the sales summary added returns as revenue; the
  admin dashboard and the products report left returns out (profit never went
  down) and the products report billed non-billable lines; the customer
  statement's profit ignored returns.

Transfers:
* stock moved only on pending → in_transit and landed in the destination
  right then; a transfer created in_transit / moved straight to completed
  moved nothing; editing a shipped transfer's quantity made cancel return a
  different amount; no stock movements were written.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import Client, override_settings

from inventory.models import Inventory, InventoryMovement, SaleInvoice, StockTransfer
from inventory.services.inventory_service import set_branch_stock
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_treasury, make_vendor
from .test_purchase_lifecycle_audit import books_problems

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class ProfitReportsTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='100000.00')
        self.vendor = make_vendor(name='مورد')
        self.part = make_product(part_number='PR-1', name='فلتر', retail_price='250',
                                 purchase_price='0', average_cost='0')
        self.c = Client()
        self.c.force_login(User.objects.create_user('pr_boss', password='x', is_staff=True, is_superuser=True))
        self.buy(10, 100)
        r = self.c.post('/system/lightning-pos/checkout/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_name': 'عميل', 'customer_phone': '01011112222',
            'payments': [{'treasury_id': self.cash.pk, 'amount': 500}],
            'items': [{'product_id': self.part.pk, 'qty': 2, 'price': 250, 'discount': 0}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        from inventory.services.invoice_service import InvoiceService
        sale = SaleInvoice.objects.get(is_return=False)
        ret = InvoiceService.create_return_invoice(
            sale, return_items=[{'item_id': sale.items.get().pk, 'quantity': 1}])
        ret.status = 'posted'
        ret.save()
        self.buy(10, 200)              # متوسط التكلفة النهارده اتغيّر (150) — البيع كان بـ 100

    def buy(self, qty, cost):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk, 'treasury_id': None, 'paid_amount': 0,
                'items': [{'product_id': self.part.pk, 'qty': qty, 'cost': cost}], 'extra_costs': []}
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)

    def test_product_profitability_uses_sale_cost_and_subtracts_returns(self):
        r = self.c.get('/system/api/v1/reports/product-profitability/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content[:300])
        row = r.json()['top_products'][0]
        # بيع 2 × 250 − مرتجع 1 ⇒ صافي 1 قطعة: إيراد 250، تكلفة 100 (وقت البيع)، ربح 150
        self.assertEqual((row['qty_sold'], row['revenue'], row['cost'], row['profit']), (1, 250.0, 100.0, 150.0))

    def test_sales_summary_subtracts_returns(self):
        r = self.c.get('/system/api/v1/reports/export/request/?type=sales_summary', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content[:300])
        data = r.json()['data']
        self.assertEqual((data['invoice_count'], data['total_revenue'], data['total_cost'], data['total_profit']),
                         (1, 250.0, 100.0, 150.0))

    def test_products_report_and_dashboard_numbers_are_net(self):
        r = self.c.get('/system/reports/products/?period=all', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.context['tot_rev'], r.context['tot_cost'], r.context['tot_profit']),
                         (D('250.00'), D('100.00'), D('150.00')))
        from inventory.services.reporting_service import ReportingService
        s = ReportingService.net_summary(SaleInvoice.objects.exclude(status='quotation'))
        self.assertEqual((s['revenue'], s['profit'], s['count']), (D('250.00'), D('150.00'), 1))


class TransferTests(ERPTenantTestCase):
    def setUp(self):
        from unittest import mock
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):
            self.a = make_branch(name='فرع أ')
            self.b = make_branch(name='فرع ب')
        self.part = make_product(part_number='TR-1', name='كويل', average_cost='0', purchase_price='0')
        with transaction.atomic():
            set_branch_stock(self.part, self.a, 20, unit_cost=D('100'))

    def qty(self, branch):
        return Inventory.objects.filter(product=self.part, branch=branch).values_list('quantity', flat=True).first() or 0

    def transfer(self, **kw):
        return StockTransfer.objects.create(product=self.part, from_branch=self.a, to_branch=self.b,
                                            quantity=kw.pop('quantity', 8), **kw)

    def test_ship_then_receive_with_movements_and_books(self):
        t = self.transfer()
        t.status = 'in_transit'
        t.save()
        self.assertEqual((self.qty(self.a), self.qty(self.b)), (12, 0))      # في الطريق
        self.assertEqual(books_problems({}), [])                              # البضاعة في الطريق لسه أصل
        t.status = 'completed'
        t.save()
        self.assertEqual((self.qty(self.a), self.qty(self.b)), (12, 8))
        self.assertEqual(sorted(InventoryMovement.objects.filter(reference_type='StockTransfer')
                                .values_list('reason', 'quantity_change')),
                         [('transfer_in', 8), ('transfer_out', -8)])
        self.assertEqual(books_problems({}), [])

    def test_created_already_moving_or_received(self):
        self.transfer(status='in_transit', quantity=3)
        self.assertEqual((self.qty(self.a), self.qty(self.b)), (17, 0))
        self.transfer(status='completed', quantity=5)
        self.assertEqual((self.qty(self.a), self.qty(self.b)), (12, 5))
        self.assertEqual(books_problems({}), [])

    def test_cancel_returns_what_left_and_shipped_transfer_is_locked(self):
        t = self.transfer()
        t.status = 'in_transit'
        t.save()
        t.quantity = 2
        with self.assertRaises(ValidationError):
            t.save()
        t.refresh_from_db()
        t.status = 'cancelled'
        t.save()
        self.assertEqual((self.qty(self.a), self.qty(self.b)), (20, 0))
        t.status = 'completed'
        with self.assertRaises(ValidationError):
            t.full_clean()
        with self.assertRaises(ValidationError):
            t.save()
        self.assertEqual(books_problems({}), [])

    def test_admin_form_errors_instead_of_crashing(self):
        t = StockTransfer(product=self.part, from_branch=self.a, to_branch=self.b, quantity=50, status='in_transit')
        with self.assertRaises(ValidationError):
            t.full_clean()                                                     # المصدر فيه 20 بس
        same = StockTransfer(product=self.part, from_branch=self.a, to_branch=self.a, quantity=1)
        with self.assertRaises(ValidationError):
            same.full_clean()
