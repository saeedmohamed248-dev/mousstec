"""
Stock Safety Tests — Inventory integrity, transfers, negative stock prevention.
These tests protect against stock corruption and phantom inventory.
"""
from decimal import Decimal
from django.core.exceptions import ValidationError
from .base import ERPTenantTestCase

from inventory.models import Inventory, StockTransfer, Product
from inventory.services.inventory_service import InventoryService
from .factories import (
    make_branch, make_product, make_inventory, make_customer,
    make_vendor, make_treasury, make_sale_invoice,
)


class NegativeStockPreventionTests(ERPTenantTestCase):
    """Stock must never go negative through any operation."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='50000.00')
        self.product = make_product(part_number='STOCK-001')
        make_inventory(self.product, self.branch, quantity=5)

    def test_sale_cannot_exceed_stock(self):
        """Selling more than available should fail."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            treasury=self.treasury,
            items=[(self.product, 10, '200.00')],  # only 5 in stock
            paid_amount='2000.00',
        )

        with self.assertRaises(Exception):
            si.status = 'posted'
            si.save()

        # Stock should be unchanged
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inv.quantity, 5)

    def test_db_constraint_prevents_negative(self):
        """Database CHECK constraint should prevent negative quantity."""
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        inv.quantity = -1

        with self.assertRaises(Exception):
            inv.save()

    def test_zero_stock_is_valid(self):
        """Zero stock is a valid state (not negative)."""
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        inv.quantity = 0
        inv.save()
        inv.refresh_from_db()
        self.assertEqual(inv.quantity, 0)


class StockTransferTests(ERPTenantTestCase):
    """Stock transfers must deduct from source and add to destination."""

    def setUp(self):
        self.branch_a = make_branch(name='فرع أ')
        self.branch_b = make_branch(name='فرع ب')
        self.product = make_product(part_number='XFER-001')
        make_inventory(self.product, self.branch_a, quantity=20)
        make_inventory(self.product, self.branch_b, quantity=5)

    def test_transfer_moves_stock(self):
        """Transfer should deduct from source and add to destination."""
        transfer = StockTransfer.objects.create(
            product=self.product,
            from_branch=self.branch_a,
            to_branch=self.branch_b,
            quantity=8,
            status='pending',
        )

        # Execute transfer
        transfer.status = 'in_transit'
        transfer.save()

        inv_a = Inventory.objects.get(product=self.product, branch=self.branch_a)
        inv_b = Inventory.objects.get(product=self.product, branch=self.branch_b)

        self.assertEqual(inv_a.quantity, 12)  # 20 - 8
        self.assertEqual(inv_b.quantity, 13)  # 5 + 8

    def test_transfer_conserves_total_stock(self):
        """Total stock across branches should remain constant after transfer."""
        total_before = sum(
            Inventory.objects.filter(product=self.product)
            .values_list('quantity', flat=True)
        )

        transfer = StockTransfer.objects.create(
            product=self.product,
            from_branch=self.branch_a,
            to_branch=self.branch_b,
            quantity=5,
            status='pending',
        )
        transfer.status = 'in_transit'
        transfer.save()

        total_after = sum(
            Inventory.objects.filter(product=self.product)
            .values_list('quantity', flat=True)
        )
        self.assertEqual(total_before, total_after)

    def test_transfer_insufficient_stock_fails(self):
        """Transfer exceeding source stock should fail."""
        transfer = StockTransfer.objects.create(
            product=self.product,
            from_branch=self.branch_a,
            to_branch=self.branch_b,
            quantity=100,  # only 20 in branch A
            status='pending',
        )

        with self.assertRaises(Exception):
            transfer.status = 'in_transit'
            transfer.save()

        # Stock should be unchanged
        inv_a = Inventory.objects.get(product=self.product, branch=self.branch_a)
        self.assertEqual(inv_a.quantity, 20)

    def test_cancel_transfer_reverses_stock(self):
        """Cancelling a transfer should return stock to source."""
        transfer = StockTransfer.objects.create(
            product=self.product,
            from_branch=self.branch_a,
            to_branch=self.branch_b,
            quantity=5,
            status='pending',
        )
        # Execute
        transfer.status = 'in_transit'
        transfer.save()

        inv_a = Inventory.objects.get(product=self.product, branch=self.branch_a)
        inv_b = Inventory.objects.get(product=self.product, branch=self.branch_b)
        self.assertEqual(inv_a.quantity, 15)
        self.assertEqual(inv_b.quantity, 10)

        # Cancel
        transfer.status = 'cancelled'
        transfer.save()

        inv_a.refresh_from_db()
        inv_b.refresh_from_db()
        self.assertEqual(inv_a.quantity, 20)
        self.assertEqual(inv_b.quantity, 5)


