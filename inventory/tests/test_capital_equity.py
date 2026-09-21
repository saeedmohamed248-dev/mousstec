"""
💼 Owner-equity movement tests.

Capital injections and owner drawings must hit the CAPITAL account (3001,
equity) — never revenue or expense — so they don't distort profit.
"""
from decimal import Decimal

from django.db.models import Sum

from inventory.models import AccountingEntry, ChartOfAccount, FinancialTransaction
from .base import ERPTenantTestCase
from .factories import make_branch, make_treasury

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


class OwnerEquityTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='0.00')

    def test_capital_injection_credits_equity_not_revenue(self):
        FinancialTransaction.objects.create(
            treasury=self.treasury, transaction_type='in', amount=D('1000000'),
            description='رأس مال', equity_kind='capital',
        )
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('1000000'))   # cash is in the box
        self.assertEqual(acct_raw('3001'), D('-1000000'))       # capital (equity) credited
        self.assertEqual(acct_raw('1001'), D('1000000'))        # cash debited
        self.assertEqual(acct_raw('4099'), D('0'))              # NOT other revenue
        self.assertTrue(ledger_is_balanced())

    def test_owner_drawings_debit_equity(self):
        # seed some cash first (as capital), then the owner draws part of it
        FinancialTransaction.objects.create(
            treasury=self.treasury, transaction_type='in', amount=D('50000'),
            description='رأس مال', equity_kind='capital',
        )
        FinancialTransaction.objects.create(
            treasury=self.treasury, transaction_type='out', amount=D('20000'),
            description='مسحوبات', equity_kind='drawings',
        )
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('30000'))     # 50k − 20k
        # capital: +50k in, −20k drawn = net 30k credit balance
        self.assertEqual(acct_raw('3001'), D('-30000'))
        self.assertEqual(acct_raw('4099'), D('0'))
        self.assertTrue(ledger_is_balanced())

    def test_plain_deposit_still_revenue(self):
        """A normal deposit (no equity_kind) is unchanged — books to other revenue."""
        FinancialTransaction.objects.create(
            treasury=self.treasury, transaction_type='in', amount=D('500'),
            description='إيداع عادي',
        )
        self.assertEqual(acct_raw('4099'), D('-500'))   # other revenue
        self.assertEqual(acct_raw('3001'), D('0'))      # capital untouched
        self.assertTrue(ledger_is_balanced())
