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
        """Posting a return on a fully-paid (cash) sale should refund the
        customer via the treasury, NOT credit their AR balance. The customer
        already paid in cash, so they get cash back — their AR balance must
        stay at zero. Crediting it would make them appear to owe us money
        we then refunded twice.
        """
        from inventory.services.invoice_service import InvoiceService

        # Customer starts with 0 balance (fully paid original)
        self.customer.refresh_from_db()
        balance_before = self.customer.balance

        ret = InvoiceService.create_return_invoice(self.original)
        ret.status = 'posted'
        ret.save()

        self.customer.refresh_from_db()
        # Balance should stay at 0 — the refund went through treasury (FT out),
        # not through AR. See test_posted_return_creates_refund_transaction
        # for the cash side of this flow.
        self.assertEqual(self.customer.balance, balance_before)

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

    # ------------------------------------------------------------------
    # 🔁 Multiple partial returns on the same invoice (per-line tracking)
    # ------------------------------------------------------------------
    def test_partial_return_sets_source_item(self):
        """Return lines must link back to the original line via source_item."""
        from inventory.services.invoice_service import InvoiceService

        orig_item = self.original.items.first()
        ret = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )
        self.assertEqual(ret.items.first().source_item_id, orig_item.pk)

    def test_returned_quantities_helper(self):
        """returned_quantities aggregates returned qty per original line."""
        from inventory.services.invoice_service import InvoiceService

        orig_item = self.original.items.first()
        r1 = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )
        r1.status = 'posted'
        r1.save()

        qty_map = InvoiceService.returned_quantities(self.original)
        self.assertEqual(qty_map.get(orig_item.pk), 2)

    def test_multiple_partial_returns_accumulate(self):
        """Two partial returns should sum, and remaining shrinks accordingly."""
        from inventory.services.invoice_service import InvoiceService

        orig_item = self.original.items.first()

        r1 = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )
        r1.status = 'posted'
        r1.save()

        r2 = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )
        r2.status = 'posted'
        r2.save()

        rows = InvoiceService.get_returnable_items(self.original)
        row = next(r for r in rows if r['item'].pk == orig_item.pk)
        self.assertEqual(row['returned'], 4)
        self.assertEqual(row['remaining'], 1)  # 5 - 4

    def test_over_return_across_returns_raises(self):
        """Total returned across returns cannot exceed the original quantity."""
        from inventory.services.invoice_service import InvoiceService
        from django.core.exceptions import ValidationError

        orig_item = self.original.items.first()
        r1 = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 4}],
        )
        r1.status = 'posted'
        r1.save()

        # Only 1 remaining — asking for 2 must raise.
        with self.assertRaises(ValidationError):
            InvoiceService.create_return_invoice(
                self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
            )

    def test_default_return_takes_only_remaining(self):
        """A default (unspecified) return after a partial one returns only the
        remaining quantity, never the full original again."""
        from inventory.services.invoice_service import InvoiceService

        orig_item = self.original.items.first()
        r1 = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )
        r1.status = 'posted'
        r1.save()

        r2 = InvoiceService.create_return_invoice(self.original)  # remaining = 3
        self.assertEqual(r2.items.first().quantity, 3)
        self.assertEqual(r2.total_amount, Decimal('600.00'))

    def test_fully_returned_invoice_rejects_further_return(self):
        """Once every line is fully returned, no further return can be made."""
        from inventory.services.invoice_service import InvoiceService
        from django.core.exceptions import ValidationError

        r1 = InvoiceService.create_return_invoice(self.original)  # full 5
        r1.status = 'posted'
        r1.save()

        with self.assertRaises(ValidationError):
            InvoiceService.create_return_invoice(self.original)

    def test_multi_return_cash_refund_bounded_by_paid(self):
        """Across several returns, total cash refunded never exceeds what the
        customer actually paid. Original paid 1000 (of 1000): two returns of
        400 and 600 refund 400 + 600 = 1000, no more."""
        from inventory.services.invoice_service import InvoiceService

        orig_item = self.original.items.first()  # 5 × 200 = 1000, paid 1000

        r1 = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )  # 400
        r1.status = 'posted'
        r1.save()
        self.assertEqual(r1.paid_amount, Decimal('400.00'))

        r2 = InvoiceService.create_return_invoice(
            self.original, return_items=[{'item_id': orig_item.pk, 'quantity': 3}],
        )  # 600
        r2.status = 'posted'
        r2.save()
        self.assertEqual(r2.paid_amount, Decimal('600.00'))

    def test_partial_return_carries_proportional_line_discount(self):
        """A line-level discount is carried proportionally into the return so
        the refund matches what the customer paid for that portion."""
        from inventory.models import SaleInvoice, SaleInvoiceItem
        from inventory.services.invoice_service import InvoiceService

        inv = SaleInvoice.objects.create(
            invoice_type='sale', customer=self.customer, branch=self.branch,
            treasury=self.treasury, status='quotation',
        )
        # 4 × 100 - 40 discount = 360 net
        SaleInvoiceItem.objects.create(
            invoice=inv, product=self.product, quantity=4,
            unit_price=Decimal('100.00'), discount=Decimal('40.00'),
            cost_at_sale=self.product.average_cost,
        )
        inv.update_total()
        inv.paid_amount = inv.total_amount
        inv.status = 'posted'
        inv.save()

        orig_item = inv.items.first()
        ret = InvoiceService.create_return_invoice(
            inv, return_items=[{'item_id': orig_item.pk, 'quantity': 2}],
        )
        ret_item = ret.items.first()
        # discount_share = 40 * 2 / 4 = 20 → line net = 2*100 - 20 = 180
        self.assertEqual(ret_item.discount, Decimal('20.00'))
        self.assertEqual(ret.total_amount, Decimal('180.00'))
