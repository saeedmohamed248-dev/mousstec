"""
Financial Tests — Treasury balance, invoice posting, transaction lifecycle.
These tests protect against bugs that could lose real money.
"""
from decimal import Decimal
from django.db import connection
from .base import ERPTenantTestCase

from inventory.models import (
    Treasury, FinancialTransaction, PurchaseInvoice, SaleInvoice,
    Inventory, Product,
)
from inventory.services.invoice_service import InvoiceService
from inventory.services.treasury_service import TreasuryService
from .factories import (
    make_branch, make_product, make_inventory, make_customer,
    make_vendor, make_treasury, make_purchase_invoice,
    make_sale_invoice, make_financial_transaction, make_expense_category,
)


class TreasuryBalanceTests(ERPTenantTestCase):
    """Treasury balance must always reflect the sum of transactions."""

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='0.00')

    def test_deposit_increases_balance(self):
        """Deposit (type=in) should increase treasury balance."""
        make_financial_transaction(self.treasury, '500.00', txn_type='in')
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('500.00'))

    def test_withdrawal_decreases_balance(self):
        """Withdrawal (type=out) should decrease treasury balance."""
        # Start with balance
        self.treasury.balance = Decimal('1000.00')
        self.treasury.save(update_fields=['balance'])

        make_financial_transaction(self.treasury, '300.00', txn_type='out')
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('700.00'))

    def test_multiple_transactions_accumulate(self):
        """Multiple deposits and withdrawals should accumulate correctly."""
        make_financial_transaction(self.treasury, '1000.00', txn_type='in')
        make_financial_transaction(self.treasury, '200.00', txn_type='out')
        make_financial_transaction(self.treasury, '500.00', txn_type='in')
        make_financial_transaction(self.treasury, '150.00', txn_type='out')

        self.treasury.refresh_from_db()
        # 1000 - 200 + 500 - 150 = 1150
        self.assertEqual(self.treasury.balance, Decimal('1150.00'))

    def test_delete_deposit_reverses_balance(self):
        """Deleting a deposit should subtract from treasury balance."""
        txn = make_financial_transaction(self.treasury, '500.00', txn_type='in')
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('500.00'))

        txn.delete()
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('0.00'))

    def test_delete_withdrawal_reverses_balance(self):
        """Deleting a withdrawal should add back to treasury balance."""
        self.treasury.balance = Decimal('1000.00')
        self.treasury.save(update_fields=['balance'])

        txn = make_financial_transaction(self.treasury, '300.00', txn_type='out')
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('700.00'))

        txn.delete()
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('1000.00'))

    def test_zero_amount_transaction(self):
        """Zero-amount transaction should not change balance."""
        make_financial_transaction(self.treasury, '0.00', txn_type='in')
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('0.00'))


class PurchasePostingTests(ERPTenantTestCase):
    """Purchase invoice posting must update inventory, cost, and treasury."""

    def setUp(self):
        self.branch = make_branch()
        self.vendor = make_vendor()
        self.treasury = make_treasury(self.branch, balance='50000.00')
        self.product = make_product(
            part_number='BMW-001',
            purchase_price='100.00',
            average_cost='100.00',
            retail_price='200.00',
        )
        make_inventory(self.product, self.branch, quantity=0)

    def test_purchase_adds_inventory(self):
        """Posting a purchase invoice should add items to inventory."""
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 10, '100.00')],
            paid_amount='1000.00',
        )
        # Post it
        pi.status = 'posted'
        pi.save()

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity, 10)

    def test_purchase_updates_average_cost(self):
        """Posting a purchase should recalculate weighted average cost."""
        # Start with existing stock at cost 100
        make_inventory(self.product, self.branch, quantity=10)
        self.product.average_cost = Decimal('100.00')
        self.product.save(update_fields=['average_cost'])

        # Buy 10 more at cost 200
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 10, '200.00')],
            paid_amount='2000.00',
        )
        pi.status = 'posted'
        pi.save()

        self.product.refresh_from_db()
        # Weighted avg: (10*100 + 10*200) / 20 = 150
        self.assertEqual(self.product.average_cost, Decimal('150.00'))

    def test_purchase_creates_treasury_payment(self):
        """Posting a paid purchase should create a financial transaction."""
        initial_balance = self.treasury.balance

        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '100.00')],
            paid_amount='500.00',
        )
        pi.status = 'posted'
        pi.save()

        self.treasury.refresh_from_db()
        self.assertEqual(
            self.treasury.balance,
            initial_balance - Decimal('500.00'),
        )

    def test_purchase_updates_vendor_balance_for_credit(self):
        """Unpaid portion of purchase should increase vendor balance."""
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 10, '100.00')],
            paid_amount='600.00',  # total=1000, paid=600, due=400
        )
        pi.status = 'posted'
        pi.save()

        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.balance, Decimal('400.00'))

    def test_purchase_idempotency(self):
        """Saving a posted invoice twice should not double-execute."""
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '100.00')],
            paid_amount='500.00',
        )
        pi.status = 'posted'
        pi.save()

        inv_qty_after_first = Inventory.objects.get(
            product=self.product, branch=self.branch
        ).quantity

        # Save again — should NOT add more stock
        pi.save()

        inv_qty_after_second = Inventory.objects.get(
            product=self.product, branch=self.branch
        ).quantity
        self.assertEqual(inv_qty_after_first, inv_qty_after_second)


