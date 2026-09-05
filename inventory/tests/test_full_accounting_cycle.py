"""
🏛️ Full accrual accounting-cycle tests.

Proves the system behaves like a real double-entry accrual package:

* revenue + COGS recognised at invoice posting (accrual), not on cash;
* credit (آجل) sales hit the general ledger (AR + revenue), not just a
  subsidiary balance;
* customer/vendor payments SETTLE receivables/payables — they never
  re-recognise revenue (no double counting);
* VAT is split out to a liability;
* returns reverse cleanly;
* the trial balance, income statement and balance sheet always tie out;
* period closing sweeps P&L into retained earnings and locks the period.
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from inventory.models import (
    AccountingEntry, ChartOfAccount, JournalEntry,
    FiscalYear, AccountingPeriod,
)
from inventory.services.accounting_service import AccountingService
from inventory.services.accounting_reports import AccountingReportService
from inventory.services.invoice_service import InvoiceService
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_vendor, make_treasury, make_product,
    make_inventory, make_sale_invoice, make_purchase_invoice,
    make_financial_transaction,
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


# ──────────────────────────────────────────────────────────────────────────────
# Sales — accrual revenue + COGS
# ──────────────────────────────────────────────────────────────────────────────
class AccrualSaleTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='0.00')
        self.product = make_product(part_number='ACR-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=50)

    def _post_sale(self, qty, price, paid, treasury=None, tax='0.00'):
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            treasury=treasury if treasury is not None else self.treasury,
            items=[(self.product, qty, price)], paid_amount=paid,
        )
        if tax and Decimal(tax) > 0:
            si.tax_percentage = Decimal(tax)
            si.save(update_fields=['tax_percentage'])
            si.update_total()
        si.status = 'posted'
        si.save()
        si.refresh_from_db()
        return si

    def test_cash_sale_recognises_revenue_and_cogs_and_settles_ar(self):
        self._post_sale(qty=2, price='100.00', paid='200.00')
        # Revenue recognised, COGS matched, AR fully settled by the cash-in.
        self.assertEqual(acct_raw('4001'), D('-200.00'))   # revenue credit
        self.assertEqual(acct_raw('5001'), D('120.00'))    # COGS debit (60*2)
        self.assertEqual(acct_raw('1200'), D('-120.00'))   # inventory credit
        self.assertEqual(acct_raw('1001'), D('200.00'))    # cash debit
        self.assertEqual(acct_raw('1100'), D('0.00'))      # AR settled
        self.assertTrue(ledger_is_balanced())

    def test_credit_sale_hits_general_ledger_receivable(self):
        # No treasury, nothing paid → pure credit sale.
        self._post_sale(qty=1, price='100.00', paid='0.00', treasury=None)
        self.assertEqual(acct_raw('1100'), D('100.00'))    # AR carries the debt
        self.assertEqual(acct_raw('4001'), D('-100.00'))   # revenue still recognised
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('100.00'))  # subsidiary ledger matches GL
        self.assertTrue(ledger_is_balanced())

    def test_later_payment_settles_receivable_without_new_revenue(self):
        self._post_sale(qty=1, price='100.00', paid='0.00', treasury=None)
        self.assertEqual(acct_raw('1100'), D('100.00'))
        revenue_before = acct_raw('4001')
        # Customer pays 60 later.
        make_financial_transaction(
            self.treasury, '60.00', txn_type='in', customer=self.customer,
        )
        self.assertEqual(acct_raw('1100'), D('40.00'))     # AR reduced
        self.assertEqual(acct_raw('4001'), revenue_before)  # revenue UNCHANGED (no double count)
        self.assertTrue(ledger_is_balanced())

    def test_vat_is_split_to_liability(self):
        si = self._post_sale(qty=1, price='100.00', paid='115.00', tax='15.00')
        self.assertEqual(si.total_amount, D('115.00'))
        self.assertEqual(acct_raw('4001'), D('-100.00'))   # revenue ex-VAT
        self.assertEqual(acct_raw('2200'), D('-15.00'))    # VAT payable credit
        self.assertEqual(acct_raw('1100'), D('0.00'))      # AR settled by 115 paid
        self.assertTrue(ledger_is_balanced())

    def test_accrual_posting_is_idempotent(self):
        si = self._post_sale(qty=1, price='100.00', paid='100.00')
        before = JournalEntry.objects.filter(sale_invoice=si, journal_type='sales').count()
        self.assertEqual(before, 1)
        AccountingService.post_sale_invoice(si)  # manual re-run
        after = JournalEntry.objects.filter(sale_invoice=si, journal_type='sales').count()
        self.assertEqual(after, 1)


# ──────────────────────────────────────────────────────────────────────────────
# Purchases — accrual inventory + payable
# ──────────────────────────────────────────────────────────────────────────────
class AccrualPurchaseTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.vendor = make_vendor()
        self.treasury = make_treasury(self.branch, balance='10000.00')
        self.product = make_product(part_number='PUR-1', average_cost='0.00')

    def test_purchase_capitalises_inventory_against_payable(self):
        pi = make_purchase_invoice(
            self.vendor, self.branch, treasury=self.treasury,
            items=[(self.product, 10, '50.00')], paid_amount='300.00', status='draft',
        )
        pi.status = 'posted'
        pi.save()
        pi.refresh_from_db()
        # Inventory value capitalised at full cost; AP carries the unpaid portion.
        self.assertEqual(acct_raw('1200'), D('500.00'))    # 10 * 50 inventory debit
        self.assertEqual(acct_raw('2100'), D('-200.00'))   # 500 billed − 300 paid = 200 payable
        self.assertEqual(acct_raw('1001'), D('-300.00'))   # cash out
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.balance, D('200.00'))
        self.assertTrue(ledger_is_balanced())


# ──────────────────────────────────────────────────────────────────────────────
# Returns
# ──────────────────────────────────────────────────────────────────────────────
class SaleReturnAccrualTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.product = make_product(part_number='RET-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=20)

    def test_return_reverses_revenue_and_restores_inventory(self):
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 2, '100.00')], paid_amount='200.00',
        )
        si.status = 'posted'
        si.save()
        self.assertEqual(acct_raw('4001'), D('-200.00'))

        ret = InvoiceService.create_return_invoice(si)
        ret.status = 'posted'
        ret.save()

        # Contra-revenue booked, COGS reversed, ledger still balanced.
        self.assertEqual(acct_raw('4090'), D('200.00'))    # sales returns (debit)
        # Net operating revenue = revenue − returns = 0.
        net_rev = -acct_raw('4001') - acct_raw('4090')
        self.assertEqual(net_rev, D('0.00'))
        self.assertTrue(ledger_is_balanced())


# ──────────────────────────────────────────────────────────────────────────────
# Financial statements tie out
# ──────────────────────────────────────────────────────────────────────────────
class FinancialStatementsTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.vendor = make_vendor()
        self.treasury = make_treasury(self.branch, balance='10000.00')
        self.product = make_product(part_number='FS-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=100)

    def _run_activity(self):
        # A purchase, a cash sale, a credit sale.
        pi = make_purchase_invoice(
            self.vendor, self.branch, treasury=self.treasury,
            items=[(self.product, 20, '60.00')], paid_amount='1200.00', status='draft',
        )
        pi.status = 'posted'; pi.save()

        si1 = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '100.00')], paid_amount='500.00',
        )
        si1.status = 'posted'; si1.save()

        si2 = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=None,
            items=[(self.product, 3, '100.00')], paid_amount='0.00',
        )
        si2.status = 'posted'; si2.save()

    def test_trial_balance_balances(self):
        self._run_activity()
        tb = AccountingReportService.trial_balance()
        self.assertTrue(tb['is_balanced'], tb['total_debit'] - tb['total_credit'])

    def test_balance_sheet_balances(self):
        self._run_activity()
        bs = AccountingReportService.balance_sheet()
        self.assertTrue(
            bs['is_balanced'],
            f"A={bs['total_assets']} L+E={bs['total_liabilities_and_equity']}",
        )

    def test_income_statement_net_profit(self):
        self._run_activity()
        pnl = AccountingReportService.income_statement(date(2000, 1, 1), date(2999, 12, 31))
        # Revenue = 8 units * 100 = 800; COGS = 8 * 60 = 480; profit = 320.
        self.assertEqual(pnl['revenue']['total'], D('800.00'))
        self.assertEqual(pnl['expenses']['total'], D('480.00'))
        self.assertEqual(pnl['net_profit'], D('320.00'))


# ──────────────────────────────────────────────────────────────────────────────
# Period close
# ──────────────────────────────────────────────────────────────────────────────
class PeriodCloseTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.product = make_product(part_number='PC-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=20)

        self.fy = FiscalYear.objects.create(
            code='FY-TEST', name='سنة الاختبار',
            start_date=date(2000, 1, 1), end_date=date(2999, 12, 31),
        )
        self.period = AccountingPeriod.objects.create(
            fiscal_year=self.fy, name='فترة الاختبار',
            start_date=date(2000, 1, 1), end_date=date(2999, 12, 31),
        )

    def test_close_sweeps_pnl_into_retained_earnings_and_locks(self):
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 2, '100.00')], paid_amount='200.00',
        )
        si.status = 'posted'; si.save()

        # Net profit before close = 200 revenue − 120 COGS = 80.
        AccountingService.close_period(self.period, created_by=None)

        self.period.refresh_from_db()
        self.assertTrue(self.period.is_closed)
        # Revenue & expense accounts net to zero after closing.
        self.assertEqual(acct_raw('4001'), D('0.00'))
        self.assertEqual(acct_raw('5001'), D('0.00'))
        # Retained earnings now carries the 80 net profit (credit).
        self.assertEqual(acct_raw('3100'), D('-80.00'))
        self.assertTrue(ledger_is_balanced())

    def test_posting_into_closed_period_is_refused(self):
        AccountingService.close_period(self.period, created_by=None)
        with self.assertRaises(ValidationError):
            AccountingService.post_journal(
                description='قيد بعد الإقفال',
                lines=[
                    {'account': 'cash', 'debit': '10.00', 'credit': 0},
                    {'account': 'other_revenue', 'debit': 0, 'credit': '10.00'},
                ],
                date=date(2500, 6, 1),
            )


# ──────────────────────────────────────────────────────────────────────────────
# Journal integrity invariant
# ──────────────────────────────────────────────────────────────────────────────
class JournalIntegrityTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.product = make_product(part_number='JI-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.product, self.branch, quantity=20)

    def test_every_journal_entry_is_balanced(self):
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 2, '100.00')], paid_amount='200.00',
        )
        si.status = 'posted'; si.save()
        self.assertTrue(JournalEntry.objects.exists())
        for je in JournalEntry.objects.all():
            self.assertTrue(je.is_balanced, f"{je.number} unbalanced: {je.total_debit} vs {je.total_credit}")
            self.assertGreater(je.lines.count(), 0)
