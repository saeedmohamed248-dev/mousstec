"""
Commission Payout Tests — DMS Backlog #5
=========================================
Locks down the commission_dashboard view + TreasuryService.pay_commissions
service against the regressions we already saw during the audit:

  • Treasury picked from wrong branch
  • FinancialTransaction missing employee FK (broken audit trail)
  • Role gate accepting non-admin/manager
  • Double-click / re-pay drains the same balance twice
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.messages.middleware import MessageMiddleware

from inventory.models import EmployeeProfile, FinancialTransaction
from inventory.services.treasury_service import TreasuryService
from inventory.views import commission_dashboard

from .base import ERPTenantTestCase
from .factories import make_branch, make_treasury, make_employee


def _build_request(user, tenant, method='get', data=None):
    """Build a fully-middleware-wired request — view-level testing."""
    rf = RequestFactory()
    if method == 'post':
        req = rf.post('/system/commissions/', data or {})
    else:
        req = rf.get('/system/commissions/')
    SessionMiddleware(lambda r: None).process_request(req)
    req.session.save()
    AuthenticationMiddleware(lambda r: None).process_request(req)
    MessageMiddleware(lambda r: None).process_request(req)
    req.user = user
    req.tenant = tenant
    return req


class CommissionPayoutServiceTests(ERPTenantTestCase):
    """TreasuryService.pay_commissions — pure service contract."""

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.mgr_user, self.mgr_profile = make_employee(
            'mgr_svc', role='manager', branch=self.branch,
        )
        self.tech1_user, self.tech1 = make_employee(
            'tech1_svc', role='tech', branch=self.branch, commission_balance='150.00',
        )
        self.tech2_user, self.tech2 = make_employee(
            'tech2_svc', role='tech', branch=self.branch, commission_balance='250.00',
        )

    # ── happy path ────────────────────────────────────────────────────
    def test_paying_two_techs_zeros_balance_and_debits_treasury(self):
        result = TreasuryService.pay_commissions(
            EmployeeProfile.objects.filter(pk__in=[self.tech1.pk, self.tech2.pk]),
            treasury=self.treasury, paid_by_user=self.mgr_user,
        )
        self.assertEqual(result['paid_count'], 2)
        self.assertEqual(result['total_paid'], Decimal('400.00'))

        self.tech1.refresh_from_db()
        self.tech2.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual(self.tech1.commission_balance, Decimal('0.00'))
        self.assertEqual(self.tech2.commission_balance, Decimal('0.00'))
        self.assertEqual(self.treasury.balance, Decimal('4600.00'))

    def test_financial_transactions_linked_to_employee_fk(self):
        """Audit trail: every payout tx must carry the employee FK."""
        TreasuryService.pay_commissions(
            EmployeeProfile.objects.filter(pk__in=[self.tech1.pk, self.tech2.pk]),
            treasury=self.treasury, paid_by_user=self.mgr_user,
        )
        txs = FinancialTransaction.objects.filter(
            treasury=self.treasury, transaction_type='out',
            employee__in=[self.tech1, self.tech2],
        )
        self.assertEqual(txs.count(), 2)
        self.assertEqual(
            sum(tx.amount for tx in txs), Decimal('400.00'),
        )
        # Every tx must mention who approved it
        for tx in txs:
            self.assertIn(self.mgr_user.username, tx.description)

    # ── validation failures ───────────────────────────────────────────
    def test_inactive_treasury_rejected(self):
        self.treasury.is_active = False
        self.treasury.save()
        with self.assertRaises(ValidationError):
            TreasuryService.pay_commissions(
                EmployeeProfile.objects.filter(pk=self.tech1.pk),
                treasury=self.treasury,
            )

    def test_empty_queryset_rejected(self):
        with self.assertRaises(ValidationError):
            TreasuryService.pay_commissions(
                EmployeeProfile.objects.none(),
                treasury=self.treasury,
            )

    def test_re_paying_zero_balance_rejected(self):
        """Idempotent: 2nd run on zero-balance employees raises (no double-pay)."""
        TreasuryService.pay_commissions(
            EmployeeProfile.objects.filter(pk__in=[self.tech1.pk, self.tech2.pk]),
            treasury=self.treasury, paid_by_user=self.mgr_user,
        )
        with self.assertRaises(ValidationError):
            TreasuryService.pay_commissions(
                EmployeeProfile.objects.filter(pk__in=[self.tech1.pk, self.tech2.pk]),
                treasury=self.treasury, paid_by_user=self.mgr_user,
            )

    def test_role_filter_skips_employee_outside_allowed_roles(self):
        """allowed_roles={'tech'} skips a salesperson even if they're selected."""
        sp_user, sp = make_employee(
            'sp_svc', role='sales', branch=self.branch, commission_balance='99.00',
        )
        result = TreasuryService.pay_commissions(
            EmployeeProfile.objects.filter(pk__in=[self.tech1.pk, sp.pk]),
            treasury=self.treasury, paid_by_user=self.mgr_user,
            allowed_roles={'tech'},
        )
        sp.refresh_from_db()
        self.assertEqual(result['paid_count'], 1)
        self.assertEqual(sp.commission_balance, Decimal('99.00'))  # untouched


class CommissionDashboardViewTests(ERPTenantTestCase):
    """commission_dashboard view — HTTP-layer behavior."""

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.mgr_user, _ = make_employee('mgr_view', role='manager', branch=self.branch)
        self.cashier_user, _ = make_employee('cashier_view', role='cashier', branch=self.branch)
        _, self.tech_profile = make_employee(
            'tech_view', role='tech', branch=self.branch, commission_balance='100.00',
        )

    # ── role gate ────────────────────────────────────────────────────
    def test_manager_can_open(self):
        req = _build_request(self.mgr_user, self.tenant, method='get')
        r = commission_dashboard(req)
        self.assertEqual(r.status_code, 200)

    def test_cashier_forbidden(self):
        """Issue #1 regression guard: non-admin/manager gets HTML 403, NOT JSON."""
        req = _build_request(self.cashier_user, self.tenant, method='get')
        r = commission_dashboard(req)
        self.assertEqual(r.status_code, 403)
        # role_required for browser nav must return rendered HTML, not JSON
        self.assertNotIn(b'application/json', r['Content-Type'].encode())
        self.assertIn(b'<!DOCTYPE html>', r.content[:50])

    # ── POST validation ──────────────────────────────────────────────
    def test_post_without_treasury_redirects_with_error(self):
        req = _build_request(self.mgr_user, self.tenant, method='post',
                             data={'employee_ids': [str(self.tech_profile.pk)]})
        r = commission_dashboard(req)
        self.assertEqual(r.status_code, 302)
        # Balance untouched
        self.tech_profile.refresh_from_db()
        self.assertEqual(self.tech_profile.commission_balance, Decimal('100.00'))

    def test_post_without_employees_redirects_with_error(self):
        req = _build_request(self.mgr_user, self.tenant, method='post',
                             data={'treasury_id': str(self.treasury.pk)})
        r = commission_dashboard(req)
        self.assertEqual(r.status_code, 302)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, Decimal('5000.00'))

    # ── happy path through HTTP layer ────────────────────────────────
    def test_post_valid_settles_and_redirects(self):
        req = _build_request(self.mgr_user, self.tenant, method='post', data={
            'treasury_id': str(self.treasury.pk),
            'employee_ids': [str(self.tech_profile.pk)],
        })
        r = commission_dashboard(req)
        self.assertEqual(r.status_code, 302)
        self.tech_profile.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual(self.tech_profile.commission_balance, Decimal('0.00'))
        self.assertEqual(self.treasury.balance, Decimal('4900.00'))
