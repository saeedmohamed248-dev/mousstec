"""
Sale Return Tests — Ensure return invoices work correctly:
treasury refund, customer credit, inventory restoration.
"""
from decimal import Decimal
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_product, make_inventory, make_customer,
    make_treasury, make_sale_invoice,
)


class SaleReturnTests(ERPTenantTestCase):
    """Return invoice lifecycle: creation, posting, financial effects."""

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='10000.00')
        self.customer = make_customer()
        self.product = make_product(
            part_number='RET-001', retail_price='200.00', average_cost='100.00',
        )
        make_inventory(self.product, self.branch, quantity=50)

        # Create and post an original invoice
        self.original = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 5, '200.00')],
            paid_amount='1000.00', status='quotation',
        )
        self.original.status = 'posted'
        self.original.save()  # triggers execute_sale

    def test_create_return_invoice(self):
        """Return invoice should be created with correct fields."""
        from inventory.services.invoice_service import InvoiceService
        ret = InvoiceService.create_return_invoice(self.original)

        self.assertTrue(ret.is_return)
        self.assertEqual(ret.original_invoice_id, self.original.id)
        self.assertEqual(ret.customer_id, self.customer.id)
        self.assertEqual(ret.status, 'quotation')
        self.assertEqual(ret.items.count(), 1)
        self.assertEqual(ret.items.first().quantity, 5)
        self.assertEqual(ret.total_amount, Decimal('1000.00'))
        self.assertEqual(ret.paid_amount, Decimal('1000.00'))

    def test_cannot_return_non_posted_invoice(self):
        """Cannot create return for draft invoice."""
        from inventory.services.invoice_service import InvoiceService
        from django.core.exceptions import ValidationError

        draft = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product, 1, '200.00')],
        )
        with self.assertRaises(ValidationError):
            InvoiceService.create_return_invoice(draft)

    def test_cannot_return_a_return(self):
        """Cannot create return from another return invoice."""
        from inventory.services.invoice_service import InvoiceService
        from django.core.exceptions import ValidationError

        ret = InvoiceService.create_return_invoice(self.original)
        ret.status = 'posted'
        ret.save()

        with self.assertRaises(ValidationError):
            InvoiceService.create_return_invoice(ret)

    def test_partial_return(self):
        """Partial return should only return specified items/qty."""
        from inventory.services.invoice_service import InvoiceService

        orig_item = self.original.items.first()
        ret = InvoiceService.create_return_invoice(
            self.original,
            return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )
        self.assertEqual(ret.items.first().quantity, 2)
        self.assertEqual(ret.total_amount, Decimal('400.00'))

    def test_return_exceeding_qty_raises(self):
        """Cannot return more than original quantity."""
        from inventory.services.invoice_service import InvoiceService
        from django.core.exceptions import ValidationError

        orig_item = self.original.items.first()
        with self.assertRaises(ValidationError):
            InvoiceService.create_return_invoice(
                self.original,
                return_items=[{'item_id': orig_item.pk, 'quantity': 999}],
            )

    def test_posted_return_refunds_treasury(self):
        """Posting a return should create an OUT transaction on the treasury."""
        from inventory.services.invoice_service import InvoiceService
        from inventory.models import FinancialTransaction, Treasury

        ret = InvoiceService.create_return_invoice(self.original)

        # Post the return
        ret.status = 'posted'
        ret.save()

        # Should have an OUT transaction for the return
        refund_txn = FinancialTransaction.objects.filter(
            sale_invoice=ret, transaction_type='out',
        ).first()
        self.assertIsNotNone(refund_txn)
        self.assertEqual(refund_txn.amount, Decimal('1000.00'))

    def test_posted_return_credits_customer_balance(self):
        """Posting a return should reduce customer balance."""
        from inventory.services.invoice_service import InvoiceService
        from inventory.models import Customer

        # Customer starts with 0 balance (fully paid original)
        self.customer.refresh_from_db()
        balance_before = self.customer.balance

        ret = InvoiceService.create_return_invoice(self.original)
        ret.status = 'posted'
        ret.save()

        self.customer.refresh_from_db()
        # Balance should decrease by total return amount
        self.assertEqual(
            self.customer.balance,
            balance_before - Decimal('1000.00'),
        )

    def test_posted_return_restores_inventory(self):
        """Posting a return should add items back to inventory."""
        from inventory.services.invoice_service import InvoiceService
        from inventory.models import Inventory

        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        qty_after_sale = inv.quantity  # Should be 45 (50 - 5)

        ret = InvoiceService.create_return_invoice(self.original)
        ret.status = 'posted'
        ret.save()

        inv.refresh_from_db()
        self.assertEqual(inv.quantity, qty_after_sale + 5)
