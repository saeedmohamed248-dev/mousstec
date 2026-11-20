"""👔 شاشات الموارد البشرية — لوحة مدير الموارد البشرية ومراجعة السلف والإجازات."""

import logging
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from ._common import _err_text

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 6.5. HR Manager Dashboard
# =====================================================================

@login_required
def hr_manager_dashboard(request):
    """
    لوحة تحكم مدير الموارد البشرية — تعرض:
    - حضور اليوم: الحاضرون / المتأخرون / الغائبون (مع face_verified flag)
    - طلبات السلف المعلقة (Advance.status='pending')
    - طلبات الإجازات المعلقة (LeaveRequest.status='pending')
    - تنبيهات: موظفين بدون مدير مباشر، عقود قاربت على الانتهاء
    - إحصائيات عامة عن القوى العاملة
    """
    from hr.models import Employee, AttendanceRecord, LeaveRequest, Advance

    # ── صلاحية: HR manager / superuser / staff / قسم HR ──
    try:
        me = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        me = None

    is_authorized = (
        request.user.is_superuser
        or request.user.is_staff
        or (me and (me.is_hr_manager or me.department == 'hr' or me.department == 'management'))
    )
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("هذه اللوحة متاحة لمدير الموارد البشرية فقط.")

    today = timezone.localdate()

    # ── حضور اليوم ──
    today_records = (
        AttendanceRecord.objects
        .filter(date=today)
        .select_related('employee', 'employee__user')
    )
    records_by_emp = {r.employee_id: r for r in today_records}

    all_active_employees = list(
        Employee.objects.filter(is_active=True)
        .select_related('user', 'supervisor', 'supervisor__user')
        .order_by('department', 'user__first_name')
    )

    present_list = []
    late_list = []
    absent_list = []
    not_checked_in = []  # موظفين مفيش لهم سجل النهاردة

    for emp in all_active_employees:
        rec = records_by_emp.get(emp.pk)
        if rec is None:
            not_checked_in.append(emp)
            continue
        if rec.status == 'present':
            present_list.append(rec)
        elif rec.status == 'late':
            late_list.append(rec)
        elif rec.status == 'absent':
            absent_list.append(rec)

    # ── طلبات معلقة ──
    pending_advances = list(
        Advance.objects.filter(status='pending')
        .select_related('employee', 'employee__user')
        .order_by('-requested_at')[:20]
    )
    pending_leaves = list(
        LeaveRequest.objects.filter(status='pending')
        .select_related('employee', 'employee__user')
        .order_by('-created_at')[:20]
    )
    # 📅 قبل الموافقة: الموظف خد كام يوم من نفس النوع السنة دي (مفيش رصيد إجازات
    #    في السيستم، فالمدير كان بيوافق من غير ما يعرف اللي اتاخد قبل كده).
    for lv in pending_leaves:
        taken = LeaveRequest.objects.filter(
            employee_id=lv.employee_id, leave_type=lv.leave_type, status='approved',
            from_date__year=lv.from_date.year)
        lv.taken_this_year = sum(x.total_days for x in taken)

    pending_advances_count = Advance.objects.filter(status='pending').count()
    pending_leaves_count = LeaveRequest.objects.filter(status='pending').count()

    # ── تنبيهات ──
    employees_no_supervisor = [
        e for e in all_active_employees
        if e.supervisor_id is None and not e.is_hr_manager and e.department not in ('management', 'hr')
    ]

    # ── إحصائيات عامة ──
    total_active = len(all_active_employees)
    by_department = {}
    for emp in all_active_employees:
        key = emp.get_department_display()
        by_department[key] = by_department.get(key, 0) + 1
    by_department_list = sorted(by_department.items(), key=lambda x: -x[1])

    # نسب الحضور
    attendance_rate = 0
    if total_active > 0:
        attendance_rate = ((len(present_list) + len(late_list)) / total_active) * 100

    # ── حضور آخر 7 أيام (للـ trend) ──
    from datetime import timedelta as _td
    week_trend = []
    for i in range(6, -1, -1):
        d = today - _td(days=i)
        cnt = AttendanceRecord.objects.filter(
            date=d, status__in=['present', 'late']
        ).count()
        week_trend.append({
            'date': d,
            'label': d.strftime('%a %d/%m'),
            'count': cnt,
            'percent': (cnt / total_active * 100) if total_active else 0,
        })

    # إحصائيات شهرية
    from django.db.models import Sum as _Sum
    month_advances_total = Advance.objects.filter(
        requested_at__year=today.year, requested_at__month=today.month,
    ).aggregate(s=_Sum('amount'))['s'] or Decimal('0')
    month_approved_leaves = LeaveRequest.objects.filter(
        reviewed_at__year=today.year, reviewed_at__month=today.month,
        status='approved',
    ).count()

    stats = {
        'total_active': total_active,
        'present_today': len(present_list),
        'late_today': len(late_list),
        'absent_today': len(absent_list),
        'not_checked_in': len(not_checked_in),
        'face_verified_today': sum(1 for r in today_records if r.face_verified),
        'attendance_rate': attendance_rate,
        'pending_advances': pending_advances_count,
        'pending_leaves': pending_leaves_count,
        'month_advances_total': month_advances_total,
        'month_approved_leaves': month_approved_leaves,
        'alerts_no_supervisor': len(employees_no_supervisor),
    }

    # سلف اتوافق عليها ولسه ماتصرفتش من الخزنة (الصرف خطوة لوحدها)
    approved_undisbursed = list(
        Advance.objects.filter(status='approved', disbursed_at__isnull=True)
        .select_related('employee', 'employee__user').order_by('approved_at')[:20]
    )

    return render(request, 'hr/hr_manager_dashboard.html', {
        'approved_undisbursed': approved_undisbursed,
        'treasuries': _payout_treasuries(),
        'flash_ok': request.GET.get('ok', ''), 'flash_err': request.GET.get('err', '')[:300],
        'me': me,
        'today': today,
        'stats': stats,
        'present_list': present_list,
        'late_list': late_list,
        'absent_list': absent_list,
        'not_checked_in': not_checked_in,
        'pending_advances': pending_advances,
        'pending_leaves': pending_leaves,
        'employees_no_supervisor': employees_no_supervisor[:10],
        'by_department_list': by_department_list,
        'week_trend': week_trend,
    })


