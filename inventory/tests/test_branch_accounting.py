"""
Per-branch accounting — every journal line must carry the branch dimension so
the shared chart of accounts can still produce a per-branch P&L, and the
consolidated totals must tie out to the full ledger.
"""
import uuid
from decimal import Decimal

from inventory.models import FinancialTransaction, AccountingEntry
from inventory.services.reporting_service import ReportingService
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_product, make_inventory,
    make_treasury, make_sale_invoice,
)


class BranchAccountingTests(ERPTenantTestCase):

    def setUp(self):
        from django.db import connection
        connection.set_schema_to_public()
        self.tenant.max_branches = 0
        self.tenant.max_treasuries = 0
        self.tenant.save(update_fields=['max_branches', 'max_treasuries'])
        connection.set_tenant(self.tenant)

        self.branch_a = make_branch(name='فرع أ')
        self.branch_b = make_branch(name='فرع ب')
        self.treasury_a = make_treasury(self.branch_a, balance='1000.00')
        self.treasury_b = make_treasury(self.branch_b, balance='1000.00')

    def test_income_entries_tagged_with_treasury_branch(self):
        ft = FinancialTransaction.objects.create(
            treasury=self.treasury_a, transaction_type='in',
            amount=Decimal('300.00'), description='إيراد نقدي',
        )
        entries = AccountingEntry.objects.filter(financial_transaction=ft)
        self.assertEqual(entries.count(), 2)
        for e in entries:
            self.assertEqual(e.branch_id, self.branch_a.pk)

    def test_expense_entries_tagged_with_treasury_branch(self):
        ft = FinancialTransaction.objects.create(
            treasury=self.treasury_b, transaction_type='out',
            amount=Decimal('120.00'), description='إيجار',
        )
        entries = AccountingEntry.objects.filter(financial_transaction=ft)
        self.assertEqual(entries.count(), 2)
        for e in entries:
            self.assertEqual(e.branch_id, self.branch_b.pk)

    def test_branch_pnl_separates_revenue_and_expense(self):
        # Branch A: 300 revenue in. Branch B: 120 expense out.
        FinancialTransaction.objects.create(
            treasury=self.treasury_a, transaction_type='in',
            amount=Decimal('300.00'), description='إيراد',
        )
        FinancialTransaction.objects.create(
            treasury=self.treasury_b, transaction_type='out',
            amount=Decimal('120.00'), description='مصروف',
        )
        pnl = ReportingService.branch_pnl()
        rows = {r['branch_name']: r for r in pnl['branches']}

        self.assertEqual(rows['فرع أ']['revenue'], Decimal('300.00'))
        self.assertEqual(rows['فرع أ']['expenses'], Decimal('0'))
        self.assertEqual(rows['فرع أ']['net'], Decimal('300.00'))

        self.assertEqual(rows['فرع ب']['revenue'], Decimal('0'))
        self.assertEqual(rows['فرع ب']['expenses'], Decimal('120.00'))
        self.assertEqual(rows['فرع ب']['net'], Decimal('-120.00'))

        self.assertEqual(pnl['totals']['revenue'], Decimal('300.00'))
        self.assertEqual(pnl['totals']['expenses'], Decimal('120.00'))
        self.assertEqual(pnl['totals']['net'], Decimal('180.00'))

    def test_sale_invoice_payment_tags_branch(self):
        customer = make_customer()
        product = make_product(part_number=f'BA-{uuid.uuid4().hex[:6]}', retail_price='100.00')
        make_inventory(product, self.branch_a, quantity=50)
        si = make_sale_invoice(
            customer=customer, branch=self.branch_a, treasury=self.treasury_a,
            items=[(product, 2, '100.00')], paid_amount='200.00',
        )
        si.status = 'posted'
        si.save()
        # The paid_amount created a cash-in FinancialTransaction → its ledger
        # entries must be tagged to branch A.
        tagged = AccountingEntry.objects.filter(
            sale_invoice=si, branch=self.branch_a,
        )
        self.assertTrue(tagged.exists())
        self.assertFalse(
            AccountingEntry.objects.filter(sale_invoice=si)
            .exclude(branch=self.branch_a).exists()
        )
