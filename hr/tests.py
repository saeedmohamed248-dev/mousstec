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
from datetime import date, timedelta, timezone as dt_timezone
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

    def test_designer_cannot_self_activate_ai_without_paying(self):
        """🐛 «فيزا» متمثّلة (payment_success = True) و«محفظة» من غير أي دفع كانوا
        بيفعّلوا اشتراك AI متسجّل «مدفوع» برقم عملية وهمي."""
        from django.core.exceptions import ValidationError
        from hr.services.ai_subscription_service import AISubscriptionService
        self.client.force_login(self.user)
        for method in ('visa', 'wallet'):
            r = self.client.post('/hr/designer/', {'action': 'activate_ai', 'plan': 'pro',
                                                   'payment_method': method, 'card_token': 'x'},
                                 HTTP_HOST=self.host)
            self.assertEqual(r.status_code, 302)
        self.assertFalse(AIDesignSubscription.objects.exists())
        with self.assertRaises(ValidationError):
            AISubscriptionService.process_visa_payment(self.emp, 'pro', 'tok')
        self.assertNotIn('aidesignsubscription/add', self._get_dashboard().content.decode())   # لينك 404
        # الإدارة لسه بتفعّل بعد الدفع
        self.assertEqual(AISubscriptionService.admin_activate(self.emp, 'pro', self.user).status, 'active')


class _HRMoneyBase(_HRTestBase):
    """شركة فيها خزنة برأس مال 20000 وموظف مرتبه 6000 وأدمن."""

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


class HRMoneyCycleTests(_HRMoneyBase):
    """سلفة: طلب ← موافقة ← صرف من الخزنة ← خصم من المرتب (الدفتر يقفل)،
    وإجازة: موافقة بعد ما الغياب اتسجّل ← تبقى إجازة، و«بدون راتب» بتتخصم."""

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