def _payout_treasuries():
    """الخزائن اللي ينفع تتصرف منها السلفة (حسب قطاع الشركة)."""
    try:
        from hr.services.payroll_service import PayrollService
        if PayrollService._detect_industry() == 'printing':
            from printing.models import PrintTreasury
            return list(PrintTreasury.objects.filter(is_active=True, kind__in=PrintTreasury.MONEY_KINDS)
                        .order_by('name'))
        from inventory.models import Treasury
        return list(Treasury.objects.filter(is_active=True).select_related('branch')
                    .order_by('branch__name', 'name'))
    except Exception:  # noqa: BLE001
        return []


def _hr_reviewer(request):
    """(ملف HR للمستخدم أو None, مسموح له يراجع؟)"""
    from hr.models import Employee
    from hr.services.leave_service import is_company_admin
    me = Employee.objects.filter(user=request.user, is_active=True).first()
    allowed = is_company_admin(request.user) or bool(
        me and (me.is_hr_manager or me.department in ('hr', 'management')))
    return me, allowed


@login_required(login_url='/secure-portal/')
@require_POST
def hr_review_advance(request, advance_id, action):
    """✅ موافقة / ❌ رفض / 💵 صرف سلفة — من لوحة مدير الموارد البشرية."""
    from urllib.parse import urlencode
    from hr.services.advance_service import AdvanceService
    me, allowed = _hr_reviewer(request)
    back = '/hr/manager/'
    if not allowed:
        return redirect(f"{back}?{urlencode({'err': 'المراجعة لمدير الموارد البشرية أو الأدمن بس.'})}#advances")
    try:
        if action == 'approve':
            AdvanceService.approve_advance(advance_id, me, by_user=request.user)
            if request.POST.get('disburse') == '1':
                AdvanceService.disburse_advance(advance_id, request.POST.get('treasury_id') or None,
                                                by_user=request.user)
                ok = 'advance_disbursed'
            else:
                ok = 'advance_approved'
        elif action == 'reject':
            AdvanceService.reject_advance(advance_id, me, (request.POST.get('reason') or '')[:500],
                                          by_user=request.user)
            ok = 'advance_rejected'
        elif action == 'disburse':
            AdvanceService.disburse_advance(advance_id, request.POST.get('treasury_id') or None,
                                            by_user=request.user)
            ok = 'advance_disbursed'
        else:
            return redirect(f"{back}#advances")
    except Exception as exc:  # noqa: BLE001 — الرسالة للمستخدم (ValidationError غالباً)
        return redirect(f"{back}?{urlencode({'err': _err_text(exc)[:300]})}#advances")
    return redirect(f"{back}?ok={ok}#advances")


@login_required(login_url='/secure-portal/')
@require_POST
def hr_review_leave(request, leave_id, action):
    """✅ موافقة / ❌ رفض إجازة — من لوحة مدير الموارد البشرية."""
    from urllib.parse import urlencode
    from hr.services.leave_service import approve_leave, reject_leave
    me, allowed = _hr_reviewer(request)
    back = '/hr/manager/'
    if not allowed:
        return redirect(f"{back}?{urlencode({'err': 'المراجعة لمدير الموارد البشرية أو الأدمن بس.'})}#leaves")
    notes = (request.POST.get('notes') or '')[:1000]
    try:
        if action == 'approve':
            approve_leave(leave_id, reviewer=me, by_user=request.user, notes=notes)
        elif action == 'reject':
            reject_leave(leave_id, reviewer=me, by_user=request.user, notes=notes)
        else:
            return redirect(f"{back}#leaves")
    except Exception as exc:  # noqa: BLE001
        return redirect(f"{back}?{urlencode({'err': _err_text(exc)[:300]})}#leaves")
    return redirect(f"{back}?ok={'leave_approved' if action == 'approve' else 'leave_rejected'}#leaves")
