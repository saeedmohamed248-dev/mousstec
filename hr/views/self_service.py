"""🙋 شاشات الموارد البشرية — طلبات الموظف: السلف، الإجازات، قسيمة المرتب."""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from ._common import _err_text, _json_response

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 2. Advance Self-Service APIs
# =====================================================================

@login_required(login_url='/secure-portal/')
@require_POST
def api_request_advance(request):
    """
    POST /hr/api/advance/request/
    Body: { "amount": 5000, "installments": 3, "reason": "..." }
    """
    from hr.models import Employee
    from hr.services.advance_service import AdvanceService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    amount = data.get('amount')
    installments = data.get('installments', 1)
    reason = data.get('reason', '')

    if not amount:
        return _json_response({"error": "مبلغ السلفة مطلوب."}, 400)

    try:
        advance = AdvanceService.request_advance(
            employee=employee,
            amount=amount,
            installments_count=int(installments),
            reason=reason,
        )
        return _json_response({
            "success": True,
            "message": "تم تقديم طلب السلفة بنجاح. في انتظار الموافقة.",
            "data": {
                "advance_id": advance.pk,
                "amount": str(advance.amount),
                "installments": advance.installments_count,
                "installment_amount": str(advance.installment_amount),
                "status": advance.get_status_display(),
            },
        })
    except Exception as e:
        return _json_response({"error": _err_text(e)}, 400)


@login_required(login_url='/secure-portal/')
@require_GET
def api_my_advances(request):
    """
    GET /hr/api/advance/mine/
    Returns the logged-in employee's active and historical advances.
    """
    from hr.models import Employee, Advance

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    advances = Advance.objects.filter(employee=employee).order_by('-requested_at')
    data = []
    for adv in advances:
        installments = list(
            adv.installments.order_by('installment_number').values(
                'installment_number', 'amount', 'due_month', 'status',
            )
        )
        data.append({
            "id": adv.pk,
            "amount": str(adv.amount),
            "remaining": str(adv.remaining_amount),
            "status": adv.get_status_display(),
            "installments": installments,
            "requested_at": adv.requested_at.isoformat() if adv.requested_at else None,
        })

    return _json_response({"advances": data})


# =====================================================================
# 4. Leave Request Self-Service API
# =====================================================================

@login_required(login_url='/secure-portal/')
@require_POST
def api_request_leave(request):
    """
    POST /hr/api/leave/request/
    Body: { "leave_type": "annual", "from_date": "2026-06-01", "to_date": "2026-06-03", "reason": "..." }
    """
    from hr.models import Employee, LeaveRequest
    from datetime import datetime

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    leave_type = data.get('leave_type', '')
    from_date = data.get('from_date', '')
    to_date = data.get('to_date', '')
    reason = data.get('reason', '')

    valid_types = [c[0] for c in LeaveRequest.TYPE_CHOICES]
    if leave_type not in valid_types:
        return _json_response({"error": f"نوع الإجازة غير صالح. الأنواع: {', '.join(valid_types)}"}, 400)

    try:
        from_date = datetime.strptime(from_date, '%Y-%m-%d').date()
        to_date = datetime.strptime(to_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return _json_response({"error": "تنسيق التاريخ غير صالح. استخدم YYYY-MM-DD"}, 400)

    if to_date < from_date:
        return _json_response({"error": "تاريخ النهاية يجب أن يكون بعد تاريخ البداية."}, 400)

    leave = LeaveRequest.objects.create(
        employee=employee,
        leave_type=leave_type,
        from_date=from_date,
        to_date=to_date,
        reason=reason,
        status='pending',
    )

    return _json_response({
        "success": True,
        "message": "تم تقديم طلب الإجازة بنجاح. في انتظار الموافقة.",
        "data": {
            "leave_id": leave.pk,
            "type": leave.get_leave_type_display(),
            "from": str(leave.from_date),
            "to": str(leave.to_date),
            "days": leave.total_days,
            "status": leave.get_status_display(),
        },
    })


# =====================================================================
# 5. Payroll Self-View API
# =====================================================================

@login_required(login_url='/secure-portal/')
@require_GET
def api_my_payslip(request):
    """
    GET /hr/api/payslip/?month=6&year=2026
    Returns the logged-in employee's payslip for the given period.
    """
    from django.db import connection
    from hr.models import Employee, PayrollEntry
    from tenancy.services.entitlements import EntitlementService

    # 🔒 المرتبات ميزة Gold/Pro وأعلى (hr_payroll)
    if not EntitlementService.allows(getattr(connection, 'tenant', None), 'hr_payroll'):
        return _json_response({"error": "كشوف المرتبات مش ضمن باقتك — متاحة في Gold/Pro وأعلى.",
                               "code": "feature_not_in_plan", "upgrade_url": "/subscription/manage/"}, 403)

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    now = timezone.now()
    try:
        month = int(request.GET.get('month', now.month))
        year = int(request.GET.get('year', now.year))
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise ValueError
    except (ValueError, TypeError):
        return _json_response({"error": "شهر أو سنة غير صالحة."}, 400)

    entry = PayrollEntry.objects.filter(
        employee=employee,
        payroll_run__period_month=month,
        payroll_run__period_year=year,
    ).first()

    if not entry:
        return _json_response({"error": f"لا يوجد كشف راتب لفترة {month}/{year}."}, 404)

    return _json_response({
        "period": f"{month}/{year}",
        "base_salary": str(entry.base_salary),
        "days_present": entry.days_present,
        "days_absent": entry.days_absent,
        "days_late": entry.days_late,
        "days_excused": entry.days_excused,
        "total_late_minutes": entry.total_late_minutes,
        "late_deduction": str(entry.late_deduction),
        "absence_deduction": str(entry.absence_deduction),
        "advance_deduction": str(entry.advance_deduction),
        "other_deductions": str(entry.other_deductions),
        "bonuses": str(entry.bonuses),
        "overtime_pay": str(entry.overtime_pay),
        "total_deductions": str(entry.total_deductions),
        "total_additions": str(entry.total_additions),
        "net_salary": str(entry.net_salary),
        "status": entry.payroll_run.get_status_display(),
    })
