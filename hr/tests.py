"""
HR Regression Tests
===================
Smoke tests that lock in the bugs fixed during the 2026-06-11 HR review:

1. AttendanceService.clock_in() must not raise FieldError from the old broken
   query (`models_Q_effective_to_null_or_gte`). Fix: hr/services/attendance_service.py
2. designer_dashboard template renders the designer's real name (was blank
   because template referenced `employee.full_name` instead of
   `employee.user.get_full_name`).
3. designer_dashboard AI subscription card correctly reflects an active
   subscription (was always "غير مشترك" because template referenced
   `ai_sub.is_active` instead of `ai_sub.is_active_and_valid`).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from inventory.tests.base import ERPTenantTestCase as _TenantTestCase

from hr.models import (
    AIDesignSubscription, Employee, EmployeeShiftAssignment,
    HRSettings, WorkShift,
)
from hr.services.attendance_service import AttendanceService

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class _HRTestBase(_TenantTestCase):
    """Re-pin the tenant schema before each test (TransactionTestCase resets it)
    and expose a tenant-aware HTTP client."""

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain

    def _make_user(self, username='tester', is_super=False):
        return User.objects.create_user(
            username=username, password='x',
            first_name='علي', last_name='محمد',
            is_superuser=is_super, is_staff=is_super,
        )


class AttendanceClockInRegressionTests(_HRTestBase):
    """Bug #1: clock_in() must not blow up on EmployeeShiftAssignment lookup."""

    def setUp(self):
        super().setUp()
        HRSettings.objects.get_or_create(pk=1)
        self.user = self._make_user('att_user')
        self.emp = Employee.objects.create(
            user=self.user, department='workshop',
            base_salary=Decimal('5000.00'),
        )

    def test_clock_in_succeeds_without_shift(self):
        """No EmployeeShiftAssignment at all → still works (status='present')."""
        rec = AttendanceService.clock_in(self.emp, face_verified=True)
        self.assertIsNotNone(rec.clock_in)
        self.assertEqual(rec.status, 'present')
        self.assertEqual(rec.late_minutes, 0)

    def test_clock_in_succeeds_with_shift(self):
        """With a shift assignment → query must use the Q expression, not the
        broken `models_Q_effective_to_null_or_gte` kwarg. Pre-fix this raised
        FieldError; this test just calling it without raising is the assertion."""
        shift = WorkShift.objects.create(
            name='صباحي',
            start_time='09:00', end_time='17:00',
            days_of_week=['sat', 'sun', 'mon', 'tue', 'wed', 'thu', 'fri'],
        )
        EmployeeShiftAssignment.objects.create(
            employee=self.emp, shift=shift,
            effective_from=date.today() - timedelta(days=30),
            # effective_to left NULL — this is the exact case the broken
            # query was supposed to handle via Q(effective_to__isnull=True).
        )
        rec = AttendanceService.clock_in(self.emp, face_verified=True)
        self.assertIsNotNone(rec.clock_in)
        self.assertEqual(rec.shift_id, shift.id)

    def test_double_clock_in_blocked(self):
        AttendanceService.clock_in(self.emp, face_verified=True)
        with self.assertRaises(Exception):
            AttendanceService.clock_in(self.emp, face_verified=True)


class DesignerDashboardTemplateTests(_HRTestBase):
    """Bugs #2 + #3: dashboard must show name + AI subscription state."""

    def setUp(self):
        super().setUp()
        HRSettings.objects.get_or_create(pk=1)
        self.user = self._make_user('puser', is_super=True)
        self.emp = Employee.objects.create(
            user=self.user, department='design',
            base_salary=Decimal('6000.00'),
        )

    def _get_dashboard(self):
        """Hit /hr/designer/ with the tenant host so TenantMainMiddleware routes
        the request into the right schema."""
        self.client.force_login(self.user)
        return self.client.get('/hr/designer/', HTTP_HOST=self.host)

    def test_dashboard_shows_designer_full_name(self):
        """Bug #2: template uses {{ employee.user.get_full_name }}, not the
        non-existent {{ employee.full_name }}."""
        resp = self._get_dashboard()
        self.assertEqual(resp.status_code, 200, f"Got {resp.status_code}; redirect chain may need inspection")
        body = resp.content.decode('utf-8')
        self.assertIn('علي محمد', body, "Designer name must appear on dashboard")
        self.assertIn('أهلاً، علي محمد', body)

    def test_ai_subscription_active_card_shown(self):
        """Bug #3: with a valid active subscription the card must render
        the usage counter using the real field names."""
        AIDesignSubscription.objects.create(
            designer=self.emp, plan='pro', status='active',
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=30),
            ai_generations_limit=300, ai_generations_used=42,
            payment_method='admin_manual', price_paid=Decimal('350'),
        )
        resp = self._get_dashboard()
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertNotIn('غير مشترك', body,
                         "Active subscription must NOT render the inactive card")
        self.assertIn('42', body)
        self.assertIn('300', body)

    def test_ai_subscription_inactive_card_shown(self):
        """With no subscription → 'غير مشترك' is the correct state."""
        resp = self._get_dashboard()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('غير مشترك', resp.content.decode('utf-8'))
