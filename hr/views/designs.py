"""🎨 شاشات الموارد البشرية — أعمال المصممين ومراجعتها ولوحة المصمم."""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.contrib import messages as django_messages
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from ._common import _json_response

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 3. Design Workflow APIs
# =====================================================================

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
            # 🐛 [FIX]: التفعيل الذاتي كان «دفع بالفيزا» متمثّل (`payment_success = True`)
            #    أو «محفظة» من غير أي دفع — أي مصمم يبعت الطلب ده ياخد اشتراك متسجّل
            #    «مدفوع» برقم عملية وهمي. مفيش بوابة دفع متربطة بالاشتراك ده: التفعيل
            #    من الـ HR/الإدارة بعد الدفع (`/hr/api/ai-sub/admin-activate/`).
            django_messages.error(request, "تفعيل اشتراك AI بيتم من الإدارة (HR) بعد الدفع — كلّم مديرك.")
            return redirect('hr:designer_dashboard')

        elif action == 'cancel_ai':
            sub_id = request.POST.get('subscription_id')
            if sub_id:
                try:
                    from hr.models import AIDesignSubscription
                    # 🛡️ Verify subscription belongs to THIS employee
                    sub_obj = AIDesignSubscription.objects.get(pk=int(sub_id), designer=employee)
                    AISubscriptionService.cancel_subscription(
                        subscription_id=sub_obj.pk,
                        cancelled_by_user=request.user,
                        reason="إلغاء ذاتي من المصمم",
                    )
                    django_messages.success(request, "تم إلغاء اشتراك AI.")
                except AIDesignSubscription.DoesNotExist:
                    django_messages.error(request, "لا يمكنك إلغاء اشتراك لا يخصك.")
                except Exception as e:
                    django_messages.error(request, f"فشل الإلغاء: {e}")
            return redirect('hr:designer_dashboard')

        else:
            django_messages.error(request, "إجراء غير معروف.")
            return redirect('hr:designer_dashboard')

    # ── جمع البيانات ──
    ai_sub = AISubscriptionService.get_designer_subscription(employee)

    recent_designs = DesignSubmission.objects.filter(
        designer=employee,
    ).order_by('-created_at')[:8]

    # إحصائيات شاملة
    all_designs = DesignSubmission.objects.filter(designer=employee)
    today = timezone.localdate()
    this_month = (today.year, today.month)

    today_designs = all_designs.filter(created_at__date=today).count()
    month_designs = all_designs.filter(
        created_at__year=this_month[0], created_at__month=this_month[1],
    ).count()

    stats = {
        'total': all_designs.count(),
        'today': today_designs,
        'month': month_designs,
        'approved': all_designs.filter(status='approved').count(),
        'pending': all_designs.filter(status='pending').count(),
        'rejected': all_designs.filter(status='rejected').count(),
        'ai_generated': all_designs.filter(execution_type__in=['ai_generated', 'ai_assisted']).count(),
    }

    # حالة الحضور اليوم
    from hr.models import AttendanceRecord, Advance, LeaveRequest
    today_attendance = AttendanceRecord.objects.filter(
        employee=employee, date=today,
    ).first()

    # السلف والإجازات المعلقة
    pending_advances = Advance.objects.filter(employee=employee, status='pending').count()
    pending_leaves = LeaveRequest.objects.filter(employee=employee, status='pending').count()

    # حركات مالية للمصمم (لو عنده صلاحية) — آخر مصروفات/فواتير
    recent_transactions = []
    treasuries = []
    try:
        from printing.models import PrintTransaction, PrintTreasury, PrintOrder
        recent_transactions = list(
            PrintTransaction.objects.filter(created_by=request.user)
                                    .select_related('treasury')
                                    .order_by('-date')[:5]
        )
        treasuries = list(PrintTreasury.objects.filter(is_active=True, kind__in=PrintTreasury.MONEY_KINDS))
        # 🐛 [FIX]: PrintOrder مفيهوش created_by ولا created_at — الاستعلام كان
        #    بيرمي FieldError يتبلع في الـ except فالقسم دايماً فاضي. طلبات
        #    المصمم = الطلبات اللي عليها مهام متسندة له.
        my_recent_orders = list(
            PrintOrder.objects.filter(jobs__designer__user=request.user)
                              .select_related('customer').distinct()
                              .order_by('-date_created')[:5]
        )
    except Exception:
        my_recent_orders = []

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
        'today_attendance': today_attendance,
        'pending_advances': pending_advances,
        'pending_leaves': pending_leaves,
        'recent_transactions': recent_transactions,
        'treasuries': treasuries,
        'my_recent_orders': my_recent_orders,
    })
