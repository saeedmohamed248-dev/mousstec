"""
Cross-branch stock lookup — a branch that is out of a part must be able to
see which OTHER branches hold it.
"""
import uuid

from inventory.services.reporting_service import ReportingService
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_inventory


class CrossBranchStockTests(ERPTenantTestCase):

    def setUp(self):
        # Lift the branch quota (0 = unlimited) so we can seed 3 branches.
        from django.db import connection
        connection.set_schema_to_public()
        self.tenant.max_branches = 0
        self.tenant.save(update_fields=['max_branches'])
        connection.set_tenant(self.tenant)

        self.branch_a = make_branch(name='فرع أ')
        self.branch_b = make_branch(name='فرع ب')
        self.branch_c = make_branch(name='فرع ج')
        self.part = make_product(part_number=f'CBS-{uuid.uuid4().hex[:6]}', name='طلمبة مياه')
        # Out at A, available at B (5) and C (2)
        make_inventory(self.part, self.branch_a, quantity=0)
        make_inventory(self.part, self.branch_b, quantity=5)
        make_inventory(self.part, self.branch_c, quantity=2)

    def test_finds_stock_in_other_branches(self):
        results = ReportingService.cross_branch_stock(
            query=self.part.part_number, requesting_branch=self.branch_a,
        )
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row['here_qty'], 0)
        self.assertEqual(row['other_qty'], 7)
        self.assertEqual(row['total_qty'], 7)
        # Only branches with stock are listed, sorted by quantity desc
        names = [b['branch_name'] for b in row['branches']]
        self.assertEqual(names, ['فرع ب', 'فرع ج'])
        self.assertFalse(any(b['is_here'] for b in row['branches']))

    def test_here_branch_flagged_and_excluded_from_other(self):
        results = ReportingService.cross_branch_stock(
            query=self.part.part_number, requesting_branch=self.branch_b,
        )
        row = results[0]
        self.assertEqual(row['here_qty'], 5)
        self.assertEqual(row['other_qty'], 2)
        here = [b for b in row['branches'] if b['is_here']]
        self.assertEqual(len(here), 1)
        self.assertEqual(here[0]['branch_name'], 'فرع ب')

    def test_lookup_by_product_id(self):
        results = ReportingService.cross_branch_stock(
            product_id=self.part.pk, requesting_branch=self.branch_a,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['total_qty'], 7)

    def test_short_query_returns_empty(self):
        self.assertEqual(ReportingService.cross_branch_stock(query='a'), [])

    def test_zero_stock_everywhere_lists_no_branches(self):
        dead = make_product(part_number=f'DEAD-{uuid.uuid4().hex[:6]}', name='قطعة نافدة')
        make_inventory(dead, self.branch_a, quantity=0)
        results = ReportingService.cross_branch_stock(
            product_id=dead.pk, requesting_branch=self.branch_a,
        )
        self.assertEqual(results[0]['total_qty'], 0)
        self.assertEqual(results[0]['branches'], [])
