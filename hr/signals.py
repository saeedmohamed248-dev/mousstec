"""
Signals: HR Module — thin dispatch layer (matches project pattern).
All heavy logic lives in services/*.py
"""

import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger('mouss_tec_core')


# ------------------------------------------------------------------
# 1. Auto-approve DesignSubmission on create (if auto_approve enabled)
# ------------------------------------------------------------------
@receiver(post_save, sender='hr.DesignSubmission')
def handle_design_submission_auto_approve(sender, instance, created, **kwargs):
    """
    When a DesignSubmission is created, the DesignWorkflowService.submit_design()
    already handles auto-approve logic. This signal is a safety net for
    direct ORM creates that bypass the service layer.
    """
    if not created:
        return

    # Only act if status is still pending and designer has auto-approve
    if instance.status == 'pending' and instance.designer.auto_approve_designs:
        from django.utils import timezone
        instance.status = 'approved'
        instance.auto_approved = True
        instance.reviewed_at = timezone.now()
        instance.save(update_fields=['status', 'auto_approved', 'reviewed_at'])
        logger.info(
            "[SIGNAL] Auto-approved design #%s for %s (safety net)",
            instance.pk, instance.designer,
        )


# ------------------------------------------------------------------
# 2. Cache old Employee instance for change tracking
# ------------------------------------------------------------------
@receiver(pre_save, sender='hr.Employee')
def cache_old_employee(sender, instance, **kwargs):
    """Cache the old instance before save for comparison in post_save."""
    if instance.pk:
        try:
            instance._old_instance = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._old_instance = None
    else:
        instance._old_instance = None


# ------------------------------------------------------------------
# 3. Log salary changes for audit trail
# ------------------------------------------------------------------
@receiver(post_save, sender='hr.Employee')
def log_employee_salary_change(sender, instance, created, **kwargs):
    """Log when an employee's salary is changed."""
    if created:
        return

    old = getattr(instance, '_old_instance', None)
    if old and old.base_salary != instance.base_salary:
        logger.info(
            "[HR AUDIT] Salary changed for %s: %s -> %s",
            instance, old.base_salary, instance.base_salary,
        )


# ------------------------------------------------------------------
# 4. Auto-set Leave dates as excused in Attendance
# ------------------------------------------------------------------
@receiver(post_save, sender='hr.LeaveRequest')
def mark_leave_days_excused(sender, instance, **kwargs):
    """
    When a leave request is approved, create AttendanceRecord entries
    with status='excused' for each day of the leave period.
    """
    if instance.status != 'approved':
        return

    from hr.models import AttendanceRecord
    from datetime import timedelta

    current = instance.from_date
    while current <= instance.to_date:
        AttendanceRecord.objects.update_or_create(
            employee=instance.employee,
            date=current,
            defaults={'status': 'excused', 'notes': f'إجازة: {instance.get_leave_type_display()}'},
        )
        current += timedelta(days=1)

    logger.info(
        "[HR SIGNAL] Marked %s days as excused for %s (leave #%s)",
        instance.total_days, instance.employee, instance.pk,
    )