class AttendanceGateLockoutTests(_HRTestBase):
    """🐛 فني/مهندس مضاف من شاشة الموظفين (من غير ملف HR) كان بيلف في
    redirect لا نهائي (البوابة ← صفحة الحضور ← الرئيسية ← البوابة)."""

    def setUp(self):
        super().setUp()
        from django.test import Client
        HRSettings.objects.get_or_create(pk=1)
        self.user = self._make_user('gate_tech')
        self.user.employee_profile.role = 'tech'
        self.user.employee_profile.save()
        self.c = Client()
        self.c.force_login(self.user)

    def test_tech_without_hr_record_is_not_locked_out(self):
        r = self.c.get('/system/tech-workspace/', HTTP_HOST=self.host)
        self.assertNotIn('/hr/attendance/', r.get('Location', ''))
        self.assertEqual(r.status_code, 200)
        Employee.objects.create(user=self.user, department='workshop', base_salary=Decimal('0'),
                                is_active=False)
        r = self.c.get('/system/tech-workspace/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)       # ملف HR موقوف = مايقدرش يسجّل حضور

    def test_tech_with_hr_record_is_gated_until_clock_in(self):
        Employee.objects.create(user=self.user, department='workshop', base_salary=Decimal('0'))
        r = self.c.get('/system/tech-workspace/', HTTP_HOST=self.host)
        self.assertIn('/hr/attendance/', r['Location'])
        self.assertEqual(self.c.get(r['Location'], HTTP_HOST=self.host).status_code, 200)
        # 🐛 الـ API نفسه كان بيتقفل بالبوابة (redirect) — الفني عمره ما يسجّل حضور
        r = self.c.post('/hr/api/clock-in/', data='{"face_verified": false}',
                        content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(self.c.get('/system/tech-workspace/', HTTP_HOST=self.host).status_code, 200)
        r = self.c.post('/hr/api/clock-out/', data='{}', content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertIn('/hr/attendance/', self.c.get('/system/tech-workspace/', HTTP_HOST=self.host)['Location'])

    def test_attendance_page_script_numbers_are_not_localized(self):
        """🐛 إحداثيات الورشة كانت بتتطبع «30,0444196» بالعربي جوه الـ JS ⇒ SyntaxError
        وزرار «تسجيل حضور» ميت."""
        from django.utils import translation
        Employee.objects.create(user=self.user, department='workshop', base_salary=Decimal('0'))
        HRSettings.objects.update(geofence_latitude=Decimal('30.0444196'), face_match_threshold=Decimal('0.45'))
        for lang in ('ar', 'en'):
            with translation.override(lang):
                html = self.c.get('/hr/attendance/', HTTP_HOST=self.host, HTTP_ACCEPT_LANGUAGE=lang).content.decode()
            self.assertIn('lat: 30.0444196,', html, lang)
            self.assertIn('faceThreshold: 0.45,', html, lang)

    def test_overnight_shift_stays_open_after_midnight_and_clocks_out(self):
        from unittest import mock
        from hr.models import AttendanceRecord
        emp = Employee.objects.create(user=self.user, department='workshop', base_salary=Decimal('0'))
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(timezone.datetime(2026, 3, 10, 22, 0), tz).astimezone(dt_timezone.utc)
        after_midnight = timezone.make_aware(timezone.datetime(2026, 3, 11, 3, 30), tz).astimezone(
            dt_timezone.utc)
        with mock.patch('django.utils.timezone.now', return_value=start):
            rec = AttendanceService.clock_in(emp)
        self.assertEqual(rec.date, timezone.datetime(2026, 3, 10).date())
        with mock.patch('django.utils.timezone.now', return_value=after_midnight):
            self.assertEqual(AttendanceService.open_record(emp), rec)
            out = AttendanceService.clock_out(emp)
        self.assertEqual(out.pk, rec.pk)
        self.assertEqual(out.worked_hours, Decimal('5.50'))
        self.assertEqual(AttendanceRecord.objects.filter(employee=emp).count(), 1)

    def test_clock_in_after_midnight_is_dated_with_the_local_day(self):
        from unittest import mock
        emp = Employee.objects.create(user=self.user, department='workshop', base_salary=Decimal('0'))
        tz = timezone.get_current_timezone()
        # timezone.now() الحقيقي بيرجّع UTC: ١ الفجر بتوقيت القاهرة = ١٠ بالليل ١ يوليو UTC
        local_1am = timezone.make_aware(timezone.datetime(2026, 7, 2, 1, 0), tz).astimezone(
            dt_timezone.utc)
        self.assertEqual(local_1am.date(), timezone.datetime(2026, 7, 1).date())
        with mock.patch('django.utils.timezone.now', return_value=local_1am):
            rec = AttendanceService.clock_in(emp)
        self.assertEqual(rec.date, timezone.datetime(2026, 7, 2).date())


class WorkshopAttendanceSyncTests(_HRMoneyBase):
    """🐛 زراير الحضور/الانصراف في ورشة الميكانيكي وشاشة الفني (GPS) كانت بتكتب في
    جداولها بس — والمرتب بيتحسب من سجل الحضور (HR): الفني ينصرف من الورشة وسجل
    المرتب يفضل مفتوح، فساعات الشغل والإضافي بصفر."""

    def setUp(self):
        super().setUp()
        from django.test import Client
        self.user.employee_profile.role = 'tech'
        self.user.employee_profile.save()
        HRSettings.objects.update(geofence_latitude=Decimal('30.1000000'),
                                  geofence_longitude=Decimal('31.3000000'), geofence_radius_meters=200)
        self.c = Client()
        self.c.force_login(self.user)

    def _clock_in_hours_ago(self, hours):
        from unittest import mock
        with mock.patch('django.utils.timezone.now', return_value=timezone.now() - timedelta(hours=hours)):
            return AttendanceService.clock_in(self.emp)

    def _post(self, url, payload=None):
        import json as _json
        return self.c.post(url, data=_json.dumps(payload or {}), content_type='application/json',
                           HTTP_HOST=self.host)

    def test_mechanic_bay_clock_out_closes_payroll_record_and_pays_overtime(self):
        shift = WorkShift.objects.create(name='صباحي', start_time='09:00', end_time='17:00',
                                         days_of_week=['sat', 'sun', 'mon', 'tue', 'wed', 'thu', 'fri'])
        EmployeeShiftAssignment.objects.create(employee=self.emp, shift=shift,
                                               effective_from=date.today() - timedelta(days=30))
        rec = self._clock_in_hours_ago(10)          # حضر من صفحة الحضور (البوابة)
        r = self._post('/system/api/v1/mechanic/shift/clock_out/')
        self.assertEqual(r.status_code, 200, r.content[:200])   # كان «لا توجد وردية مفتوحة»
        rec.refresh_from_db()
        self.assertIsNotNone(rec.clock_out)
        self.assertAlmostEqual(rec.worked_hours, Decimal('10.00'), delta=Decimal('0.02'))
        self.assertAlmostEqual(rec.overtime_hours, Decimal('2.00'), delta=Decimal('0.02'))
        self.assertAlmostEqual(r.json()['total_hours'], 10.0, delta=0.02)

        from hr.services.payroll_service import PayrollService
        run = PayrollService.generate_payroll(rec.date.month, rec.date.year, created_by=self.boss)
        entry = run.entries.get(employee=self.emp)
        hourly = self.emp.get_daily_rate() / Decimal('8')
        self.assertEqual(entry.overtime_pay,
                         (hourly * Decimal('1.5') * rec.overtime_hours).quantize(Decimal('0.01')))
        self.assertGreater(entry.overtime_pay, Decimal('0'))
        # خلّص ورديته ⇒ البوابة بترجّعه لصفحة الحضور
        self.assertIn('/hr/attendance/', self.c.get('/system/tech-workspace/', HTTP_HOST=self.host)['Location'])

    def test_mechanic_bay_shift_writes_both_records(self):
        from hr.models import AttendanceRecord
        from inventory.models import EmployeeShift
        r = self._post('/system/api/v1/mechanic/shift/clock_in/')
        self.assertEqual(r.status_code, 200, r.content[:200])   # البوابة كانت بتحوّله (302)
        self.assertEqual(r.json()['hr_warning'], '')
        self.assertTrue(EmployeeShift.objects.filter(employee=self.user.employee_profile,
                                                     clock_out__isnull=True).exists())
        rec = AttendanceService.open_record(self.emp)
        self.assertIsNotNone(rec)
        self.assertEqual(self.c.get('/system/tech-workspace/', HTTP_HOST=self.host).status_code, 200)
        r = self._post('/system/api/v1/mechanic/shift/clock_out/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(EmployeeShift.objects.filter(clock_out__isnull=True).exists())
        self.assertIsNone(AttendanceService.open_record(self.emp))
        self.assertEqual(AttendanceRecord.objects.filter(employee=self.emp).count(), 1)

    def test_gps_check_in_and_out_from_tech_workspace_reach_payroll_record(self):
        from inventory.models import AttendanceCheckIn
        r = self._post('/system/api/attendance/checkin/', {'event_type': 'in', 'lat': '30.1001', 'lng': '31.3001'})
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual((r.json()['hr_recorded'], r.json()['hr_warning']), (True, ''))
        rec = AttendanceService.open_record(self.emp)
        self.assertTrue(rec.location_verified)
        self.assertEqual(self.c.get('/system/tech-workspace/', HTTP_HOST=self.host).status_code, 200)
        r = self._post('/system/api/attendance/checkin/', {'event_type': 'out', 'lat': '30.1001', 'lng': '31.3001'})
        self.assertTrue(r.json()['hr_recorded'])
        rec.refresh_from_db()
        self.assertIsNotNone(rec.clock_out)
        self.assertEqual(AttendanceCheckIn.objects.filter(employee=self.user.employee_profile).count(), 2)

    def test_gps_check_in_outside_geofence_is_logged_but_not_counted_for_payroll(self):
        from inventory.models import AttendanceCheckIn
        r = self._post('/system/api/attendance/checkin/', {'event_type': 'in', 'lat': '30.15', 'lng': '31.3'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['hr_recorded'])
        self.assertIn('خارج نطاق', r.json()['hr_warning'])
        self.assertFalse(AttendanceCheckIn.objects.latest('id').is_inside_geofence)   # الحركة متسجّلة للـ HR يراجعها
        self.assertIsNone(AttendanceService.open_record(self.emp))
        self.assertIn('/hr/attendance/', self.c.get('/system/tech-workspace/', HTTP_HOST=self.host)['Location'])

    def test_tech_without_hr_record_keeps_the_workshop_shift_only(self):
        from django.test import Client
        from hr.models import AttendanceRecord
        from inventory.models import EmployeeShift
        u = self._make_user('bay_only')
        u.employee_profile.role = 'tech'
        u.employee_profile.save()
        c = Client()
        c.force_login(u)
        for action in ('clock_in', 'clock_out'):
            r = c.post(f'/system/api/v1/mechanic/shift/{action}/', HTTP_HOST=self.host)
            self.assertEqual(r.status_code, 200, r.content[:200])
            self.assertEqual(r.json()['hr_warning'], '')
        self.assertEqual(EmployeeShift.objects.filter(employee=u.employee_profile).count(), 1)
        self.assertFalse(AttendanceRecord.objects.exists())
        r = c.post('/system/api/v1/mechanic/shift/clock_out/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 400)        # مفيش وردية ولا حضور مفتوح


class PayrollAdvanceCarryOverTests(_HRMoneyBase):
    """🐛 لو الخصومات أكبر من المرتب: القسط كان بيتعلّم «اتخصم» كله والصافي صفر
    — السلفة بتتسامح لوحدها. + «إعادة الحساب» كانت ميتة، ومسح الدورة كان بيضيّع
    الأقساط."""

    def _disbursed_advance(self, amount='1500', installments=3):
        from hr.services.advance_service import AdvanceService
        adv = AdvanceService.request_advance(self.emp, Decimal(amount), installments_count=installments)
        AdvanceService.approve_advance(adv.pk, None, by_user=self.boss)
        AdvanceService.disburse_advance(adv.pk, self.cash.pk, by_user=self.boss)
        adv.refresh_from_db()
        return adv

    def _absent(self, first_day, days):
        from hr.models import AttendanceRecord
        for i in range(days):
            AttendanceRecord.objects.create(employee=self.emp, date=first_day + timedelta(days=i),
                                            status='absent')

    def test_uncovered_installment_is_carried_not_forgiven(self):
        from hr.models import Advance
        from hr.services.payroll_service import PayrollService
        from inventory.services.books_audit import books_problems
        adv = self._disbursed_advance()
        first_due = adv.installments.order_by('due_month').first().due_month
        # مرتب 6000 ÷ 26 يوم = 230.77 — ٢٥ يوم غياب = 5769.25 ⇒ الباقي 230.75 بس
        self._absent(first_due, 25)
        run = PayrollService.generate_payroll(first_due.month, first_due.year, created_by=self.boss)
        entry = run.entries.get(employee=self.emp)
        self.assertEqual(entry.advance_deduction, Decimal('230.75'))
        self.assertEqual(entry.net_salary, Decimal('0.00'))
        self.assertEqual(sorted(adv.installments.values_list('status', 'amount')), sorted([
            ('deducted', Decimal('230.75')), ('skipped', Decimal('269.25')),
            ('scheduled', Decimal('500.00')), ('scheduled', Decimal('500.00'))]))
        PayrollService.approve_payroll(run.pk, approved_by=self.boss)
        PayrollService.disburse_payroll(run.pk)
        adv.refresh_from_db()
        self.assertEqual((adv.status, adv.remaining_amount), ('active', Decimal('1269.25')))
        self.assertEqual(self._acct('1150'), Decimal('1269.25'))      # الأصل = اللي لسه عليه
        self.assertEqual(books_problems(), [])
        # الشهر الجاي: القسط بتاعه + المترحّل
        nxt = (first_due.replace(day=28) + timedelta(days=4)).replace(day=1)
        run2 = PayrollService.generate_payroll(nxt.month, nxt.year, created_by=self.boss)
        entry2 = run2.entries.get(employee=self.emp)
        self.assertEqual(entry2.advance_deduction, Decimal('769.25'))
        self.assertEqual(entry2.net_salary, Decimal('5230.75'))
        PayrollService.approve_payroll(run2.pk, approved_by=self.boss)
        PayrollService.disburse_payroll(run2.pk)
        self.assertEqual(Advance.objects.get(pk=adv.pk).remaining_amount, Decimal('500.00'))
        self.assertEqual(self._acct('1150'), Decimal('500.00'))
        self.assertEqual(books_problems(), [])

    def test_calculated_payroll_can_be_recalculated_without_losing_installments(self):
        from hr.models import PayrollRun
        from hr.services.payroll_service import PayrollService
        adv = self._disbursed_advance()
        first_due = adv.installments.order_by('due_month').first().due_month
        self._absent(first_due, 25)
        run = PayrollService.generate_payroll(first_due.month, first_due.year, created_by=self.boss)
        self.assertEqual(run.entries.get().advance_deduction, Decimal('230.75'))
        # الغياب كان غلط ← اتمسح ← إعادة الحساب من الأدمن
        from hr.models import AttendanceRecord
        AttendanceRecord.objects.filter(employee=self.emp).delete()
        r = self.client_boss.post('/secure-portal/hr/payrollrun/', {
            'action': 'generate_payroll_action', '_selected_action': [run.pk]}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        entry = PayrollRun.objects.get(pk=run.pk).entries.get()
        self.assertEqual((entry.advance_deduction, entry.net_salary), (Decimal('500.00'), Decimal('5500.00')))
        self.assertEqual(sum(i.amount for i in adv.installments.filter(status='deducted')), Decimal('500.00'))
        self.assertEqual(sum(i.amount for i in adv.installments.all()), Decimal('1500.00'))
        self.assertFalse(adv.installments.filter(status='deducted', deducted_in_payroll__isnull=True).exists())

    def test_deleting_an_unpaid_run_returns_its_installments_and_paid_runs_stay(self):
        from hr.models import PayrollRun
        from hr.services.payroll_service import PayrollService
        adv = self._disbursed_advance()
        first_due = adv.installments.order_by('due_month').first().due_month
        run = PayrollService.generate_payroll(first_due.month, first_due.year, created_by=self.boss)
        self.assertTrue(adv.installments.filter(status='deducted').exists())
        r = self.client_boss.post(f'/secure-portal/hr/payrollrun/{run.pk}/delete/', {'post': 'yes'},
                                  HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(PayrollRun.objects.filter(pk=run.pk).exists())
        self.assertEqual(set(adv.installments.values_list('status', flat=True)), {'scheduled'})
        run = PayrollService.generate_payroll(first_due.month, first_due.year, created_by=self.boss)
        PayrollService.approve_payroll(run.pk, approved_by=self.boss)
        PayrollService.disburse_payroll(run.pk)
        r = self.client_boss.post(f'/secure-portal/hr/payrollrun/{run.pk}/delete/', {'post': 'yes'},
                                  HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(PayrollRun.objects.filter(pk=run.pk).exists())
