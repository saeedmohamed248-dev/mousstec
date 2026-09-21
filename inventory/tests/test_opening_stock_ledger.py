"""
🏛️ Opening / direct stock capitalisation + inventory-ledger reconciliation.

Opening stock (added without a purchase bill) must debit the Inventory asset
against Opening-Balance Equity — otherwise selling it drives the Inventory GL
account negative. The reconcile command repairs any historical drift.
"""
from decimal import Decimal

from django.db.models import Sum

from inventory.models import AccountingEntry, ChartOfAccount, Inventory
from inventory.services.accounting_service import AccountingService
from inventory.management.commands.reconcile_inventory_ledger import Command as ReconcileCmd
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_inventory

D = lambda v: Decimal(str(v))  # noqa: E731


def acct_raw(code):
    acct = ChartOfAccount.objects.filter(code=code).first()
    if not acct:
        return Decimal('0.00')
    agg = AccountingEntry.objects.filter(account=acct).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or Decimal('0')) - (agg['c'] or Decimal('0'))


def ledger_is_balanced():
    agg = AccountingEntry.objects.aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or Decimal('0')) == (agg['c'] or Decimal('0'))


class OpeningStockLedgerTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()

    def test_opening_stock_capitalises_to_equity(self):
        AccountingService.post_opening_stock(
            amount=D('1000'), description='مخزون افتتاحي — قطعة',
        )
        self.assertEqual(acct_raw('1200'), D('1000'))    # inventory asset up
        self.assertEqual(acct_raw('3005'), D('-1000'))   # opening-balance equity
        self.assertTrue(ledger_is_balanced())

    def test_zero_amount_posts_nothing(self):
        self.assertIsNone(AccountingService.post_opening_stock(amount=D('0'), description='x'))
        self.assertEqual(acct_raw('1200'), D('0'))

    def test_reconcile_matches_ledger_to_physical_value(self):
        # Physical stock worth 10 × 50 = 500, but nothing was capitalised in the GL.
        p = make_product(part_number='REC-1', average_cost='50.00')
        make_inventory(p, self.branch, quantity=10)
        self.assertEqual(acct_raw('1200'), D('0'))       # ledger empty

        ReconcileCmd()._reconcile_current_schema(dry_run=False)

        self.assertEqual(acct_raw('1200'), D('500'))     # now matches physical value
        self.assertEqual(acct_raw('3005'), D('-500'))    # offset to opening equity
        self.assertTrue(ledger_is_balanced())

    def test_reconcile_fixes_negative_inventory(self):
        # Simulate selling uncapitalised stock: a lone credit drives inventory negative.
        p = make_product(part_number='REC-2', average_cost='50.00')
        make_inventory(p, self.branch, quantity=10)      # physical value 500
        AccountingService.post_journal(
            description='بيع مخزون غير مرسمَل',
            lines=[
                {'account': 'cogs', 'debit': D('200'), 'credit': 0},
                {'account': 'inventory', 'debit': 0, 'credit': D('200')},
            ],
            journal_type='sales',
        )
        self.assertEqual(acct_raw('1200'), D('-200'))    # negative!

        ReconcileCmd()._reconcile_current_schema(dry_run=False)

        self.assertEqual(acct_raw('1200'), D('500'))     # repaired to physical value
        self.assertTrue(ledger_is_balanced())

    def test_reconcile_dry_run_changes_nothing(self):
        p = make_product(part_number='REC-3', average_cost='50.00')
        make_inventory(p, self.branch, quantity=10)
        ReconcileCmd()._reconcile_current_schema(dry_run=True)
        self.assertEqual(acct_raw('1200'), D('0'))       # untouched
