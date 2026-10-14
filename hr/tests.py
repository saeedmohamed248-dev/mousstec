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


class HRMoneyCycleTests(_HRTestBase):
    """سلفة: طلب ← موافقة ← صرف من الخزنة ← خصم من المرتب (الدفتر يقفل)،
    وإجازة: موافقة بعد ما الغياب اتسجّل ← تبقى إجازة، و«بدون راتب» بتتخصم."""

    def setUp(self):
        super().setUp()
        from inventory.models import Branch, FinancialTransaction, Treasury
        HRSettings.objects.get_or_create(pk=1)
        self.branch = Branch.objects.create(name='فرع HR')
        self.cash = Treasury.objects.create(name='كاش HR', branch=self.branch, balance=Decimal('0'))
        FinancialTransaction.objects.create(treasury=self.cash, transaction_type='in',
                                            amount=Decimal('20000'), description='رأس مال',
                                            equity_kind='capital')
        self.boss = self._make_user('hr_boss', is_super=True)
        self.user = self._make_user('hr_emp')
        self.user.employee_profile.branch = self.branch
        self.user.employee_profile.save()
        self.emp = Employee.objects.create(user=self.user, department='workshop',
                                           base_salary=Decimal('6000.00'))
        from django.test import Client
        self.client_boss = Client()
        self.client_boss.force_login(self.boss)

    @staticmethod
    def _acct(code):
        from django.db.models import Sum
        from inventory.models import AccountingEntry
        agg = AccountingEntry.objects.filter(account__code=code).aggregate(d=Sum('debit'), c=Sum('credit'))
        return (agg['d'] or Decimal('0')) - (agg['c'] or Decimal('0'))

    def test_advance_is_paid_out_then_recovered_from_salary(self):
        from hr.models import Advance, PayrollRun
        from hr.services.advance_service import AdvanceService
        from hr.services.payroll_service import PayrollService
        from inventory.models import FinancialTransaction
        from inventory.services.books_audit import books_problems
        adv = AdvanceService.request_advance(self.emp, Decimal('1500'), installments_count=3)
        r = self.client_boss.post(f'/hr/manager/advance/{adv.pk}/approve/',
                                  {'disburse': '1', 'treasury_id': self.cash.pk}, HTTP_HOST=self.host)
        self.assertIn('ok=advance_disbursed', r['Location'])
        adv.refresh_from_db()
        self.cash.refresh_from_db()
        self.assertEqual(adv.status, 'approved')
        self.assertIsNotNone(adv.disbursed_at)
        self.assertEqual(self.cash.balance, Decimal('18500.00'))
        self.assertEqual(self._acct('1150'), Decimal('1500.00'))   # أصل، مش مصروف
        self.assertEqual(books_problems(), [])
        # مش مصروف في شاشة المصاريف، ومايتحذفش من سجل الحركات
        r = self.client_boss.get('/system/expenses/', HTTP_HOST=self.host)
        self.assertEqual(r.context['total'], Decimal('0'))
        ft = FinancialTransaction.objects.get(pk=adv.disbursement_tx_id)
        r = self.client_boss.post(f'/system/transactions/{ft.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('err=linked', r['Location'])
        with self.assertRaises(Exception):
            AdvanceService.cancel_advance(adv.pk, None)
        with self.assertRaises(Exception):
            AdvanceService.disburse_advance(adv.pk)          # مرتين لأ

        first_due = adv.installments.order_by('due_month').first().due_month
        run = PayrollService.generate_payroll(first_due.month, first_due.year, created_by=self.boss)
        entry = run.entries.get(employee=self.emp)
        self.assertEqual(entry.advance_deduction, Decimal('500.00'))
        self.assertEqual(entry.net_salary, Decimal('5500.00'))
        PayrollService.approve_payroll(run.pk, approved_by=self.boss)
        PayrollService.disburse_payroll(run.pk)
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, Decimal('13000.00'))      # 18500 − 5500
        self.assertEqual(self._acct('1150'), Decimal('1000.00'))      # قسط اتسدّد
        from inventory.models import ExpenseCategory
        from inventory.services.accounting_service import AccountingService
        cat = ExpenseCategory.objects.filter(system_key='salaries').order_by('id').first()
        self.assertEqual(self._acct(AccountingService.category_account_code(cat)), Decimal('6000.00'))
        self.assertEqual(books_problems(), [])
        self.assertEqual(PayrollRun.objects.get(pk=run.pk).status, 'paid')
        # تقرير الأرباح والخسائر شايف المرتب كامل (5500 صرف + 500 قسط)
        r = self.client_boss.get('/system/reports/pnl/?period=all', HTTP_HOST=self.host)
        self.assertEqual(r.context['total_exp'], Decimal('6000.00'))
        self.assertEqual(Advance.objects.get(pk=adv.pk).status, 'active')

    def test_leave_approved_after_absence_turns_it_into_leave_and_unpaid_is_deducted(self):
        from hr.models import AttendanceRecord, LeaveRequest
        from hr.services.payroll_service import PayrollService
        today = timezone.localdate()
        d1 = today.replace(day=1)
        d2 = d1 + timedelta(days=1)
        for d in (d1, d2):
            AttendanceRecord.objects.create(employee=self.emp, date=d, status='absent')
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type='unpaid',
                                            from_date=d1, to_date=d2, status='pending')
        r = self.client_boss.post(f'/hr/manager/leave/{leave.pk}/approve/', HTTP_HOST=self.host)
        self.assertIn('ok=leave_approved', r['Location'])
        self.assertEqual(set(AttendanceRecord.objects.filter(employee=self.emp)
                             .values_list('status', flat=True)), {'excused'})
        run = PayrollService.generate_payroll(today.month, today.year, created_by=self.boss)
        entry = run.entries.get(employee=self.emp)
        self.assertEqual(entry.absence_deduction, Decimal('0.00'))
        self.assertEqual(entry.other_deductions, (self.emp.get_daily_rate() * 2).quantize(Decimal('0.01')))

    def test_non_hr_employee_cannot_review(self):
        from hr.models import LeaveRequest
        from django.test import Client
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type='annual',
                                            from_date=timezone.localdate(), to_date=timezone.localdate())
        c = Client()
        c.force_login(self.user)
        r = c.post(f'/hr/manager/leave/{leave.pk}/approve/', HTTP_HOST=self.host)
        self.assertIn('err=', r['Location'])
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'pending')

    def test_gps_checkin_is_checked_against_the_workshop_geofence(self):
        import json as _json
        from django.test import Client
        from inventory.models import AttendanceCheckIn
        c = Client()
        c.force_login(self.user)

        def checkin(lat, lng, accuracy=10):
            return c.post('/system/api/attendance/checkin/', data=_json.dumps(
                {'event_type': 'in', 'lat': lat, 'lng': lng, 'accuracy': accuracy}),
                content_type='application/json', HTTP_HOST=self.host)

        self.assertEqual(checkin('30.1', '31.3').status_code, 200)
        self.assertIn('مش متضبط', AttendanceCheckIn.objects.latest('id').flagged_reason)
        HRSettings.objects.update(geofence_latitude=Decimal('30.1000000'),
                                  geofence_longitude=Decimal('31.3000000'), geofence_radius_meters=200)
        checkin('30.1001', '31.3001')
        rec = AttendanceCheckIn.objects.latest('id')
        self.assertEqual((rec.is_inside_geofence, rec.flagged_reason), (True, ''))
        checkin('30.15', '31.3')                       # ~5.5 كم
        rec = AttendanceCheckIn.objects.latest('id')
        self.assertFalse(rec.is_inside_geofence)
        self.assertIn('خارج نطاق', rec.flagged_reason)
        checkin('30.1', '31.3', accuracy=900)
        self.assertIn('دقة GPS', AttendanceCheckIn.objects.latest('id').flagged_reason)
        self.assertEqual(checkin('abc', '31.3').status_code, 400)   # كان 500
        self.assertEqual(checkin('NaN', '31.3').status_code, 400)
        self.assertEqual(checkin('300', '31.3').status_code, 400)
        r = self.client_boss.get('/system/hr-workspace/?flagged=1', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context['records']), 3)
