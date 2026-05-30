"""
Reporting Service Tests — Ensure debt aging and slow-moving inventory work correctly.
"""
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta

from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_product, make_inventory, make_customer,
    make_treasury, make_sale_invoice,
)


class CustomerDebtAgingTests(ERPTenantTestCase):
    """Debt aging should bucket unpaid invoices by age."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer(name='عميل مدين')
        self.product = make_product(part_number='AGE-001', retail_price='100.00')
        make_inventory(self.product, self.branch, quantity=100)

    def test_no_debt_returns_empty(self):
        """Customer with 0 balance should not appear in aging."""
        from inventory.services.reporting_service import ReportingService
        result = ReportingService.customer_debt_aging()
        self.assertEqual(len(result), 0)

    def test_recent_debt_in_0_30_bucket(self):
        """A recently created unpaid invoice should fall in 0-30 bucket."""
        from inventory.services.reporting_service import ReportingService
        from inventory.models import SaleInvoice

        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product, 5, '100.00')],
            paid_amount='0.00',
        )
        si.status = 'posted'
        si.save()

        self.customer.refresh_from_db()
        result = ReportingService.customer_debt_aging()
        self.assertGreater(len(result), 0)
        entry = next(e for e in result if e['customer_id'] == self.customer.id)
        self.assertGreater(entry['0_30'], 0)

    def test_old_debt_in_90_plus_bucket(self):
        """An old unpaid invoice should fall in 90+ bucket."""
        from inventory.services.reporting_service import ReportingService
        from inventory.models import SaleInvoice

        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product, 3, '100.00')],
            paid_amount='0.00',
        )
        si.status = 'posted'
        si.save()

        # Backdate the invoice
        SaleInvoice.objects.filter(pk=si.pk).update(
            date_created=timezone.now() - timedelta(days=120),
        )

        self.customer.refresh_from_db()
        result = ReportingService.customer_debt_aging()
        entry = next(e for e in result if e['customer_id'] == self.customer.id)
        self.assertGreater(entry['90_plus'], 0)


class SlowMovingInventoryTests(ERPTenantTestCase):
    """Slow-moving inventory should detect unsold products."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.product_fast = make_product(
            part_number='FAST-001', retail_price='100.00',
        )
        self.product_slow = make_product(
            part_number='SLOW-001', retail_price='200.00',
        )
        make_inventory(self.product_fast, self.branch, quantity=20)
        make_inventory(self.product_slow, self.branch, quantity=15)

    def test_unsold_product_is_slow_moving(self):
        """A product with stock but no recent sales should appear."""
        from inventory.services.reporting_service import ReportingService

        # Sell the fast product (so it's NOT slow)
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch,
            items=[(self.product_fast, 1, '100.00')],
        )
        si.status = 'posted'
        si.save()

        result = ReportingService.slow_moving_inventory(days_threshold=60)
        slow_pns = [s['part_number'] for s in result]
        self.assertIn('SLOW-001', slow_pns)
        self.assertNotIn('FAST-001', slow_pns)

    def test_no_stock_not_slow_moving(self):
        """A product with 0 stock should NOT appear even if unsold."""
        from inventory.services.reporting_service import ReportingService

        make_inventory(self.product_slow, self.branch, quantity=0)
        result = ReportingService.slow_moving_inventory(days_threshold=60)
        slow_pns = [s['part_number'] for s in result]
        self.assertNotIn('SLOW-001', slow_pns)
