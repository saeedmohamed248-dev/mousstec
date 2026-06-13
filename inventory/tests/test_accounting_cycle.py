"""
Accounting Cycle Tests — double-entry integrity, commission ledger balance,
inventory conservation, and EscrowHold money preservation.

These tests were added after the 2026-06-13 audit to cover gaps found in:
- bulk_create() bypassing AccountingEntry.clean()
- validate_balanced() never being called after commission entries
- EscrowHold conservation law not enforced at application level
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection

from inventory.models import (
    AccountingEntry, ChartOfAccount, FinancialTransaction, SaleInvoice,
    Inventory,
)
from inventory.services.treasury_service import TreasuryService
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_expense_category,
    make_financial_transaction, make_inventory, make_product,
    make_sale_invoice, make_treasury, make_vendor,
)


# ──────────────────────────────────────────────────────────────────────────────
# AccountingEntry validation
# ──────────────────────────────────────────────────────────────────────────────
class AccountingEntryValidationTests(ERPTenantTestCase):
    """AccountingEntry.clean() must enforce single-sided entries."""

    def _make_account(self, code, name, account_type='asset'):
        obj, _ = ChartOfAccount.objects.get_or_create(
            code=code, defaults={'name': name, 'account_type': account_type},
        )
        return obj

    def test_debit_only_entry_is_valid(self):
        """An entry with only debit > 0 is valid."""
        acct = self._make_account('TST-D01', 'نقدية اختبار', 'asset')
        entry = AccountingEntry(
            reference='TEST-001',
            description='قيد مدين',
            account=acct,
            debit=Decimal('100.00'),
            credit=Decimal('0.00'),
        )
        entry.clean()  # Must not raise

    def test_credit_only_entry_is_valid(self):
        """An entry with only credit > 0 is valid."""
        acct = self._make_account('TST-C01', 'إيرادات اختبار', 'revenue')
        entry = AccountingEntry(
            reference='TEST-002',
            description='قيد دائن',
            account=acct,
            debit=Decimal('0.00'),
            credit=Decimal('200.00'),
        )
        entry.clean()  # Must not raise

    def test_both_debit_and_credit_raises(self):
        """An entry cannot have both debit > 0 and credit > 0."""
        acct = self._make_account('TST-M01', 'حساب مختلط', 'asset')
        entry = AccountingEntry(
            reference='TEST-003',
            description='قيد مختلط خاطئ',
            account=acct,
            debit=Decimal('50.00'),
            credit=Decimal('50.00'),
        )
        with self.assertRaises(ValidationError):
            entry.clean()

    def test_zero_debit_and_zero_credit_raises(self):
        """An entry cannot have both debit = 0 and credit = 0."""
        acct = self._make_account('TST-Z01', 'حساب صفري', 'asset')
        entry = AccountingEntry(
            reference='TEST-004',
            description='قيد صفري خاطئ',
            account=acct,
            debit=Decimal('0.00'),
            credit=Decimal('0.00'),
        )
        with self.assertRaises(ValidationError):
            entry.clean()

    def test_validate_balanced_passes_for_balanced_entries(self):
        """validate_balanced() must pass when debit total == credit total."""
        cash_acct = self._make_account('TST-B01', 'خزينة', 'asset')
        rev_acct = self._make_account('TST-B02', 'إيرادات', 'revenue')
        ref = 'BALANCED-001'
        AccountingEntry.objects.create(
            reference=ref, description='مدين', account=cash_acct,
            debit=Decimal('500.00'), credit=Decimal('0.00'),
        )
        AccountingEntry.objects.create(
            reference=ref, description='دائن', account=rev_acct,
            debit=Decimal('0.00'), credit=Decimal('500.00'),
        )
        result = AccountingEntry.validate_balanced(ref)
        self.assertTrue(result)

    def test_validate_balanced_raises_for_unbalanced_entries(self):
        """validate_balanced() must raise when entries are not balanced."""
        acct = self._make_account('TST-U01', 'خزينة اختبار', 'asset')
        ref = 'UNBALANCED-001'
        AccountingEntry.objects.create(
            reference=ref, description='مدين وحيد', account=acct,
            debit=Decimal('300.00'), credit=Decimal('0.00'),
        )
        with self.assertRaises(ValidationError):
            AccountingEntry.validate_balanced(ref)


# ──────────────────────────────────────────────────────────────────────────────
# Commission double-entry (invoice_service.py fix verification)
# ──────────────────────────────────────────────────────────────────────────────
class CommissionLedgerTests(ERPTenantTestCase):
    """Commission journal entries must be balanced and validated by clean()."""

    def setUp(self):
        from inventory.models import EmployeeProfile
        from django.contrib.auth.models import User

        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.product = make_product(
            part_number='SVC-001', retail_price='500.00', average_cost='200.00',
        )
        make_inventory(self.product, self.branch, quantity=10)

        user = User.objects.create_user('tech_user_test', password='x')
        self.tech_profile = EmployeeProfile.objects.create(
            user=user,
            role='tech',
            branch=self.branch,
            commission_balance=Decimal('0.00'),
        )

    def _get_commission_entries(self, invoice):
        prefix = f"COMM-INV{invoice.pk}-EMP{self.tech_profile.pk}"
        return AccountingEntry.objects.filter(reference=prefix)

    def test_commission_entries_created_individually_not_bulk(self):
        """Commission entries must go through clean() (not bulk_create)."""
        from inventory.models import ServiceCatalog, SaleInvoiceServiceItem

        service = ServiceCatalog.objects.create(
            name='تغيير زيت',
            price=Decimal('200.00'),
            tech_commission_percent=Decimal('10.00'),
            estimated_hours=Decimal('1.00'),
        )
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 1, '500.00')],
            paid_amount='700.00',
        )
        SaleInvoiceServiceItem.objects.create(
            invoice=si,
            service=service,
            technician=self.tech_profile,
            price=Decimal('200.00'),
            actual_hours=Decimal('0.50'),
        )

        si.status = 'posted'
        si.save()

        # Commission entries must exist and be balanced
        entries = self._get_commission_entries(si)
        self.assertEqual(entries.count(), 2, "Expected exactly 2 commission journal entries")

        total_debit = sum(e.debit for e in entries)
        total_credit = sum(e.credit for e in entries)
        self.assertEqual(total_debit, total_credit, "Commission entries must be balanced")

    def test_commission_entries_pass_clean_validation(self):
        """Each commission entry individually must satisfy clean() rules."""
        from inventory.models import ServiceCatalog, SaleInvoiceServiceItem

        service = ServiceCatalog.objects.create(
            name='خدمة اختبار',
            price=Decimal('100.00'),
            tech_commission_percent=Decimal('5.00'),
            estimated_hours=Decimal('2.00'),
        )
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 1, '500.00')],
            paid_amount='600.00',
        )
        SaleInvoiceServiceItem.objects.create(
            invoice=si,
            service=service,
            technician=self.tech_profile,
            price=Decimal('100.00'),
            actual_hours=Decimal('2.00'),
        )

        si.status = 'posted'
        si.save()

        entries = self._get_commission_entries(si)
        for entry in entries:
            # Each entry should have exactly one side set
            has_debit = entry.debit > 0
            has_credit = entry.credit > 0
            self.assertNotEqual(
                has_debit, has_credit,
                f"Entry {entry.pk} must be single-sided (debit XOR credit)"
            )
            self.assertFalse(
                has_debit and has_credit,
                "Entry cannot have both debit and credit set"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Double-entry integrity for financial transactions
# ──────────────────────────────────────────────────────────────────────────────
class DoubleEntryIntegrityTests(ERPTenantTestCase):
    """Every FinancialTransaction must produce balanced journal entries."""

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='10000.00')

    def _assert_balanced(self, txn):
        entries = AccountingEntry.objects.filter(financial_transaction=txn)
        self.assertEqual(entries.count(), 2, "Expected exactly 2 accounting entries per transaction")
        total_debit = sum(e.debit for e in entries)
        total_credit = sum(e.credit for e in entries)
        self.assertEqual(
            total_debit, total_credit,
            f"Entries unbalanced: debit={total_debit} credit={total_credit}"
        )
        return total_debit

    def test_income_transaction_balanced(self):
        """IN transaction: cash debit == revenue credit."""
        txn = make_financial_transaction(self.treasury, '1500.00', txn_type='in')
        amount = self._assert_balanced(txn)
        self.assertEqual(amount, Decimal('1500.00'))

    def test_expense_transaction_balanced(self):
        """OUT transaction: expense debit == cash credit."""
        cat = make_expense_category()
        txn = make_financial_transaction(
            self.treasury, '750.00', txn_type='out', category=cat,
        )
        amount = self._assert_balanced(txn)
        self.assertEqual(amount, Decimal('750.00'))

    def test_entry_types_correct_for_income(self):
        """Cash account should be debited on income; revenue account credited."""
        txn = make_financial_transaction(self.treasury, '2000.00', txn_type='in')
        entries = AccountingEntry.objects.filter(financial_transaction=txn)
        debit_entry = entries.get(debit__gt=0)
        credit_entry = entries.get(credit__gt=0)
        self.assertEqual(debit_entry.account.account_type, 'asset')
        self.assertEqual(credit_entry.account.account_type, 'revenue')

    def test_entry_types_correct_for_expense(self):
        """Expense account should be debited on payment; cash account credited."""
        cat = make_expense_category()
        txn = make_financial_transaction(
            self.treasury, '300.00', txn_type='out', category=cat,
        )
        entries = AccountingEntry.objects.filter(financial_transaction=txn)
        debit_entry = entries.get(debit__gt=0)
        credit_entry = entries.get(credit__gt=0)
        self.assertIn(debit_entry.account.account_type, ('expense',))
        self.assertEqual(credit_entry.account.account_type, 'asset')


# ──────────────────────────────────────────────────────────────────────────────
# Inventory conservation
# ──────────────────────────────────────────────────────────────────────────────
class InventoryConservationTests(ERPTenantTestCase):
    """Inventory quantity must never go negative after a sale."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.product = make_product(
            part_number='INV-CON-001', retail_price='100.00', average_cost='50.00',
        )

    def test_sale_exceeding_stock_is_rejected(self):
        """Posting a sale for more units than in stock must raise an error."""
        make_inventory(self.product, self.branch, quantity=3)

        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 10, '100.00')],
            paid_amount='1000.00',
        )
        with self.assertRaises((ValidationError, Exception)):
            si.status = 'posted'
            si.save()

        # Inventory must be unchanged
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity, 3)

    def test_exact_stock_sale_reduces_to_zero(self):
        """Selling exactly available stock should reduce inventory to zero."""
        make_inventory(self.product, self.branch, quantity=5)
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '100.00')],
            paid_amount='500.00',
        )
        si.status = 'posted'
        si.save()

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity, 0)

    def test_return_restores_inventory(self):
        """Posting a return invoice should add back stock."""
        from inventory.services.invoice_service import InvoiceService

        make_inventory(self.product, self.branch, quantity=10)
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 4, '100.00')],
            paid_amount='400.00',
        )
        si.status = 'posted'
        si.save()

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity, 6)  # 10 - 4

        return_inv = InvoiceService.create_return_invoice(si)
        return_inv.status = 'posted'
        return_inv.save()

        inv.refresh_from_db()
        self.assertEqual(inv.quantity, 10)  # 6 + 4 returned


