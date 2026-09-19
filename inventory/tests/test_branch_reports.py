"""
🏢 Per-branch financial reports.

A journal entry carries the branch of its source document, so the trial
balance and income statement can be scoped to one branch (or consolidated
for the whole company when no branch is given).
"""
from datetime import date
from decimal import Decimal

from inventory.services.accounting_service import AccountingService
from inventory.services.accounting_reports import AccountingReportService
from inventory.models import JournalEntry, Branch
from .base import ERPTenantTestCase
from .factories import make_vendor, make_product, make_inventory, make_purchase_invoice

D = lambda v: Decimal(str(v))  # noqa: E731


class BranchReportsTests(ERPTenantTestCase):
    def setUp(self):
        # bulk_create يتخطّى إشارة حد الفروع في الباقة (محتاجين فرعين للتقرير فقط).
        self.a, self.b = Branch.objects.bulk_create([Branch(name='فرع أ'), Branch(name='فرع ب')])

    def _revenue(self, branch, amount):
        AccountingService.post_journal(
            description='بيع نقدي', journal_type='sales', branch=branch,
            lines=[
                {'account': 'cash', 'debit': D(amount), 'credit': 0},
                {'account': 'sales_revenue', 'debit': 0, 'credit': D(amount)},
            ],
        )

    def test_trial_balance_scoped_to_branch(self):
        self._revenue(self.a, 1000)
        self._revenue(self.b, 400)

        tb_a = AccountingReportService.trial_balance(branch=self.a)
        rev_a = next((r for r in tb_a['rows'] if r['code'] == '4001'), None)
        self.assertIsNotNone(rev_a)
        self.assertEqual(rev_a['credit'], D('1000'))
        self.assertTrue(tb_a['is_balanced'])

        tb_all = AccountingReportService.trial_balance()  # consolidated
        rev_all = next((r for r in tb_all['rows'] if r['code'] == '4001'), None)
        self.assertEqual(rev_all['credit'], D('1400'))

    def test_income_statement_scoped_to_branch(self):
        self._revenue(self.a, 1000)
        self._revenue(self.b, 400)
        today = date.today()
        is_b = AccountingReportService.income_statement(
            date(today.year, 1, 1), today, branch=self.b)
        self.assertEqual(is_b['revenue']['total'], D('400'))
        is_all = AccountingReportService.income_statement(date(today.year, 1, 1), today)
        self.assertEqual(is_all['revenue']['total'], D('1400'))

    def test_branch_derived_from_source_invoice(self):
        """A posted purchase invoice tags its journal entries with its branch."""
        vendor = make_vendor()
        product = make_product(part_number='BR-1', average_cost='0')
        make_inventory(product, self.a, quantity=0)
        pi = make_purchase_invoice(
            vendor, self.a, items=[(product, 2, '100.00')], status='draft')
        pi.status = 'posted'
        pi.save()
        je = JournalEntry.objects.filter(purchase_invoice=pi).first()
        self.assertIsNotNone(je)
        self.assertEqual(je.branch_id, self.a.id)
