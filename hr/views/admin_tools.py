"""⚙️ شاشات الموارد البشرية — تفعيل/إلغاء اشتراك الذكاء الاصطناعي والحركة السريعة على الخزنة."""

import json
import logging
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from erp_core.localization import current_tenant_symbol as _sym
from ._common import _json_response

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 7. Admin AI Subscription Management APIs
# =====================================================================

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
        # 🛡️ Tenant isolation: only fetch employees in the CURRENT tenant schema
        from django.db import connection
        designer = Employee.objects.get(pk=employee_id, is_active=True)
        # Double-check we're in the same schema (multi-tenant safety)
        if connection.schema_name == 'public' and not request.user.is_superuser:
            return _json_response({"error": "ليس لديك صلاحية."}, 403)
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


# =====================================================================
# 8. Quick Treasury Transaction (Designer dashboard quick actions)
# =====================================================================

@login_required(login_url='/secure-portal/')
@require_POST
def api_quick_treasury_transaction(request):
    """POST /hr/api/treasury/quick-transaction/

    Body: { "treasury_id": 1, "amount": 250.50, "description": "...",
            "transaction_type": "in" | "out" }

    تسجيل سريع لمصروف/إيراد من داش بورد المصمم. بيتحقق من صلاحية الموظف
    (can_manage_treasury أو superuser) قبل ما يسجّل الحركة.
    """
    try:
        from printing.models import PrintTreasury, PrintTransaction, StaffPermission
    except Exception:
        return _json_response({"error": "وحدة المالية غير متاحة."}, 400)

    # صلاحية: superuser أو staff_permission.can_manage_treasury
    user = request.user
    if not user.is_superuser:
        try:
            perm = StaffPermission.objects.get(user=user)
            if not perm.can_manage_treasury:
                return _json_response({"error": "ليس لديك صلاحية إدارة الخزينة."}, 403)
        except StaffPermission.DoesNotExist:
            return _json_response({"error": "ليس لديك صلاحية إدارة الخزينة."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    treasury_id = data.get('treasury_id')
    amount_raw = data.get('amount')
    description = (data.get('description') or '').strip()
    txn_type = data.get('transaction_type')

    if not treasury_id or amount_raw is None or not description or txn_type not in ('in', 'out'):
        return _json_response({"error": "كل الحقول مطلوبة."}, 400)

    try:
        amount = Decimal(str(amount_raw))
        if amount <= 0:
            raise ValueError
    except (ValueError, ArithmeticError, TypeError):
        return _json_response({"error": "مبلغ غير صالح."}, 400)

    try:
        treasury = PrintTreasury.objects.get(pk=int(treasury_id), is_active=True,
                                             kind__in=PrintTreasury.MONEY_KINDS)
    except PrintTreasury.DoesNotExist:
        return _json_response({"error": "الخزينة غير موجودة."}, 404)

    # 🐛 [FIX]: التحصيل من هنا ماكانش بيتربط بأي طلب — فالعميل يفضل «عليه» في
    # كشف حسابه والمستحقات رغم إنه دفع. رقم الطلب اختياري ويربط الدفعة بيه.
    order = None
    order_number = (data.get('order_number') or '').strip().lstrip('#')
    if order_number:
        from printing.models import PrintOrder
        order = PrintOrder.objects.filter(order_number__iexact=order_number).first()
        if order is None:
            return _json_response({"error": f"مفيش طلب برقم {order_number}."}, 404)

    try:
        txn = PrintTransaction.objects.create(
            treasury=treasury,
            transaction_type=txn_type,
            amount=amount,
            description=description[:300],
            order=order,
            created_by=user,
        )
    except ValueError as e:
        # رصيد الخزنة لا يكفي (raised من PrintTransaction.save)
        return _json_response({"error": str(e)}, 400)
    except Exception as e:
        logger.error(f"[QUICK TXN] failed for user={user.id}: {e}")
        return _json_response({"error": "فشل تسجيل الحركة."}, 500)

    icon = "🟢 إيراد" if txn_type == 'in' else "🔴 مصروف"
    return _json_response({
        "success": True,
        "message": f"تم تسجيل {icon} بقيمة {amount:,.2f} {_sym()}.",
        "transaction_id": txn.pk,
    })