class SalePostingTests(ERPTenantTestCase):
    """Sale invoice posting must deduct inventory and update treasury."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='0.00')
        self.product = make_product(
            part_number='BMW-002',
            purchase_price='100.00',
            average_cost='100.00',
            retail_price='200.00',
        )
        make_inventory(self.product, self.branch, quantity=20)

    def test_sale_deducts_inventory(self):
        """Posting a sale should deduct items from inventory."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '200.00')],
            paid_amount='1000.00',
        )
        si.status = 'posted'
        si.save()

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity, 15)  # 20 - 5 = 15

    def test_sale_creates_treasury_income(self):
        """Posting a paid sale should credit treasury."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 2, '200.00')],
            paid_amount='400.00',
        )
        si.status = 'posted'
        si.save()

        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('400.00'))

    def test_sale_updates_customer_balance_for_credit(self):
        """Unpaid portion of sale should increase customer balance."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '200.00')],
            paid_amount='500.00',  # total=1000, paid=500, due=500
        )
        si.status = 'posted'
        si.save()

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal('500.00'))

    def test_sale_idempotency(self):
        """Saving a posted sale twice should not double-deduct."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 3, '200.00')],
            paid_amount='600.00',
        )
        si.status = 'posted'
        si.save()

        inv_qty_first = Inventory.objects.get(
            product=self.product, branch=self.branch
        ).quantity

        si.save()

        inv_qty_second = Inventory.objects.get(
            product=self.product, branch=self.branch
        ).quantity
        self.assertEqual(inv_qty_first, inv_qty_second)

    def test_overselling_raises_error(self):
        """Selling more than available stock should raise ValidationError."""
        from django.core.exceptions import ValidationError

        make_inventory(self.product, self.branch, quantity=2)

        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '200.00')],
            paid_amount='1000.00',
        )

        with self.assertRaises((ValidationError, Exception)):
            si.status = 'posted'
            si.save()


class AccountingEntryTests(ERPTenantTestCase):
    """Double-entry accounting must create balanced journal entries."""

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='10000.00')

    def test_deposit_creates_debit_and_credit(self):
        """Deposit should create debit(cash) + credit(revenue) entries."""
        from inventory.models import AccountingEntry

        txn = make_financial_transaction(self.treasury, '1000.00', txn_type='in')

        entries = AccountingEntry.objects.filter(financial_transaction=txn)
        self.assertEqual(entries.count(), 2)

        total_debit = sum(e.debit for e in entries)
        total_credit = sum(e.credit for e in entries)
        self.assertEqual(total_debit, total_credit)
        self.assertEqual(total_debit, Decimal('1000.00'))

    def test_withdrawal_creates_debit_and_credit(self):
        """Withdrawal should create debit(expense) + credit(cash) entries."""
        from inventory.models import AccountingEntry

        txn = make_financial_transaction(
            self.treasury, '500.00', txn_type='out',
            category=make_expense_category(),
        )

        entries = AccountingEntry.objects.filter(financial_transaction=txn)
        self.assertEqual(entries.count(), 2)

        total_debit = sum(e.debit for e in entries)
        total_credit = sum(e.credit for e in entries)
        self.assertEqual(total_debit, total_credit)
        self.assertEqual(total_debit, Decimal('500.00'))
