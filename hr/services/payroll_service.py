"""
Service: Automated Payroll Engine

Responsibilities:
- Generate monthly payroll run for all active employees
- Calculate deductions: lateness, absence, advance installments
- Calculate additions: bonuses, overtime
- Link payroll disbursement to Treasury (FinancialTransaction / PrintTransaction)
- Update advance installment status after deduction
"""

import logging
from decimal import Decimal
from datetime import date

from django.db import transaction, connection
from django.db.models import F
from django.core.exceptions import ValidationError
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


class PayrollService:
    """All payroll operations go through here."""

    # ------------------------------------------------------------------
    # Generate Payroll Run — calculate all employees' salaries
    # ------------------------------------------------------------------
    @staticmethod
    def generate_payroll(month, year, created_by=None):
        """
        Generate payroll for a given month/year.
        Creates PayrollRun + PayrollEntry for each active employee.
        Automatically calculates deductions from attendance and advances.
        Returns the PayrollRun instance.
        """
        from hr.models import (
            Employee, PayrollRun, PayrollEntry,
            HRSettings, AdvanceInstallment,
        )
        from hr.services.attendance_service import AttendanceService

        # Guard: don't regenerate
        # 🐛 [FIX]: «إعادة الحساب» كانت ميتة — بعد أول حساب الدورة بتبقى «محسوب»
        #    والحارس كان بيرفض أي حالة غير «مسودة»، فتصحيح حضور أو مرتب بعد
        #    الحساب مالوش طريق. دلوقتي المحسوبة (قبل الاعتماد) بتتعاد عادي.
        existing = PayrollRun.objects.filter(
            period_month=month, period_year=year
        ).first()
        if existing and existing.status not in ('draft', 'calculated'):
            raise ValidationError(
                f"دورة رواتب {month}/{year} موجودة بالفعل بحالة: {existing.get_status_display()}"
            )

        hr_settings = HRSettings.get_settings()
        due_month_date = date(year, month, 1)

        with transaction.atomic():
            if existing is not None:
                # 🐛 [FIX]: مسح الكشوف القديمة كان بيسيب أقساطها «تم الخصم» من غير
                #    كشف (SET_NULL) — فالقسط مايتخصمش تاني والسلفة تتسامح لوحدها.
                #    بنرجّع أقساط الدورة لحالتها قبل الحساب الأول.
                PayrollService.release_installments(
                    PayrollEntry.objects.filter(payroll_run=existing), due_month_date)
            # Create or reset payroll run
            payroll_run, _ = PayrollRun.objects.update_or_create(
                period_month=month,
                period_year=year,
                defaults={
                    'status': 'draft',
                    'created_by': created_by,
                    'total_gross': Decimal('0.00'),
                    'total_deductions': Decimal('0.00'),
                    'total_net': Decimal('0.00'),
                    'total_employees': 0,
                },
            )

            # Delete old entries if re-generating draft
            PayrollEntry.objects.filter(payroll_run=payroll_run).delete()

            employees = Employee.objects.filter(is_active=True)
            total_gross = Decimal('0.00')
            total_deductions = Decimal('0.00')
            total_net = Decimal('0.00')
            count = 0

            for emp in employees:
                entry = PayrollService._calculate_employee_entry(
                    payroll_run, emp, month, year, hr_settings, due_month_date,
                )
                if entry:
                    total_gross += entry.base_salary
                    total_deductions += entry.total_deductions
                    total_net += entry.net_salary
                    count += 1

            payroll_run.total_gross = total_gross
            payroll_run.total_deductions = total_deductions
            payroll_run.total_net = total_net
            payroll_run.total_employees = count
            payroll_run.status = 'calculated'
            payroll_run.save()

        logger.info(
            "[PAYROLL] Generated payroll %s/%s: %s employees, gross=%s, deductions=%s, net=%s",
            month, year, count, total_gross, total_deductions, total_net,
        )

        return payroll_run

    # ------------------------------------------------------------------
    # Calculate single employee entry
    # ------------------------------------------------------------------
    @staticmethod
    def _calculate_employee_entry(payroll_run, employee, month, year, hr_settings, due_month_date):
        """Build and save a PayrollEntry for one employee."""
        from hr.models import PayrollEntry, AdvanceInstallment
        from hr.services.attendance_service import AttendanceService

        if employee.base_salary <= 0:
            return None

        # --- Attendance summary ---
        att = AttendanceService.get_monthly_summary(employee, month, year)
        daily_rate = employee.get_daily_rate()

        # --- Late deduction ---
        late_deduction = Decimal('0.00')
        if att['days_late'] > 0:
            if hr_settings.late_deduction_per_minute > 0:
                # Per-minute deduction mode
                late_deduction = (
                    Decimal(str(att['total_late_minutes']))
                    * hr_settings.late_deduction_per_minute
                )
            elif hr_settings.late_deduction_percentage > 0:
                # Percentage-of-daily mode
                late_deduction = (
                    daily_rate
                    * hr_settings.late_deduction_percentage / Decimal('100')
                    * Decimal(str(att['days_late']))
                )

        # --- Absence deduction ---
        absence_deduction = Decimal('0.00')
        if att['days_absent'] > 0:
            absence_deduction = (
                daily_rate
                * hr_settings.absence_deduction_days
                * Decimal(str(att['days_absent']))
            )

        # --- Unpaid leave («بدون راتب») — أيامها بتتخصم زي الغياب بالظبط ---
        # 🐛 [FIX]: كانت بتتسجّل «إجازة» عادية فالموظف بياخد مرتبها كامل.
        from hr.services.leave_service import unpaid_leave_days
        unpaid_days = unpaid_leave_days(employee, month, year)
        unpaid_deduction = (daily_rate * Decimal(str(unpaid_days))) if unpaid_days else Decimal('0.00')

        # --- Overtime pay (1.5x hourly rate) ---
        overtime_pay = Decimal('0.00')
        if att['total_overtime_hours'] > 0 and daily_rate > 0:
            hourly = daily_rate / Decimal('8')  # Assume 8-hour workday
            overtime_pay = (
                hourly * Decimal('1.5') * Decimal(str(att['total_overtime_hours']))
            ).quantize(Decimal('0.01'))

        # --- Advance installments due this month (+ ones carried from earlier) ---
        # 🐛 [FIX]: لو الخصومات أكبر من المرتب كان الصافي بيتقفل على صفر لكن
        #    القسط كله بيتعلّم «اتخصم» ويتقيّد سداد — يعني السلفة بتتسامح لوحدها
        #    (موظف واخد سلفة وغاب الشهر كله = سلفته اتقفلت). دلوقتي القسط بيتخصم
        #    في حدود الباقي من المرتب، والباقي منه بيترحّل «مؤجل» للشهر الجاي.
        available = max(
            employee.base_salary + overtime_pay
            - late_deduction.quantize(Decimal('0.01'))
            - absence_deduction.quantize(Decimal('0.01'))
            - unpaid_deduction.quantize(Decimal('0.01')),
            Decimal('0.00'),
        )
        from django.db.models import Q
        due_installments = list(AdvanceInstallment.objects.select_for_update().filter(
            Q(status='scheduled', due_month__year=year, due_month__month=month)
            | Q(status='skipped', due_month__lt=due_month_date),
            advance__employee=employee,
            advance__status__in=['approved', 'active'],
        ).order_by('due_month', 'installment_number'))
        advance_deduction = sum((i.amount for i in due_installments), Decimal('0.00'))
        advance_deduction = min(advance_deduction, available)

        # --- Build entry ---
        entry = PayrollEntry(
            payroll_run=payroll_run,
            employee=employee,
            base_salary=employee.base_salary,
            days_present=att['days_present'],
            days_absent=att['days_absent'],
            days_late=att['days_late'],
            days_excused=att['days_excused'],
            total_late_minutes=att['total_late_minutes'],
            total_worked_hours=Decimal(str(att['total_worked_hours'])),
            late_deduction=late_deduction.quantize(Decimal('0.01')),
            absence_deduction=absence_deduction.quantize(Decimal('0.01')),
            advance_deduction=advance_deduction.quantize(Decimal('0.01')),
            other_deductions=unpaid_deduction.quantize(Decimal('0.01')),
            other_deductions_note=(f"إجازة بدون راتب ({unpaid_days} يوم)" if unpaid_days else ''),
            overtime_pay=overtime_pay,
        )
        entry.calculate_totals()
        entry.save()

        # --- Mark installments as deducted (or carried over) ---
        PayrollService._allocate_installments(entry, due_installments, advance_deduction)

        return entry

    # ------------------------------------------------------------------
    # Internal: deduct installments up to what the payslip can cover
    # ------------------------------------------------------------------
    @staticmethod
    def _allocate_installments(entry, installments, covered):
        """يعلّم الأقساط «اتخصم» في حدود المبلغ المغطّى من المرتب.

        القسط اللي اتغطّى جزء منه بيتقسم: الجزء المخصوم يفضل على القسط،
        والباقي قسط جديد «مؤجل» بنفس شهر الاستحقاق. اللي ماتغطّاش خالص بيبقى
        «مؤجل». كل الأقساط بتتربط بالكشف عشان إعادة الحساب ترجّعها زي ما كانت.
        """
        from hr.models import AdvanceInstallment
        left = covered
        for inst in installments:
            if left >= inst.amount:
                inst.status = 'deducted'
                left -= inst.amount
            elif left > 0:
                last = (AdvanceInstallment.objects.filter(advance_id=inst.advance_id)
                        .order_by('-installment_number').values_list('installment_number', flat=True).first())
                AdvanceInstallment.objects.create(
                    advance_id=inst.advance_id, installment_number=(last or 0) + 1,
                    amount=inst.amount - left, due_month=inst.due_month,
                    status='skipped', deducted_in_payroll=entry,
                )
                inst.amount = left
                inst.status = 'deducted'
                left = Decimal('0.00')
            else:
                inst.status = 'skipped'
            inst.deducted_in_payroll = entry
            inst.save(update_fields=['amount', 'status', 'deducted_in_payroll'])

    @staticmethod
    def release_installments(entries, due_month_date):
        """يرجّع أقساط كشوف هتتمسح (إعادة حساب / حذف دورة) لحالتها قبل الحساب.

        قسط مستحق في شهر الدورة يرجع «مجدول»، وقسط مترحّل من شهر قبلها يرجع
        «مؤجل» عشان الدورة الجاية تلقطه. مجموع الأقساط مابيتغيّرش (القسط
        المتقسّم بيفضل جزئين بنفس المجموع).
        """
        from hr.models import AdvanceInstallment
        linked = AdvanceInstallment.objects.filter(deducted_in_payroll__in=entries)
        linked.filter(due_month__gte=due_month_date).update(status='scheduled', deducted_in_payroll=None)
        linked.filter(due_month__lt=due_month_date).update(status='skipped', deducted_in_payroll=None)

    # ------------------------------------------------------------------
    # Approve Payroll Run
    # ------------------------------------------------------------------
    @staticmethod
    def approve_payroll(payroll_run_id, approved_by=None):
        """Mark payroll as approved — ready for disbursement."""
        from hr.models import PayrollRun

        with transaction.atomic():
            run = PayrollRun.objects.select_for_update().get(pk=payroll_run_id)
            if run.status != 'calculated':
                raise ValidationError(
                    f"لا يمكن اعتماد دورة رواتب بحالة: {run.get_status_display()}"
                )
            run.status = 'approved'
            run.approved_by = approved_by
            run.save(update_fields=['status', 'approved_by'])

        logger.info("[PAYROLL] Approved payroll run #%s by user %s", run.pk, approved_by)
        return run

    # ------------------------------------------------------------------
    # Disburse Payroll — create treasury transactions
    # ------------------------------------------------------------------
    @staticmethod
    def disburse_payroll(payroll_run_id):
        """
        Create treasury transactions for each employee's net salary.
        Detects industry (automotive vs printing) and uses the correct model.
        Updates advance statuses to 'completed' where fully paid.
        """
        from hr.models import PayrollRun, Advance

        with transaction.atomic():
            run = PayrollRun.objects.select_for_update().get(pk=payroll_run_id)
            if run.status != 'approved':
                raise ValidationError(
                    f"يجب اعتماد الدورة أولاً. الحالة الحالية: {run.get_status_display()}"
                )

            entries = run.entries.select_related('employee__user').all()
            industry = PayrollService._detect_industry()

            for entry in entries:
                if industry != 'printing':
                    PayrollService._post_advance_recovery(entry, run)
                if entry.net_salary <= 0:
                    continue

                tx_id = PayrollService._create_treasury_transaction(
                    industry, entry.employee, entry.net_salary,
                    f"راتب {run.period_month}/{run.period_year} — "
                    f"{entry.employee.user.get_full_name() or entry.employee.user.username}",
                )
                entry.treasury_transaction_id = tx_id
                entry.save(update_fields=['treasury_transaction_id'])

            # --- Update fully-paid advances ---
            for entry in entries:
                active_advances = Advance.objects.filter(
                    employee=entry.employee,
                    status__in=['approved', 'active'],
                )
                for adv in active_advances:
                    # Mark as active if first installment was deducted
                    if adv.status == 'approved':
                        adv.status = 'active'

                    # 🐛 [FIX]: القسط «المؤجل» (مترحّل لأن المرتب ماغطّاهوش) لسه
                    #    مستحق — من غيره السلفة كانت تتقفل «اتسدّدت» وهي لسه عليها.
                    pending = adv.installments.filter(status__in=['scheduled', 'skipped']).count()
                    if pending == 0:
                        adv.status = 'completed'
                        adv.remaining_amount = Decimal('0.00')
                    else:
                        deducted_total = sum(
                            i.amount for i in adv.installments.filter(status='deducted')
                        )
                        adv.remaining_amount = adv.amount - deducted_total

                    adv.save(update_fields=['status', 'remaining_amount'])

            run.status = 'paid'
            run.paid_at = timezone.now()
            run.save(update_fields=['status', 'paid_at'])

        logger.info(
            "[PAYROLL] Disbursed payroll #%s: %s entries, total net=%s",
            run.pk, entries.count(), run.total_net,
        )
        return run

    # ------------------------------------------------------------------
    # Internal: advance installments recovered through this payslip
    # ------------------------------------------------------------------
    @staticmethod
    def _post_advance_recovery(entry, run):
        """قسط السلفة اللي اتخصم من المرتب = جزء من المرتب اتدفع مقدّماً.

        السلفة اتقيّدت وقت صرفها على «سلف الموظفين» (أصل)، فالقسط بيتقيّد:
        مدين رواتب وأجور / دائن سلف الموظفين — فمصروف المرتب يبقى كامل
        والأصل يقفل. السلف القديمة اللي ماتصرفتش من النظام مالهاش قيد صرف
        فمابنقفلهاش (عشان الأصل مايطلعش بالسالب).
        """
        from django.db.models import Sum
        from inventory.models import ExpenseCategory
        from inventory.services.accounting_service import AccountingService
        recovered = (entry.advance_installments_deducted
                     .filter(status='deducted', advance__disbursed_at__isnull=False)
                     .aggregate(s=Sum('amount'))['s'] or Decimal('0.00'))
        if recovered <= 0:
            return None
        category = (ExpenseCategory.objects.filter(system_key='salaries').order_by('id').first()
                    or ExpenseCategory.objects.get_or_create(
                        name='رواتب وأجور', defaults={'system_key': 'salaries'})[0])
        name = entry.employee.user.get_full_name() or entry.employee.user.username
        return AccountingService.post_journal(
            description=f"خصم قسط سلفة من راتب {run.period_month}/{run.period_year} — {name}",
            lines=[
                {'account': AccountingService.category_account(category), 'debit': recovered, 'credit': 0},
                {'account': 'employee_advances', 'debit': 0, 'credit': recovered},
            ],
            journal_type='general',
            reference=f"PAYROLL-{run.pk}-ADV-{entry.pk}",
        )

    # ------------------------------------------------------------------
    # Internal: Detect industry (automotive vs printing)
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_industry():
        """Detect tenant industry from available models."""
        schema = connection.schema_name
        if schema == 'public':
            return 'automotive'  # fallback

        try:
            from clients.models import Client
            tenant = Client.objects.filter(schema_name=schema).first()
            if tenant:
                return tenant.industry
        except Exception:
            pass

        return 'automotive'

    # ------------------------------------------------------------------
    # Internal: Create treasury transaction per industry
    # ------------------------------------------------------------------
    @staticmethod
    def _create_treasury_transaction(industry, employee, amount, description):
        """
        Create an outgoing financial transaction for salary payment.
        Returns the transaction PK.
        Uses FinancialTransaction for automotive, PrintTransaction for printing.
        """
        if industry == 'printing':
            from printing.models import PrintTreasury, PrintTransaction
            treasury = PrintTreasury.objects.filter(is_active=True).first()
            if not treasury:
                logger.error("[PAYROLL] No active PrintTreasury found for salary disbursement")
                return None
            tx = PrintTransaction.objects.create(
                treasury=treasury,
                transaction_type='out',
                amount=amount,
                description=description,
            )
            return tx.pk
        else:
            from inventory.models import ExpenseCategory, FinancialTransaction, Treasury
            profile = getattr(employee.user, 'employee_profile', None)
            # 🐛 [FIX]: كان بياخد أول خزنة في الشركة (أي فرع، أي نوع) من غير ما
            #    يتأكد إن فيها رصيد — فالخزنة بتنزل بالسالب — والقيد بيروح
            #    «مصروفات عمومية» من غير بند الرواتب. دلوقتي: خزنة الفرع النقدية
            #    (أو أي خزنة نشطة فيها رصيد يكفي)، وبند «رواتب وأجور».
            active = Treasury.objects.select_for_update().filter(is_active=True)
            branch_id = getattr(profile, 'branch_id', None)
            candidates = ([active.filter(branch_id=branch_id, type='cash')] if branch_id else []) + [
                active.filter(type='cash'), active]
            treasury = None
            for qs in candidates:
                treasury = qs.filter(balance__gte=amount).order_by('-balance', 'id').first()
                if treasury is not None:
                    break
            if treasury is None:
                if not active.exists():
                    logger.error("[PAYROLL] No active Treasury found for salary disbursement")
                    return None
                raise ValidationError(
                    f"مفيش خزنة فيها رصيد كافٍ لصرف {amount} — {description}. "
                    "اشحن الخزنة الأول (مافيش أي راتب اتصرف).")
            category = (ExpenseCategory.objects.filter(system_key='salaries').order_by('id').first()
                        or ExpenseCategory.objects.get_or_create(
                            name='رواتب وأجور', defaults={'system_key': 'salaries'})[0])
            tx = FinancialTransaction.objects.create(
                treasury=treasury,
                transaction_type='out',
                amount=amount,
                description=description,
                category=category,
                employee=profile,
            )
            return tx.pk
