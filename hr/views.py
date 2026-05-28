"""
Views: HR Module — API endpoints for mobile/PWA attendance and self-service.
JSON APIs + Designer Dashboard (HTML).
"""

import json
import logging
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages as django_messages
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

logger = logging.getLogger('mouss_tec_core')


def _json_response(data, status=200):
    """Safe JSON response wrapper — masks errors in production."""
    if status >= 500 and not settings.DEBUG:
        if 'error' in data:
            data = {"error": "حدث خطأ داخلي. يرجى المحاولة لاحقاً."}
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


# =====================================================================
# 1. Attendance APIs (Mobile / PWA)
# =====================================================================

@csrf_exempt
@login_required(login_url='/secure-portal/')
@require_POST
def api_clock_in(request):
    """
    POST /hr/api/clock-in/
    Body: { "latitude": 30.044, "longitude": 31.235, "face_verified": true }
    """
    from hr.models import Employee
    from hr.services.attendance_service import AttendanceService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    latitude = data.get('latitude')
    longitude = data.get('longitude')
    face_verified = data.get('face_verified', False)

    try:
        record = AttendanceService.clock_in(
            employee=employee,
            latitude=Decimal(str(latitude)) if latitude else None,
            longitude=Decimal(str(longitude)) if longitude else None,
            face_verified=bool(face_verified),
        )
        return _json_response({
            "success": True,
            "message": "تم تسجيل حضورك بنجاح.",
            "data": {
                "status": record.get_status_display(),
                "clock_in": record.clock_in.strftime('%H:%M:%S'),
                "late_minutes": record.late_minutes,
                "face_verified": record.face_verified,
                "location_verified": record.location_verified,
            },
        })
    except Exception as e:
        return _json_response({"error": str(e)}, 400)


@csrf_exempt
@login_required(login_url='/secure-portal/')
@require_POST
def api_clock_out(request):
    """
    POST /hr/api/clock-out/
    Body: { "latitude": 30.044, "longitude": 31.235 }
    """
    from hr.models import Employee
    from hr.services.attendance_service import AttendanceService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    latitude = data.get('latitude')
    longitude = data.get('longitude')

    try:
        record = AttendanceService.clock_out(
            employee=employee,
            latitude=Decimal(str(latitude)) if latitude else None,
            longitude=Decimal(str(longitude)) if longitude else None,
        )
        return _json_response({
            "success": True,
            "message": "تم تسجيل انصرافك بنجاح.",
            "data": {
                "clock_out": record.clock_out.strftime('%H:%M:%S'),
                "worked_hours": str(record.worked_hours),
                "overtime_hours": str(record.overtime_hours),
            },
        })
    except Exception as e:
        return _json_response({"error": str(e)}, 400)


@login_required(login_url='/secure-portal/')
@require_GET
def api_my_attendance(request):
    """
    GET /hr/api/my-attendance/?month=6&year=2026
    Returns the logged-in employee's attendance summary for the month.
    """
    from hr.models import Employee, AttendanceRecord
    from hr.services.attendance_service import AttendanceService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    now = timezone.now()
    month = int(request.GET.get('month', now.month))
    year = int(request.GET.get('year', now.year))

    summary = AttendanceService.get_monthly_summary(employee, month, year)

    records = AttendanceRecord.objects.filter(
        employee=employee, date__month=month, date__year=year,
    ).order_by('date').values(
        'date', 'status', 'clock_in', 'clock_out', 'late_minutes', 'worked_hours',
    )

    return _json_response({
        "employee": str(employee),
        "period": f"{month}/{year}",
        "summary": summary,
        "records": list(records),
    })


# =====================================================================
# 2. Advance Self-Service APIs
# =====================================================================

@csrf_exempt
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
        return _json_response({"error": str(e)}, 400)


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
# 3. Design Workflow APIs
# =====================================================================

@csrf_exempt
@login_required(login_url='/secure-portal/')
@require_POST
def api_submit_design(request):
    """
    POST /hr/api/design/submit/  (multipart/form-data)
    Fields: title, execution_type, description, design_file, preview_image (optional)
    """
    from hr.models import Employee
    from hr.services.design_workflow_service import DesignWorkflowService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    title = request.POST.get('title', '').strip()
    if not title:
        return _json_response({"error": "عنوان التصميم مطلوب."}, 400)

    design_file = request.FILES.get('design_file')
    if not design_file:
        return _json_response({"error": "ملف التصميم مطلوب."}, 400)

    try:
        submission = DesignWorkflowService.submit_design(
            designer_employee=employee,
            title=title,
            design_file=design_file,
            execution_type=request.POST.get('execution_type', 'manual'),
            description=request.POST.get('description', ''),
            preview_image=request.FILES.get('preview_image'),
            related_order_id=request.POST.get('related_order_id') or None,
        )
        return _json_response({
            "success": True,
            "message": (
                "تم رفع التصميم واعتماده تلقائياً." if submission.auto_approved
                else "تم رفع التصميم وإرساله للمراجعة."
            ),
            "data": {
                "submission_id": submission.pk,
                "status": submission.get_status_display(),
                "auto_approved": submission.auto_approved,
                "reviewer": str(submission.reviewer) if submission.reviewer else None,
            },
        })
    except Exception as e:
        return _json_response({"error": str(e)}, 400)


