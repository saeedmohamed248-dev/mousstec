"""
Service: Leave approvals.

* الموافقة على إجازة بعد ما الأيام اتعلّمت «غائب» (الغياب بيتسجّل آخر اليوم)
  بتحوّل الأيام دي لـ «إجازة / إذن» — من غير كده الموظف كان بيتخصم عليه غياب
  عن أيام إجازته الموافق عليها.
* الإجازة «بدون راتب» بتتخصم من المرتب (PayrollService بيعدّ أيامها).
"""
import logging
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


def _can_review(reviewer, by_user, leave):
    """مدير الموارد البشرية، أو الأدمن، أو المدير المباشر للموظف."""
    if by_user is not None and by_user.is_superuser:
        return True
    if reviewer is None:
        return False
    return bool(reviewer.is_hr_manager or reviewer.user.is_superuser
                or (leave.employee.supervisor_id and leave.employee.supervisor_id == reviewer.pk))


@transaction.atomic
def approve_leave(leave_id, reviewer=None, by_user=None, notes=''):
    from hr.models import AttendanceRecord, LeaveRequest
    leave = LeaveRequest.objects.select_for_update(of=('self',)).select_related('employee').get(pk=leave_id)
    if leave.status != 'pending':
        raise ValidationError(f"الطلب حالته «{leave.get_status_display()}» — مينفعش يتوافق عليه.")
    if not _can_review(reviewer, by_user, leave):
        raise ValidationError("الموافقة على الإجازات لمدير الموارد البشرية أو المدير المباشر أو الأدمن بس.")
    leave.status = 'approved'
    leave.reviewed_by = reviewer
    leave.reviewed_at = timezone.now()
    if notes:
        leave.review_notes = notes[:1000]
    leave.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])
    fixed = (AttendanceRecord.objects
             .filter(employee_id=leave.employee_id, status='absent',
                     date__gte=leave.from_date, date__lte=leave.to_date)
             .update(status='excused'))
    logger.info("[LEAVE] Approved #%s (%s absent day(s) → excused)", leave.pk, fixed)
    return leave


@transaction.atomic
def reject_leave(leave_id, reviewer=None, by_user=None, notes=''):
    from hr.models import LeaveRequest
    leave = LeaveRequest.objects.select_for_update(of=('self',)).select_related('employee').get(pk=leave_id)
    if leave.status != 'pending':
        raise ValidationError(f"الطلب حالته «{leave.get_status_display()}» — مينفعش يترفض.")
    if not _can_review(reviewer, by_user, leave):
        raise ValidationError("رفض الإجازات لمدير الموارد البشرية أو المدير المباشر أو الأدمن بس.")
    leave.status = 'rejected'
    leave.reviewed_by = reviewer
    leave.reviewed_at = timezone.now()
    if notes:
        leave.review_notes = notes[:1000]
    leave.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])
    return leave


def unpaid_leave_days(employee, month, year):
    """أيام الإجازة «بدون راتب» الموافق عليها اللي اتسجّلت إجازة في الشهر ده."""
    from hr.models import AttendanceRecord, LeaveRequest
    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    days = set()
    for lv in LeaveRequest.objects.filter(employee=employee, status='approved', leave_type='unpaid',
                                          from_date__lte=last, to_date__gte=first):
        start, end = max(lv.from_date, first), min(lv.to_date, last)
        days.update(AttendanceRecord.objects.filter(
            employee=employee, status='excused', date__gte=start, date__lte=end,
        ).values_list('date', flat=True))
    return len(days)
