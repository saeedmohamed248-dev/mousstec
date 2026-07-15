"""
Company-wide per-branch overview — sales, expenses, treasury and attendance
must be attributed to the correct branch, with a correct totals row.
"""
import uuid
from decimal import Decimal

from django.contrib.auth.models import User

from inventory.models import (
    EmployeeProfile, AttendanceCheckIn, FinancialTransaction,
)
from inventory.services.reporting_service import ReportingService
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_product, make_inventory,
    make_treasury, make_sale_invoice,
)


class CompanyOverviewTests(ERPTenantTestCase):

    def setUp(self):
        from django.db import connection
        connection.set_schema_to_public()
        self.tenant.max_branches = 0
        self.tenant.max_users = 0
        self.tenant.max_treasuries = 0
        self.tenant.save(update_fields=['max_branches', 'max_users', 'max_treasuries'])
        connection.set_tenant(self.tenant)

        self.branch_a = make_branch(name='فرع أ')
        self.branch_b = make_branch(name='فرع ب')
        self.customer = make_customer()
        self.treasury_a = make_treasury(self.branch_a, balance='1000.00')
        self.treasury_b = make_treasury(self.branch_b, balance='500.00')
        self.product = make_product(part_number=f'CO-{uuid.uuid4().hex[:6]}', retail_price='100.00')
        make_inventory(self.product, self.branch_a, quantity=50)
        make_inventory(self.product, self.branch_b, quantity=50)

    def _make_employee(self, branch, name):
        # A post_save signal auto-creates the profile (branch=None); update it.
        u = User.objects.create_user(f'{name}_{uuid.uuid4().hex[:6]}', password='x')
        profile = EmployeeProfile.objects.get(user=u)
        profile.role = 'cashier'
        profile.branch = branch
        profile.save(update_fields=['role', 'branch'])
        return profile

    def test_sales_and_treasury_attributed_per_branch(self):
        # A posted sale of 3 units @100 in branch A, paid to treasury A.
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch_a, treasury=self.treasury_a,
            items=[(self.product, 3, '100.00')], paid_amount='300.00',
        )
        si.status = 'posted'
        si.save()
        overview = ReportingService.company_overview()
        rows = {r['branch_name']: r for r in overview['branches']}

        self.assertEqual(rows['فرع أ']['sales'], Decimal('300.00'))
        self.assertEqual(rows['فرع أ']['invoices_count'], 1)
        self.assertEqual(rows['فرع ب']['sales'], Decimal('0'))

        # Treasury A gained 300 from the sale (1000 + 300), B unchanged.
        self.assertEqual(rows['فرع أ']['treasury_balance'], Decimal('1300.00'))
        self.assertEqual(rows['فرع ب']['treasury_balance'], Decimal('500.00'))

        self.assertEqual(overview['totals']['sales'], Decimal('300.00'))

    def test_expenses_attributed_via_treasury_branch(self):
        FinancialTransaction.objects.create(
            treasury=self.treasury_b, transaction_type='out',
            amount=Decimal('120.00'), description='إيجار',
        )
        overview = ReportingService.company_overview()
        rows = {r['branch_name']: r for r in overview['branches']}
        self.assertEqual(rows['فرع ب']['expenses'], Decimal('120.00'))
        self.assertEqual(rows['فرع أ']['expenses'], Decimal('0'))
        self.assertEqual(overview['totals']['expenses'], Decimal('120.00'))

    def test_attendance_present_vs_absent(self):
        e1 = self._make_employee(self.branch_a, 'حاضر')
        e2 = self._make_employee(self.branch_a, 'غائب')
        e3 = self._make_employee(self.branch_b, 'خرج')

        # e1 checked in (present). e3 checked in then out (absent). e2 nothing.
        AttendanceCheckIn.objects.create(employee=e1, event_type='in', lat=0, lng=0)
        AttendanceCheckIn.objects.create(employee=e3, event_type='in', lat=0, lng=0)
        AttendanceCheckIn.objects.create(employee=e3, event_type='out', lat=0, lng=0)

        overview = ReportingService.company_overview()
        rows = {r['branch_name']: r for r in overview['branches']}

        self.assertEqual(rows['فرع أ']['present'], 1)
        self.assertEqual(rows['فرع أ']['absent'], 1)
        self.assertEqual(rows['فرع ب']['present'], 0)
        self.assertEqual(rows['فرع ب']['absent'], 1)
        self.assertEqual(overview['totals']['present'], 1)
        self.assertEqual(overview['totals']['employees_total'], 3)