@login_required(login_url='/secure-portal/')
@require_GET
def api_my_designs(request):
    """
    GET /hr/api/design/mine/
    Returns the logged-in designer's submissions.
    """
    from hr.models import Employee, DesignSubmission

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    submissions = DesignSubmission.objects.filter(
        designer=employee
    ).order_by('-created_at').values(
        'id', 'title', 'execution_type', 'status',
        'auto_approved', 'review_notes', 'created_at',
    )

    return _json_response({"designs": list(submissions)})


@login_required(login_url='/secure-portal/')
@require_GET
def api_pending_reviews(request):
    """
    GET /hr/api/design/pending/
    Returns designs pending review for the logged-in supervisor.
    """
    from hr.models import Employee, DesignSubmission

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    # Show designs where this employee is the supervisor of the designer
    from django.db.models import Q
    pending = DesignSubmission.objects.filter(
        Q(reviewer=employee) | Q(designer__supervisor=employee),
        status='pending',
    ).distinct().order_by('-created_at').values(
        'id', 'title', 'designer__user__first_name', 'designer__user__last_name',
        'execution_type', 'status', 'created_at',
    )

    return _json_response({"pending_reviews": list(pending)})


@csrf_exempt
@login_required(login_url='/secure-portal/')
@require_POST
def api_review_design(request, submission_id):
    """
    POST /hr/api/design/<id>/review/
    Body: { "action": "approve"|"reject"|"revision", "notes": "..." }
    """
    from hr.models import Employee
    from hr.services.design_workflow_service import DesignWorkflowService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    action = data.get('action', '').strip()
    notes = data.get('notes', '')

    try:
        if action == 'approve':
            sub = DesignWorkflowService.approve_design(submission_id, employee, notes)
        elif action == 'reject':
            sub = DesignWorkflowService.reject_design(submission_id, employee, notes)
        elif action == 'revision':
            sub = DesignWorkflowService.request_revision(submission_id, employee, notes)
        else:
            return _json_response({"error": "الإجراء غير صالح. استخدم: approve, reject, revision"}, 400)

        return _json_response({
            "success": True,
            "message": f"تم {sub.get_status_display()} التصميم بنجاح.",
            "data": {
                "submission_id": sub.pk,
                "status": sub.get_status_display(),
            },
        })
    except Exception as e:
        return _json_response({"error": str(e)}, 400)


# =====================================================================
# 4. Leave Request Self-Service API
# =====================================================================

@csrf_exempt
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
    from hr.models import Employee, PayrollEntry

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    now = timezone.now()
    month = int(request.GET.get('month', now.month))
    year = int(request.GET.get('year', now.year))

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


# =====================================================================
# 6. Designer Dashboard — لوحة تحكم المصمم (HTML)
# =====================================================================