# ──────────────────────────────────────────────────────────────────────────────
# EscrowHold conservation
# ──────────────────────────────────────────────────────────────────────────────
class EscrowHoldConservationTests(ERPTenantTestCase):
    """EscrowHold disbursements must never exceed held_amount."""

    def _make_escrow_hold(self, held, seller, buyer, commission, status='held'):
        from clients.models import EscrowHold, PartOrder, PartListing, MarketplaceCustomer
        from django.contrib.auth.models import User

        user = User.objects.create_user(
            f'mkt_{PartListing.objects.count()}', password='x'
        )
        mkt_customer = MarketplaceCustomer.objects.create(
            user=user,
            phone='01000000001',
            display_name='مشتري اختبار',
        )
        listing = PartListing.objects.create(
            seller=mkt_customer,
            title='قطعة اختبار',
            price=Decimal(str(held)),
            category='engine',
            condition='used',
            status='available',
        )
        order = PartOrder.objects.create(
            listing=listing,
            buyer=mkt_customer,
            amount=Decimal(str(held)),
            order_code=f'ORD-{PartOrder.objects.count() + 1:06d}',
            status='paid_held',
        )
        return EscrowHold.objects.create(
            order=order,
            status=status,
            held_amount=Decimal(str(held)),
            seller_payout_amount=Decimal(str(seller)),
            buyer_refund_amount=Decimal(str(buyer)),
            platform_commission_amount=Decimal(str(commission)),
        )

    def test_valid_conservation_is_accepted(self):
        """EscrowHold where disbursements <= held_amount should save."""
        hold = self._make_escrow_hold(
            held=1000, seller=900, buyer=0, commission=100,
        )
        self.assertIsNotNone(hold.pk)

    def test_conservation_violation_raises_integrity_error(self):
        """EscrowHold where disbursements > held_amount must be rejected by DB."""
        from django.db import IntegrityError

        with self.assertRaises((IntegrityError, Exception)):
            self._make_escrow_hold(
                held=1000, seller=900, buyer=200, commission=100,
                # 900 + 200 + 100 = 1200 > 1000 — violates constraint
            )

    def test_negative_amount_raises_integrity_error(self):
        """Negative disbursement amounts must be rejected by DB constraint."""
        from django.db import IntegrityError

        with self.assertRaises((IntegrityError, Exception)):
            self._make_escrow_hold(
                held=1000, seller=-100, buyer=0, commission=0,
            )