class WeightedAverageCostTests(ERPTenantTestCase):
    """Weighted average cost must be calculated correctly."""

    def setUp(self):
        self.branch = make_branch()
        self.vendor = make_vendor()
        self.treasury = make_treasury(self.branch, balance='100000.00')

    def test_first_purchase_sets_cost(self):
        """First purchase should set average cost to purchase price."""
        product = make_product(
            part_number='WAC-001', average_cost='0.00',
            purchase_price='0.00',
        )
        make_inventory(product, self.branch, quantity=0)

        from .factories import make_purchase_invoice
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[(product, 10, '150.00')],
            paid_amount='1500.00',
        )
        pi.status = 'posted'
        pi.save()

        product.refresh_from_db()
        self.assertEqual(product.average_cost, Decimal('150.00'))

    def test_second_purchase_recalculates_weighted_avg(self):
        """Second purchase at different price should recalculate weighted average."""
        product = make_product(
            part_number='WAC-002', average_cost='100.00',
            purchase_price='100.00',
        )
        make_inventory(product, self.branch, quantity=10)

        from .factories import make_purchase_invoice
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[(product, 10, '200.00')],
            paid_amount='2000.00',
        )
        pi.status = 'posted'
        pi.save()

        product.refresh_from_db()
        # (10*100 + 10*200) / 20 = 150
        self.assertEqual(product.average_cost, Decimal('150.00'))

    def test_multiple_products_independent_costs(self):
        """Each product's average cost should be calculated independently."""
        product_a = make_product(
            part_number='WAC-A', average_cost='50.00', purchase_price='50.00',
        )
        product_b = make_product(
            part_number='WAC-B', average_cost='80.00', purchase_price='80.00',
        )
        make_inventory(product_a, self.branch, quantity=5)
        make_inventory(product_b, self.branch, quantity=5)

        from .factories import make_purchase_invoice
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch, treasury=self.treasury,
            items=[
                (product_a, 5, '100.00'),
                (product_b, 5, '120.00'),
            ],
            paid_amount='1100.00',
        )
        pi.status = 'posted'
        pi.save()

        product_a.refresh_from_db()
        product_b.refresh_from_db()

        # product_a: (5*50 + 5*100) / 10 = 75
        self.assertEqual(product_a.average_cost, Decimal('75.00'))
        # product_b: (5*80 + 5*120) / 10 = 100
        self.assertEqual(product_b.average_cost, Decimal('100.00'))


class InventoryUniqueConstraintTests(ERPTenantTestCase):
    """Each product-branch combination must be unique."""

    def test_duplicate_product_branch_fails(self):
        """Cannot create two Inventory records for same product+branch."""
        branch = make_branch()
        product = make_product(part_number='UNIQ-001')

        Inventory.objects.create(product=product, branch=branch, quantity=10)

        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            Inventory.objects.create(product=product, branch=branch, quantity=5)

    def test_same_product_different_branch_ok(self):
        """Same product in different branches should be allowed."""
        branch_a = make_branch(name='فرع 1')
        branch_b = make_branch(name='فرع 2')
        product = make_product(part_number='UNIQ-002')

        inv_a = Inventory.objects.create(product=product, branch=branch_a, quantity=10)
        inv_b = Inventory.objects.create(product=product, branch=branch_b, quantity=5)

        self.assertEqual(Inventory.objects.filter(product=product).count(), 2)
        self.assertEqual(product.total_inventory_qty, 15)
