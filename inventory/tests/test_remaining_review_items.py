"""
🔍 The last items left over from the inventory/purchases review.

* A quotation posted after the average cost moved left the store at the cost
  from when its lines were typed (ledger ≠ stock value, wrong profit).
* A sale return / a deleted sale put the goods back at their sale cost but left
  the average cost alone.
* Maintenance-contract invoices took parts out of the store with no journal at
  all (no COGS, inventory account kept the parts).
* A branch transfer moved quantities only — each branch's trial balance kept
  (or never got) the goods.
* A vendor credit (advance / overpaid after a return) lowered the vendor's
  balance but his invoices stayed «unpaid».
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.db.models import Sum
from django.test import Client, override_settings

from inventory.models import (AccountingEntry, FinancialTransaction, Inventory, JournalEntry,
                              Product, PurchaseInvoice, SaleInvoice, SaleInvoiceItem, StockTransfer,
                              Treasury, Vendor)
from inventory.services.inventory_service import set_branch_stock
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_product, make_treasury, make_vendor
from .test_purchase_lifecycle_audit import books_problems

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class _Base(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='100000.00')
        self.opening = {self.cash.pk: D('100000.00')}
        self.vendor = make_vendor(name='مورد')
        self.customer = make_customer()
        self.part = make_product(part_number='RR-1', name='فلتر', retail_price='300',
                                 purchase_price='0', average_cost='0')
        self.c = Client()
        self.c.force_login(User.objects.create_user('rr_boss', password='x', is_staff=True, is_superuser=True))

    def ok(self, step):
        self.assertEqual(books_problems(self.opening), [], f"after: {step}")

    def buy(self, qty, cost, product=None, paid=0):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk,
                'treasury_id': self.cash.pk if paid else None, 'paid_amount': paid,
                'items': [{'product_id': (product or self.part).pk, 'qty': qty, 'cost': cost}],
                'extra_costs': []}
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        return PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])

    def avg(self):
        return Product.objects.get(pk=self.part.pk).average_cost

    def stock(self):
        return Inventory.objects.get(product=self.part, branch=self.branch).quantity

    def quotation(self, qty, price=300, **kw):
        inv = SaleInvoice.objects.create(customer=self.customer, branch=self.branch, status='quotation', **kw)
        SaleInvoiceItem.objects.create(invoice=inv, product=self.part, quantity=qty, unit_price=D(price))
        inv.refresh_from_db()
        return inv

    def post(self, inv):
        inv.refresh_from_db()
        inv.status = 'posted'
        inv.save()
        inv.refresh_from_db()
        return inv


class SaleCostTests(_Base):
    def test_quotation_posted_later_leaves_at_todays_cost(self):
        self.buy(10, 100)
        q = self.quotation(2)                          # التكلفة وقت كتابة العرض 100
        self.buy(10, 200)                              # المتوسط بقى 150
        q = self.post(q)
        self.assertEqual(q.items.get().cost_at_sale, D('150.00'))
        self.assertEqual((q.total_cost, q.net_profit), (D('300.00'), D('300.00')))
        self.ok('quotation posted after the cost moved')

    def test_return_and_delete_reaverage_the_cost(self):
        from inventory.services.invoice_service import InvoiceService
        self.buy(10, 100)
        sale = self.post(self.quotation(4))           # 4 × 100
        self.buy(6, 200)                               # 6×100 + 6×200 / 12 = 150
        self.assertEqual(self.avg(), D('150.00'))
        ret = InvoiceService.create_return_invoice(sale, return_items=[{'item_id': sale.items.get().pk,
                                                                       'quantity': 2}])
        self.post(ret)                                 # (12×150 + 2×100) / 14
        self.assertEqual((self.stock(), self.avg()), (14, D('142.86')))
        self.ok('sale return')

        sale2 = self.post(self.quotation(3))          # 3 × 142.86
        self.buy(1, 500)
        r = self.c.post(f'/system/invoices/{sale2.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('deleted', r['Location'])
        self.assertEqual(self.stock(), 15)
        self.ok('sale deleted')


class ContractInvoiceTests(_Base):
    def test_contract_invoice_books_the_parts_cost_only(self):
        from inventory.models import MaintenanceContract
        from inventory.services.invoice_service import InvoiceService
        self.buy(10, 100)
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):   # ميزة الباقة
            contract = MaintenanceContract.objects.create(customer=self.customer, end_date='2030-01-01',
                                                          total_value=D('50000'))
        balance_before = self.customer.balance
        inv = self.post(self.quotation(3, maintenance_contract=contract))
        self.assertEqual(self.stock(), 7)
        lines = dict(AccountingEntry.objects.filter(journal_entry__sale_invoice=inv)
                     .values_list('account__code').annotate(n=Sum('debit') - Sum('credit')))
        self.assertEqual(lines, {'5001': D('300.00'), '1200': D('-300.00')})   # مفيش مديونية ولا إيراد
        self.ok('contract invoice')
        ret = InvoiceService.create_return_invoice(inv, return_items=[{'item_id': inv.items.get().pk,
                                                                      'quantity': 1}])
        self.post(ret)
        self.customer.refresh_from_db()
        self.assertEqual((self.stock(), self.customer.balance), (8, balance_before))
        self.ok('contract invoice return')


class TransferLedgerTests(_Base):
    def setUp(self):
        super().setUp()
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):
            self.b = make_branch(name='فرع ب')
        with transaction.atomic():
            set_branch_stock(self.part, self.branch, 20, unit_cost=D('100'))

    def branch_stock_value(self, branch, code='1200'):
        agg = AccountingEntry.objects.filter(journal_entry__branch=branch, account__code=code).aggregate(
            d=Sum('debit'), c=Sum('credit'))
        return (agg['d'] or 0) - (agg['c'] or 0)

    def test_each_branch_carries_its_own_goods(self):
        t = StockTransfer.objects.create(product=self.part, from_branch=self.branch, to_branch=self.b, quantity=5)
        t.status = 'in_transit'
        t.save()
        self.assertEqual(self.branch_stock_value(self.branch), D('1500.00'))
        self.assertEqual(self.branch_stock_value(self.branch, '1210'), D('500.00'))   # في الطريق
        self.ok('shipped')
        t.status = 'completed'
        t.save()
        self.assertEqual((self.branch_stock_value(self.branch), self.branch_stock_value(self.b)),
                         (D('1500.00'), D('500.00')))
        self.assertEqual(self.branch_stock_value(self.branch, '1210') + self.branch_stock_value(self.b, '1210'), 0)
        self.ok('received')

    def test_cancel_and_direct_receive(self):
        t = StockTransfer.objects.create(product=self.part, from_branch=self.branch, to_branch=self.b,
                                         quantity=4, status='in_transit')
        t.status = 'cancelled'
        t.save()
        self.assertEqual((self.branch_stock_value(self.branch), self.branch_stock_value(self.branch, '1210')),
                         (D('2000.00'), 0))
        StockTransfer.objects.create(product=self.part, from_branch=self.branch, to_branch=self.b,
                                     quantity=3, status='completed')
        self.assertEqual((self.branch_stock_value(self.branch), self.branch_stock_value(self.b)),
                         (D('1700.00'), D('300.00')))
        self.ok('cancel + direct receive')

    def test_purchase_while_goods_are_on_the_road(self):
        t = StockTransfer.objects.create(product=self.part, from_branch=self.branch, to_branch=self.b,
                                         quantity=10, status='in_transit')
        self.buy(10, 400)                              # المتوسط على 30 قطعة (منهم 10 في الطريق)
        self.assertEqual(self.avg(), D('200.00'))
        self.ok('bought while on the road')
        t.status = 'completed'
        t.save()
        self.ok('received after the purchase')


class VendorCreditTests(_Base):
    def ft_journals_match(self):
        for ft in FinancialTransaction.objects.filter(vendor=self.vendor):
            amounts = set(AccountingEntry.objects.filter(journal_entry__financial_transaction=ft)
                          .values_list('debit', 'credit'))
            self.assertTrue(amounts <= {(ft.amount, D('0')), (D('0'), ft.amount)}, (ft.pk, amounts))

    def balances(self):
        from inventory.services.vendor_payments import expected_balances
        self.vendor.refresh_from_db()
        return self.vendor.balance, expected_balances([self.vendor.pk]).get(self.vendor.pk)

    def test_advance_payment_settles_the_next_invoices(self):
        from inventory.services.vendor_payments import pay_vendor
        pay_vendor(self.vendor, D('500'), self.cash)            # مقدّم — مفيش فواتير
        cash_after_advance = Treasury.objects.get(pk=self.cash.pk).balance
        a = self.buy(3, 100)                                    # 300 ← من المقدّم كله
        a.refresh_from_db()
        self.assertEqual((a.paid_amount, a.net_due), (D('300.00'), D('0.00')))
        b = self.buy(10, 100)                                   # 1000 ← الباقي 200
        b.refresh_from_db()
        self.assertEqual((b.paid_amount, b.net_due), (D('200.00'), D('800.00')))
        self.assertEqual(self.balances(), (D('800.00'), D('800.00')))
        self.assertEqual(Treasury.objects.get(pk=self.cash.pk).balance, cash_after_advance)   # مفيش فلوس اتحركت
        self.ft_journals_match()
        self.ok('advance applied')

        r = self.c.post(f'/system/purchases/{a.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('ok=deleted', r['Location'])
        b.refresh_from_db()
        self.assertEqual((b.paid_amount, Treasury.objects.get(pk=self.cash.pk).balance),
                         (D('500.00'), cash_after_advance))     # الـ 300 رجعت رصيد واتخصمت من b
        self.assertEqual(self.balances(), (D('500.00'), D('500.00')))
        self.ft_journals_match()
        self.ok('invoice that used the credit deleted')

    def test_credit_left_by_a_return_pays_the_open_invoice(self):
        from inventory.services.purchase_return_service import return_to_vendor
        a = self.buy(10, 100, paid=1000)                        # مدفوعة كاملة
        b = self.buy(5, 100)                                    # 500 آجل
        return_to_vendor(a, [(str(a.items.get().pk), '4')])    # 400 لينا عنده
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.net_due, b.net_due, b.paid_amount), (D('0.00'), D('100.00'), D('400.00')))
        self.assertEqual(self.balances(), (D('100.00'), D('100.00')))
        self.ft_journals_match()
        self.ok('return credit applied')

    def test_old_credit_button(self):
        from inventory.services.vendor_payments import pay_vendor
        b = self.buy(10, 100)
        pay_vendor(self.vendor, D('300'), self.cash)            # بتتوزّع على b
        FinancialTransaction.objects.filter(purchase_invoice=b).update(purchase_invoice=None)
        PurchaseInvoice.objects.filter(pk=b.pk).update(paid_amount=0)   # زي داتا قديمة: مقدّم مش مربوط
        r = self.c.get(f'/system/vendors/{self.vendor.pk}/', HTTP_HOST=self.host)
        self.assertContains(r, 'apply-credit-form')
        r = self.c.post(f'/system/vendors/{self.vendor.pk}/apply-credit/', HTTP_HOST=self.host)
        self.assertIn('ok=credit', r['Location'])
        b.refresh_from_db()
        self.assertEqual(b.net_due, D('700.00'))
        self.ok('old credit applied')
