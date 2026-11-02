"""
Service: العطلات الرسمية.

* يوم جوه عطلة رسمية بيتسجّل للموظف اللي ليه وردية «عطلة رسمية» مش «غائب»
  (mark_absent_employees) — والرواتب بتعدّه زي الإجازة المدفوعة.
* العطلة اللي بتتضاف بعد ما الغياب اتسجّل (أو بيتعدّل تاريخها أو بتتمسح)
  بتصحّح أيام الغياب جوه مدتها — نفس فكرة الموافقة على إجازة بعد الغياب.
* العطلة بتغلب على الإجازة: يوم عيد جوه إجازة «بدون راتب» مابيتخصمش.
* اللي سجّل حضور في يوم العطلة بيفضل «حاضر» — مابنلمسش أي سجل فيه حضور.
"""
import logging
from datetime import timedelta

from django.db import transaction

logger = logging.getLogger('mouss_tec_core')


def is_holiday(day):
    from hr.models import PublicHoliday
    return PublicHoliday.objects.filter(from_date__lte=day, to_date__gte=day).exists()


@transaction.atomic
def apply_holiday(from_date, to_date):
    """أيام «غائب/إجازة» من غير حضور جوه المدة ← «عطلة رسمية». بيرجّع العدد."""
    from hr.models import AttendanceRecord
    fixed = (AttendanceRecord.objects
             .filter(date__gte=from_date, date__lte=to_date, clock_in__isnull=True,
                     status__in=('absent', 'excused'))
             .update(status='holiday'))
    if fixed:
        logger.info("[HOLIDAY] %s → %s: %s day(s) → holiday", from_date, to_date, fixed)
    return fixed


@transaction.atomic
def unapply_holiday(from_date, to_date):
    """العطلة اتشالت (أو اتنقلت): أيام «عطلة رسمية» جوه المدة اللي مش جوه عطلة
    تانية بترجع «إجازة» لو فيه إجازة موافق عليها، وإلا «غائب». بيرجّع العدد."""
    from hr.models import AttendanceRecord, LeaveRequest
    if not from_date or not to_date:
        return 0
    changed = 0
    day = from_date
    while day <= to_date:
        if not is_holiday(day):
            rows = list(AttendanceRecord.objects.filter(
                date=day, clock_in__isnull=True, status='holiday',
            ).values_list('pk', 'employee_id'))
            if rows:
                on_leave = set(LeaveRequest.objects.filter(
                    status='approved', from_date__lte=day, to_date__gte=day,
                    employee_id__in=[e for _pk, e in rows],
                ).values_list('employee_id', flat=True))
                excused = [pk for pk, e in rows if e in on_leave]
                absent = [pk for pk, e in rows if e not in on_leave]
                changed += AttendanceRecord.objects.filter(pk__in=excused).update(status='excused')
                changed += AttendanceRecord.objects.filter(pk__in=absent).update(status='absent')
        day += timedelta(days=1)
    if changed:
        logger.info("[HOLIDAY] removed %s → %s: %s day(s) restored", from_date, to_date, changed)
    return changed
