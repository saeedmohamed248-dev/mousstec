"""
🚢 Landed-cost & shipment-expense tests.

Proves the full cycle for extra costs on a purchase (a shipment/container):

* landed costs (customs/freight/loading/insurance) capitalise into the
  product's moving-average cost, allocated BY VALUE;
* period costs (travel/food) do NOT touch product cost — they hit an
  operating-expense account;
* paying an extra cost from a treasury books a real cash-out and the right
  counter-account (Inventory for landed, Expense for period cost);
* an unpaid extra cost accrues to the import-costs payable;
* the general ledger always ties out.
"""
from decimal import Decimal

from django.db.models import Sum

from inventory.models import (
    AccountingEntry, ChartOfAccount, PurchaseInvoiceExtraCost,
)
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_vendor, make_treasury, make_product,
    make_inventory, make_purchase_invoice,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def acct_raw(code):
    """Signed (debit − credit) balance of an account, straight from the ledger."""
    acct = ChartOfAccount.objects.filter(code=code).first()
    if not acct:
        return Decimal('0.00')
    agg = AccountingEntry.objects.filter(account=acct).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or Decimal('0')) - (agg['c'] or Decimal('0'))


def ledger_is_balanced():
    agg = AccountingEntry.objects.aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or Decimal('0')) == (agg['c'] or Decimal('0'))


class LandedCostTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.vendor = make_vendor()
        self.treasury = make_treasury(self.branch, balance='20000.00')

    def _post(self, pi):
        pi.status = 'posted'
        pi.save()
        pi.refresh_from_db()

    def test_landed_costs_capitalise_and_treasury_pays(self):
        """Customs (landed) → into avg cost; travel (expense) → out of cost;
        both paid from treasury book real cash-out to the right accounts."""
        product = make_product(part_number='LC-1', average_cost='0.00', purchase_price='0.00')
        make_inventory(product, self.branch, quantity=0)
        pi = make_purchase_invoice(
            self.vendor, self.branch, items=[(product, 1, '14000.00')], status='draft',
        )
        PurchaseInvoiceExtraCost.objects.create(
            invoice=pi, kind='customs', behavior='landed',
            amount=D('3500.00'), treasury=self.treasury,
        )
        PurchaseInvoiceExtraCost.objects.create(
            invoice=pi, kind='travel', behavior='expense',
            amount=D('500.00'), treasury=self.treasury,
        )
        self._post(pi)

        # Product cost = supplier price + landed only (travel excluded).
        product.refresh_from_db()
        self.assertEqual(product.average_cost, D('17500.00'))
        self.assertEqual(product.purchase_price, D('14000.00'))  # raw supplier price
        item = pi.items.first()
        self.assertEqual(item.landed_unit_cost, D('17500.00'))

        # Treasury paid both extras (goods unpaid): 20000 − 3500 − 500 = 16000.
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('16000.00'))

        # Ledger: inventory = goods 14000 + landed 3500; expense 5150 = 500.
        self.assertEqual(acct_raw('1200'), D('17500.00'))
        self.assertEqual(acct_raw('5150'), D('500.00'))
        self.assertEqual(acct_raw('1001'), D('-4000.00'))   # cash out for extras
        self.assertEqual(acct_raw('2100'), D('-14000.00'))  # goods payable to vendor
        self.assertTrue(ledger_is_balanced())

    def test_allocation_by_value_across_items(self):
        """Landed cost splits by line value: the pricier line carries more."""
        cheap = make_product(part_number='LC-CHEAP', average_cost='0.00')
        dear = make_product(part_number='LC-DEAR', average_cost='0.00')
        make_inventory(cheap, self.branch, quantity=0)
        make_inventory(dear, self.branch, quantity=0)
        # values: cheap 1*1000 = 1000, dear 1*3000 = 3000, total 4000.
        pi = make_purchase_invoice(
            self.vendor, self.branch,
            items=[(cheap, 1, '1000.00'), (dear, 1, '3000.00')], status='draft',
        )
        PurchaseInvoiceExtraCost.objects.create(
            invoice=pi, kind='shipping', behavior='landed', amount=D('400.00'),
        )
        self._post(pi)
        cheap.refresh_from_db()
        dear.refresh_from_db()
        # 400 split by value: cheap 25% = 100 → 1100; dear 75% = 300 → 3300.
        self.assertEqual(cheap.average_cost, D('1100.00'))
        self.assertEqual(dear.average_cost, D('3300.00'))

    def test_expense_only_does_not_touch_product_cost(self):
        """A pure period cost (food) never moves the moving-average cost."""
        product = make_product(part_number='LC-EXP', average_cost='60.00')
        make_inventory(product, self.branch, quantity=10)
        pi = make_purchase_invoice(
            self.vendor, self.branch, items=[(product, 10, '100.00')], status='draft',
        )
        PurchaseInvoiceExtraCost.objects.create(
            invoice=pi, kind='food', behavior='expense',
            amount=D('300.00'), treasury=self.treasury,
        )
        self._post(pi)
        product.refresh_from_db()
        # weighted avg = (10*60 + 10*100) / 20 = 80; food excluded.
        self.assertEqual(product.average_cost, D('80.00'))
        self.assertEqual(acct_raw('5150'), D('300.00'))
        self.assertTrue(ledger_is_balanced())

    def test_unpaid_extra_accrues_to_payable(self):
        """No treasury → landed cost still capitalises, credited to accrued payable."""
        product = make_product(part_number='LC-ACCR', average_cost='0.00')
        make_inventory(product, self.branch, quantity=0)
        pi = make_purchase_invoice(
            self.vendor, self.branch, items=[(product, 1, '1000.00')], status='draft',
        )
        PurchaseInvoiceExtraCost.objects.create(
            invoice=pi, kind='customs', behavior='landed', amount=D('200.00'),
        )
        self._post(pi)
        product.refresh_from_db()
        self.assertEqual(product.average_cost, D('1200.00'))
        self.assertEqual(acct_raw('1200'), D('1200.00'))       # goods + landed
        self.assertEqual(acct_raw('2120'), D('-200.00'))       # import costs payable
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('20000.00'))  # untouched
        self.assertTrue(ledger_is_balanced())

    def test_default_behavior_filled_on_save(self):
        """behavior left blank falls back to the kind's default."""
        pi = make_purchase_invoice(
            self.vendor, self.branch,
            items=[(make_product(part_number='LC-DEF'), 1, '100.00')], status='draft',
        )
        customs = PurchaseInvoiceExtraCost.objects.create(invoice=pi, kind='customs', amount=D('10'))
        travel = PurchaseInvoiceExtraCost.objects.create(invoice=pi, kind='travel', amount=D('10'))
        self.assertEqual(customs.behavior, 'landed')
        self.assertEqual(travel.behavior, 'expense')
