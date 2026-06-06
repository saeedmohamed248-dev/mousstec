"""
quick_expense_create Tests — DMS Backlog #1
============================================
Locks down the salary-employee FK requirement on quick_expense_create:

  • Posting a 'salaries' category expense WITHOUT an employee_id → 400
  • Posting a 'salaries' expense WITH a valid employee_id → 200 + tx.employee set
  • Non-salary categories ignore the employee_id requirement
  • Negative / zero amount rejected
  • Missing treasury rejected
  • Insufficient treasury balance → 409 (no debit)
"""
from decimal import Decimal
from django.test import RequestFactory
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware

from inventory.models import FinancialTransaction, ExpenseCategory
from inventory.views_lightning import quick_expense_create

from .base import ERPTenantTestCase
from .factories import make_branch, make_treasury, make_employee


def _wire(user, tenant, data):
    rf = RequestFactory()
    req = rf.post('/system/expense/create/', data)
    SessionMiddleware(lambda r: None).process_request(req)
    req.session.save()
    AuthenticationMiddleware(lambda r: None).process_request(req)
    req.user = user
    req.tenant = tenant
    return req


class QuickExpenseCreateTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        # admin user — quick_expense_create has no role gate but does need auth
        self.admin_user, _ = make_employee('exp_admin', role='admin', branch=self.branch)
        self.tech_user, self.tech_profile = make_employee(
            'exp_tech', role='tech', branch=self.branch,
        )

        # Categories
        self.salaries_cat = ExpenseCategory.objects.create(
            name='رواتب', system_key='salaries',
        )
        self.rent_cat = ExpenseCategory.objects.create(
            name='إيجار', system_key='rent',
        )

    # ── input validation ─────────────────────────────────────────────
    def test_zero_amount_rejected(self):
        req = _wire(self.admin_user, self.tenant, {
            'treasury_id': str(self.treasury.pk),
            'amount': '0',
            'description': 'test',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 400)
        # Error body is Arabic — just assert the JSON shape carries an `error` key
        self.assertIn(b'error', r.content)

    def test_missing_treasury_rejected(self):
        req = _wire(self.admin_user, self.tenant, {
            'amount': '100',
            'description': 'no treasury',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 400)

    def test_nonexistent_treasury_rejected(self):
        req = _wire(self.admin_user, self.tenant, {
            'treasury_id': '99999',
            'amount': '100',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 404)

    # ── insufficient balance ─────────────────────────────────────────
    def test_insufficient_balance_returns_409(self):
        req = _wire(self.admin_user, self.tenant, {
            'treasury_id': str(self.treasury.pk),
            'amount': '99999.00',
            'description': 'huge expense',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 409)
        # Treasury must NOT be debited
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('5000.00'))

    # ── salary employee FK requirement ───────────────────────────────
    def test_salary_category_without_employee_rejected(self):
        """The exact regression DMS Backlog #1 names: salary expense MUST
        require the employee_id, otherwise we lose the link between payroll
        outflow and the recipient."""
        req = _wire(self.admin_user, self.tenant, {
            'treasury_id': str(self.treasury.pk),
            'category_id': str(self.salaries_cat.pk),
            'amount': '1500.00',
            'description': 'مرتب',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 400)
        # Treasury must NOT be debited
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('5000.00'))

    def test_salary_category_with_valid_employee_succeeds(self):
        req = _wire(self.admin_user, self.tenant, {
            'treasury_id': str(self.treasury.pk),
            'category_id': str(self.salaries_cat.pk),
            'employee_id': str(self.tech_profile.pk),
            'amount': '1500.00',
            'description': 'مرتب يونيه',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 200)

        # Treasury debited
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('3500.00'))

        # FinancialTransaction created with employee FK + 'salaries' category
        tx = FinancialTransaction.objects.filter(treasury=self.treasury, transaction_type='out').last()
        self.assertEqual(tx.amount, Decimal('1500.00'))
        self.assertEqual(tx.employee_id, self.tech_profile.pk)
        self.assertEqual(tx.category_id, self.salaries_cat.pk)

    def test_salary_category_with_nonexistent_employee_rejected(self):
        req = _wire(self.admin_user, self.tenant, {
            'treasury_id': str(self.treasury.pk),
            'category_id': str(self.salaries_cat.pk),
            'employee_id': '99999',
            'amount': '1500.00',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 404)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('5000.00'))

    # ── non-salary category: employee not required ───────────────────
    def test_rent_category_without_employee_succeeds(self):
        """Non-salary categories must NOT require an employee_id."""
        req = _wire(self.admin_user, self.tenant, {
            'treasury_id': str(self.treasury.pk),
            'category_id': str(self.rent_cat.pk),
            'amount': '2000.00',
            'description': 'إيجار يونيه',
        })
        r = quick_expense_create(req)
        self.assertEqual(r.status_code, 200)

        tx = FinancialTransaction.objects.filter(treasury=self.treasury, transaction_type='out').last()
        self.assertEqual(tx.amount, Decimal('2000.00'))
        self.assertIsNone(tx.employee_id)
        self.assertEqual(tx.category_id, self.rent_cat.pk)