@login_required
def designer_dashboard(request):
    """
    لوحة تحكم المصمم — تعرض:
    - اشتراك AI الحالي (حالة، أيام متبقية، استهلاك)
    - تجديد/تفعيل اشتراك AI
    - آخر التصاميم المرفوعة
    - إحصائيات الأداء
    """
    from hr.models import Employee, DesignSubmission, AIDesignSubscription
    from hr.services.ai_subscription_service import AISubscriptionService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        django_messages.error(request, "ليس لديك ملف موظف مفعّل.")
        return redirect('/')

    # ── معالجة POST: تجديد/تفعيل اشتراك AI ──
    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'activate_ai':
            plan = request.POST.get('plan', 'basic')
            payment_method = request.POST.get('payment_method', 'visa')
            card_last_four = request.POST.get('card_last_four', '')

            try:
                if payment_method == 'visa':
                    card_token = request.POST.get('card_token', 'simulated')
                    sub = AISubscriptionService.process_visa_payment(
                        designer=employee,
                        plan=plan,
                        card_token=card_token,
                        card_last_four=card_last_four,
                    )
                    plan_label = sub.get_plan_display()
                    django_messages.success(
                        request,
                        f"تم تفعيل اشتراك AI «{plan_label}» بنجاح عبر الفيزا! "
                        f"ينتهي في {sub.end_date}."
                    )
                else:
                    # wallet أو أي طريقة أخرى
                    sub = AISubscriptionService.activate_subscription(
                        designer=employee,
                        plan=plan,
                        payment_method=payment_method,
                        duration_days=30,
                    )
                    django_messages.success(request, f"تم تفعيل اشتراك AI بنجاح! ينتهي في {sub.end_date}.")
            except Exception as e:
                django_messages.error(request, f"فشل التفعيل: {e}")

            return redirect('hr:designer_dashboard')

        elif action == 'cancel_ai':
            sub_id = request.POST.get('subscription_id')
            if sub_id:
                try:
                    AISubscriptionService.cancel_subscription(
                        subscription_id=int(sub_id),
                        cancelled_by_user=request.user,
                        reason="إلغاء ذاتي من المصمم",
                    )
                    django_messages.success(request, "تم إلغاء اشتراك AI.")
                except Exception as e:
                    django_messages.error(request, f"فشل الإلغاء: {e}")
            return redirect('hr:designer_dashboard')

    # ── جمع البيانات ──
    ai_sub = AISubscriptionService.get_designer_subscription(employee)

    recent_designs = DesignSubmission.objects.filter(
        designer=employee,
    ).order_by('-created_at')[:10]

    # إحصائيات
    all_designs = DesignSubmission.objects.filter(designer=employee)
    stats = {
        'total': all_designs.count(),
        'approved': all_designs.filter(status='approved').count(),
        'pending': all_designs.filter(status='pending').count(),
        'rejected': all_designs.filter(status='rejected').count(),
        'ai_generated': all_designs.filter(execution_type__in=['ai_generated', 'ai_assisted']).count(),
    }

    # أسعار الباقات للعرض
    plan_prices = AIDesignSubscription.PLAN_PRICES
    plan_limits = AIDesignSubscription.PLAN_LIMITS

    return render(request, 'hr/designer_dashboard.html', {
        'employee': employee,
        'ai_sub': ai_sub,
        'recent_designs': recent_designs,
        'stats': stats,
        'plan_prices': plan_prices,
        'plan_limits': plan_limits,
    })


# =====================================================================
# 7. Admin AI Subscription Management APIs
# =====================================================================

@csrf_exempt
@login_required
@require_POST
def api_admin_ai_activate(request):
    """
    POST /hr/api/ai-sub/admin-activate/
    Body: { "employee_id": 5, "plan": "pro", "duration_days": 30, "notes": "..." }
    Admin/HR Manager only.
    """
    from hr.models import Employee
    from hr.services.ai_subscription_service import AISubscriptionService

    # فحص الصلاحية: أدمن أو HR Manager
    if not request.user.is_staff:
        try:
            requester = Employee.objects.get(user=request.user, is_active=True)
            if not requester.is_hr_manager:
                return _json_response({"error": "ليس لديك صلاحية."}, 403)
        except Employee.DoesNotExist:
            return _json_response({"error": "ليس لديك صلاحية."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    employee_id = data.get('employee_id')
    plan = data.get('plan', 'basic')
    duration_days = int(data.get('duration_days', 30))
    notes = data.get('notes', '')

    if not employee_id:
        return _json_response({"error": "employee_id مطلوب."}, 400)

    try:
        designer = Employee.objects.get(pk=employee_id, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "الموظف غير موجود."}, 404)

    try:
        sub = AISubscriptionService.admin_activate(
            designer=designer,
            plan=plan,
            admin_user=request.user,
            duration_days=duration_days,
            notes=notes,
        )
        return _json_response({
            "success": True,
            "message": f"تم تفعيل اشتراك AI لـ {designer} — {sub.get_plan_display()}",
            "data": {
                "subscription_id": sub.pk,
                "plan": sub.plan,
                "start_date": str(sub.start_date),
                "end_date": str(sub.end_date),
                "status": sub.get_status_display(),
            },
        })
    except Exception as e:
        return _json_response({"error": str(e)}, 400)


@csrf_exempt
@login_required
@require_POST
def api_admin_ai_cancel(request):
    """
    POST /hr/api/ai-sub/admin-cancel/
    Body: { "subscription_id": 12, "reason": "..." }
    Admin/HR Manager only.
    """
    from hr.models import Employee
    from hr.services.ai_subscription_service import AISubscriptionService

    if not request.user.is_staff:
        try:
            requester = Employee.objects.get(user=request.user, is_active=True)
            if not requester.is_hr_manager:
                return _json_response({"error": "ليس لديك صلاحية."}, 403)
        except Employee.DoesNotExist:
            return _json_response({"error": "ليس لديك صلاحية."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    sub_id = data.get('subscription_id')
    reason = data.get('reason', '')

    if not sub_id:
        return _json_response({"error": "subscription_id مطلوب."}, 400)

    try:
        sub = AISubscriptionService.admin_cancel(
            subscription_id=int(sub_id),
            admin_user=request.user,
            reason=reason,
        )
        return _json_response({
            "success": True,
            "message": f"تم إلغاء اشتراك AI لـ {sub.designer}",
        })
    except Exception as e:
        return _json_response({"error": str(e)}, 400)
