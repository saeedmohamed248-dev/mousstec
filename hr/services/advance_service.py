"""
Service: Advances & Loans Management

Responsibilities:
- Validate advance request against policy (max % of salary, max installments)
- Approve / reject advances (HR Manager or Company Admin only)
- Generate scheduled installments upon approval
- Track remaining balance after each payroll deduction
"""

import logging
from datetime import date
from decimal import Decimal
from dateutil.relativedelta import relativedelta

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


class AdvanceService:
    """All advance/loan operations go through here."""

    # ------------------------------------------------------------------
    # Request Advance — employee submits request
    # ------------------------------------------------------------------
    @staticmethod
    def request_advance(employee, amount, installments_count=1, reason=''):
        """
        Create a new advance request.
        Validates against HR policy before saving.
        Returns the Advance instance.
        """
        from hr.models import Advance, HRSettings

        hr_settings = HRSettings.get_settings()

        # --- Policy validation ---
        amount = Decimal(str(amount))
        max_allowed = (
            employee.base_salary * hr_settings.max_advance_percentage / Decimal('100')
        )

        if amount <= 0:
            raise ValidationError("مبلغ السلفة يجب أن يكون أكبر من صفر.")

        if amount > max_allowed:
            raise ValidationError(
                f"مبلغ السلفة ({amount:,.2f}) يتجاوز الحد الأقصى المسموح "
                f"({max_allowed:,.2f} ج.م — {hr_settings.max_advance_percentage}% من الراتب)."
            )

        if installments_count > hr_settings.max_installments:
            raise ValidationError(
                f"عدد الأقساط ({installments_count}) يتجاوز الحد الأقصى ({hr_settings.max_installments})."
            )

        # --- Check for existing active/pending advances ---
        active_advances = Advance.objects.filter(
            employee=employee,
            status__in=['pending', 'approved', 'active'],
        )
        if active_advances.exists():
            raise ValidationError(
                "لديك سلفة قائمة بالفعل. يجب انتهاء السلفة الحالية أو إلغاؤها قبل طلب سلفة جديدة."
            )

        advance = Advance.objects.create(
            employee=employee,
            amount=amount,
            installments_count=installments_count,
            reason=reason,
            status='pending',
            remaining_amount=amount,
        )

        logger.info(
            "[ADVANCE] New request: %s requested %s EGP (%s installments)",
            employee, amount, installments_count,
        )

        return advance

    # ------------------------------------------------------------------
    # Approve Advance — HR Manager / Admin action
    # ------------------------------------------------------------------
    @staticmethod
    def approve_advance(advance_id, approved_by_employee):
        """
        Approve an advance request.
        - Validates approver has HR Manager or admin role
        - Generates scheduled installments
        Returns the updated Advance instance.
        """
        from hr.models import Advance, AdvanceInstallment

        with transaction.atomic():
            advance = Advance.objects.select_for_update().get(pk=advance_id)

            if advance.status != 'pending':
                raise ValidationError(
                    f"لا يمكن الموافقة على سلفة بحالة: {advance.get_status_display()}"
                )

            # --- Permission check ---
            if not (approved_by_employee.is_hr_manager or approved_by_employee.user.is_superuser):
                raise ValidationError(
                    "فقط مدير الموارد البشرية أو أدمن الشركة يمكنه الموافقة على السلف."
                )

            advance.status = 'approved'
            advance.approved_by = approved_by_employee
            advance.approved_at = timezone.now()
            advance.save(update_fields=['status', 'approved_by', 'approved_at'])

            # --- Generate installments ---
            AdvanceService._generate_installments(advance)

        logger.info(
            "[ADVANCE] Approved: #%s for %s (%s EGP, %s installments) by %s",
            advance.pk, advance.employee, advance.amount,
            advance.installments_count, approved_by_employee,
        )

        return advance

    # ------------------------------------------------------------------
    # Reject Advance
    # ------------------------------------------------------------------
    @staticmethod
    def reject_advance(advance_id, rejected_by_employee, rejection_reason=''):
        """Reject an advance request."""
        from hr.models import Advance

        with transaction.atomic():
            advance = Advance.objects.select_for_update().get(pk=advance_id)

            if advance.status != 'pending':
                raise ValidationError(
                    f"لا يمكن رفض سلفة بحالة: {advance.get_status_display()}"
                )

            if not (rejected_by_employee.is_hr_manager or rejected_by_employee.user.is_superuser):
                raise ValidationError(
                    "فقط مدير الموارد البشرية أو أدمن الشركة يمكنه رفض السلف."
                )

            advance.status = 'rejected'
            advance.rejection_reason = rejection_reason
            advance.save(update_fields=['status', 'rejection_reason'])

        logger.info(
            "[ADVANCE] Rejected: #%s for %s. Reason: %s",
            advance.pk, advance.employee, rejection_reason or '(none)',
        )

        return advance

    # ------------------------------------------------------------------
    # Cancel Advance
    # ------------------------------------------------------------------
    @staticmethod
    def cancel_advance(advance_id, cancelled_by_employee):
        """
        Cancel an advance. Only pending or approved (before any deduction) can be cancelled.
        """
        from hr.models import Advance

        with transaction.atomic():
            advance = Advance.objects.select_for_update().get(pk=advance_id)

            if advance.status not in ('pending', 'approved'):
                raise ValidationError(
                    "لا يمكن إلغاء سلفة بدأ خصم أقساطها بالفعل."
                )

            # Check no installment was deducted yet
            if advance.installments.filter(status='deducted').exists():
                raise ValidationError(
                    "تم خصم قسط واحد على الأقل بالفعل. لا يمكن إلغاء السلفة."
                )

            advance.status = 'cancelled'
            advance.save(update_fields=['status'])

            # Delete scheduled installments
            advance.installments.filter(status='scheduled').delete()

        logger.info("[ADVANCE] Cancelled: #%s by %s", advance.pk, cancelled_by_employee)

        return advance

    # ------------------------------------------------------------------
    # Internal: Generate installments schedule
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_installments(advance):
        """
        Generate AdvanceInstallment records starting from next month.
        Last installment absorbs rounding difference.
        """
        from hr.models import AdvanceInstallment

        today = timezone.now().date()
        # Start from the 1st of next month
        if today.month == 12:
            start_month = date(today.year + 1, 1, 1)
        else:
            start_month = date(today.year, today.month + 1, 1)

        base_installment = (
            advance.amount / Decimal(str(advance.installments_count))
        ).quantize(Decimal('0.01'))

        total_scheduled = Decimal('0.00')
        installments_to_create = []

        for i in range(advance.installments_count):
            due_month = start_month + relativedelta(months=i)

            # Last installment absorbs rounding
            if i == advance.installments_count - 1:
                inst_amount = advance.amount - total_scheduled
            else:
                inst_amount = base_installment
                total_scheduled += inst_amount

            installments_to_create.append(AdvanceInstallment(
                advance=advance,
                installment_number=i + 1,
                amount=inst_amount,
                due_month=due_month,
                status='scheduled',
            ))

        AdvanceInstallment.objects.bulk_create(installments_to_create)

        logger.info(
            "[ADVANCE] Generated %s installments for advance #%s (base=%s EGP/month)",
            advance.installments_count, advance.pk, base_installment,
        )
