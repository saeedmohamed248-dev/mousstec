"""
Invoice Total Calculation Tests — Ensure correct totals, tax, discounts, profit.
"""
from decimal import Decimal
from .base import ERPTenantTestCase

from inventory.models import (
    SaleInvoice, SaleInvoiceItem, PurchaseInvoice, PurchaseInvoiceItem,
    ServiceCatalog, SaleInvoiceServiceItem,
)
from .factories import (
    make_branch, make_product, make_inventory, make_customer,
    make_vendor, make_treasury, make_sale_invoice, make_purchase_invoice,
)


class SaleInvoiceTotalTests(ERPTenantTestCase):
    """Sale invoice totals must correctly sum items, services, tax, discount."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.product_a = make_product(
            part_number='TOT-A', retail_price='200.00', average_cost='100.00',
        )
        self.product_b = make_product(
            part_number='TOT-B', retail_price='300.00', average_cost='150.00',
        )
        make_inventory(self.product_a, self.branch, quantity=50)
        make_inventory(self.product_b, self.branch, quantity=50)

    def test_single_item_total(self):
        """Single item: total = qty * price."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product_a, 3, '200.00')],
        )
        si.refresh_from_db()
        self.assertEqual(si.total_amount, Decimal('600.00'))

    def test_multiple_items_total(self):
        """Multiple items: total = sum(qty * price)."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[
                (self.product_a, 2, '200.00'),
                (self.product_b, 3, '300.00'),
            ],
        )
        si.refresh_from_db()
        # 2*200 + 3*300 = 400 + 900 = 1300
        self.assertEqual(si.total_amount, Decimal('1300.00'))

    def test_discount_applied(self):
        """Discount should reduce total."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product_a, 5, '200.00')],
        )
        si.discount = Decimal('100.00')
        si.save(update_fields=['discount'])
        si.update_total()
        si.refresh_from_db()
        # 5*200 - 100 = 900
        self.assertEqual(si.total_amount, Decimal('900.00'))

    def test_tax_applied(self):
        """Tax percentage should increase total."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product_a, 10, '100.00')],
        )
        si.tax_percentage = Decimal('14.00')  # 14% VAT
        si.save(update_fields=['tax_percentage'])
        si.update_total()
        si.refresh_from_db()
        # 10*100 = 1000, tax = 1000 * 14% = 140, total = 1140
        self.assertEqual(si.total_amount, Decimal('1140.00'))

    def test_profit_calculation(self):
        """Net profit should be revenue - cost - tax."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product_a, 5, '200.00')],  # cost=100 each
        )
        si.refresh_from_db()
        # Revenue: 5*200 = 1000, Cost: 5*100 = 500, Profit = 500
        self.assertEqual(si.net_profit, Decimal('500.00'))

    def test_due_amount_calculation(self):
        """Due amount should be total - paid."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product_a, 5, '200.00')],
            paid_amount='300.00',
        )
        si.refresh_from_db()
        # total=1000, paid=300, due=700
        self.assertEqual(si.due_amount, Decimal('700.00'))

    def test_fully_paid_invoice_zero_due(self):
        """Fully paid invoice should have zero due amount."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product_a, 5, '200.00')],
            paid_amount='1000.00',
        )
        si.refresh_from_db()
        self.assertEqual(si.due_amount, Decimal('0.00'))


class PurchaseInvoiceTotalTests(ERPTenantTestCase):
    """Purchase invoice totals must correctly sum items."""

    def setUp(self):
        self.branch = make_branch()
        self.vendor = make_vendor()
        self.product = make_product(part_number='PI-001')

    def test_purchase_total_calculation(self):
        """Purchase total = sum(qty * cost_price)."""
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch,
            items=[(self.product, 10, '150.00')],
        )
        pi.refresh_from_db()
        self.assertEqual(pi.total_amount, Decimal('1500.00'))

    def test_adding_item_updates_total(self):
        """Adding a new item should update the total via signal."""
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch,
            items=[(self.product, 5, '100.00')],
        )
        pi.refresh_from_db()
        self.assertEqual(pi.total_amount, Decimal('500.00'))

        product_b = make_product(part_number='PI-002')
        PurchaseInvoiceItem.objects.create(
            invoice=pi, product=product_b,
            quantity=3, cost_price=Decimal('200.00'),
        )
        pi.refresh_from_db()
        # 5*100 + 3*200 = 500 + 600 = 1100
        self.assertEqual(pi.total_amount, Decimal('1100.00'))

    def test_deleting_item_updates_total(self):
        """Deleting an item should update the total via signal."""
        product_b = make_product(part_number='PI-003')
        pi = make_purchase_invoice(
            vendor=self.vendor, branch=self.branch,
            items=[
                (self.product, 5, '100.00'),
                (product_b, 3, '200.00'),
            ],
        )
        pi.refresh_from_db()
        self.assertEqual(pi.total_amount, Decimal('1100.00'))

        # Delete second item
        PurchaseInvoiceItem.objects.filter(
            invoice=pi, product=product_b
        ).delete()
        pi.refresh_from_db()
        self.assertEqual(pi.total_amount, Decimal('500.00'))
