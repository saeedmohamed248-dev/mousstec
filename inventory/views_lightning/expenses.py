"""💸 المصروفات: إضافة سريعة، القائمة، التعديل والحذف (وبند المرتبات).

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import (
    Branch,
    EmployeeProfile,
    ExpenseCategory,
    FinancialTransaction,
    Treasury,
)
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required, role_required
from inventory.services.accounting_service import (
    ADVANCE_TAG as _ADVANCE_TAG,
    TRANSFER_TAG as _TRANSFER_TAG,
    is_advance_disbursement,
)
from ._common import _RollbackWith, _can_edit_invoices


# =====================================================================
# 4. QUICK EXPENSE — daily out-of-pocket expense entry
# =====================================================================
# 👥 كلمات دلالية للتعرّف الآلي على بند «المرتبات» — لو المستخدم أضاف بند
# اسمه فيه أي منها، بنفعّل مفتاح 'salaries' اللي بيظهر قائمة الموظفين تلقائياً.
_SALARY_KEYWORDS = ('مرتب', 'رات', 'أجور', 'اجور', 'سلف', 'سلفة', 'معاش', 'salar', 'wage', 'payroll')


def _salary_system_key_for(name):
    """يرجّع 'salaries' لو اسم البند بيدل على مرتبات/أجور/سلف، وإلا ''."""
    low = (name or '').strip().lower()
    return 'salaries' if any(k in low for k in _SALARY_KEYWORDS) else ''


def _get_or_create_expense_category(name):
    """بند مصروف بالاسم — وبيتوسم تلقائياً كـ 'salaries' لو اسمه يدل على مرتبات.

    لو البند موجود من غير مفتاح نظام واسمه يدل على مرتبات، بنحدّثه عشان قائمة
    الموظفين تشتغل من غير ما الأدمن يدخل يظبطه يدوياً.
    """
    category, created = ExpenseCategory.objects.get_or_create(name=name)
    key = _salary_system_key_for(name)
    if key and category.system_key != key:
        category.system_key = key
        category.save(update_fields=['system_key'])
    return category


# 🐛 [FIX]: صفحة المصروف ماكانش عليها أي فحص دور — أمين المخزن/الفني/المراجع
#    كانوا يقدروا يسحبوا فلوس من خزنة الفرع (أو يصرفوا «راتب» لأي حد) لو فتحوا
#    اللينك بإيدهم. نفس الأدوار اللي الداشبورد بيظهر لها «تسجيل مصروف».
_EXPENSE_ROLES = ('admin', 'manager', 'accountant', 'cashier', 'sales')


@login_required(login_url='/login/')
@tenant_required
@role_required(*_EXPENSE_ROLES)
def quick_expense(request):
    branch = _get_branch_for_user(request.user)
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)

    # Salary employees — surfaced when category.system_key == 'salaries'
    emp_qs = (EmployeeProfile.objects
              .select_related('user', 'branch')
              .order_by('user__first_name', 'user__username'))
    if branch is not None:
        emp_qs = emp_qs.filter(Q(branch=branch) | Q(branch__isnull=True))

    return render(request, "inventory/quick_expense.html", {
        "branch": branch,
        "branches": Branch.objects.all() if branch is None else None,
        "treasuries": treasury_qs,
        "categories": ExpenseCategory.objects.all().order_by("name"),
        "salary_employees": emp_qs,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required(*_EXPENSE_ROLES)
@require_POST
def quick_expense_create(request):
    treasury_id = request.POST.get("treasury_id")
    try:
        amount = Decimal(str(request.POST.get("amount") or "0"))
    except InvalidOperation:
        amount = Decimal("0")
    # NaN/Infinity كانوا بيوقّعوا المقارنة بـ 500
    if not amount.is_finite() or amount <= 0:
        return _json_response_safe({"error": "أدخل مبلغاً صحيحاً أكبر من صفر."}, status=400)
    if not treasury_id:
        return _json_response_safe({"error": "اختر الخزنة."}, status=400)
    description = (request.POST.get("description") or "").strip() or "مصروف يومي"
    category_id = request.POST.get("category_id") or None
    employee_id = request.POST.get("employee_id") or None

    try:
        with transaction.atomic():
            treasury = (Treasury.objects.select_for_update()
                        .filter(id=treasury_id, is_active=True).first())
            if treasury is None:
                raise _RollbackWith(_json_response_safe({"error": "الخزنة غير موجودة."}, status=404))
            if not _user_can_edit_branch(request.user, treasury.branch):
                raise _RollbackWith(_json_response_safe({"error": "👁 صلاحيتك في فرع الخزنة دي عرض فقط — مش مسموح بالصرف."}, status=403))
            if (treasury.balance or Decimal("0")) < amount:
                raise _RollbackWith(_json_response_safe({
                    "error": f"رصيد الخزنة غير كافٍ (متاح: {treasury.balance})."
                }, status=409))
            # 🏷️ بند جديد: لو المستخدم اختار «➕ بند جديد» وكتب اسم، نحفظه (أو
            # نستخدم الموجود بنفس الاسم) — عشان يقدر يضيف بنود من غير الأدمن.
            new_category_name = (request.POST.get("new_category") or "").strip()[:100]
            if category_id == "__new__" or (new_category_name and not str(category_id or "").isdigit()):
                if not new_category_name:
                    raise _RollbackWith(_json_response_safe({"error": "اكتب اسم البند الجديد."}, status=400))
                category = _get_or_create_expense_category(new_category_name)
            else:
                category = ExpenseCategory.objects.filter(id=category_id).first() if category_id else None

            # 👥 If category is 'salaries', require an employee link
            employee = None
            if category and category.system_key == 'salaries':
                if not employee_id:
                    raise _RollbackWith(_json_response_safe({
                        "error": "اختر الموظف المستلم للراتب."
                    }, status=400))
                employee = EmployeeProfile.objects.filter(id=employee_id).first()
                if employee is None:
                    raise _RollbackWith(_json_response_safe({"error": "الموظف غير موجود."}, status=404))
                # Stamp the description for ledger clarity
                description = f"{description} — {employee.user.get_full_name() or employee.user.username}"

            tx = FinancialTransaction.objects.create(
                treasury=treasury,
                transaction_type="out",
                amount=amount,
                description=description,
                category=category,
                employee=employee,
            )
        treasury.refresh_from_db(fields=["balance"])
        return _json_response_safe({
            "ok": True,
            "transaction_id": tx.id,
            "new_balance": float(treasury.balance),
            "treasury_id": treasury.id,
        })
    except _RollbackWith as rb:
        # 🐛 [FIX]: كان return من جوه atomic — البند الجديد كان بيتحفظ حتى لو
        #    المصروف نفسه اترفض (مثلاً «اختر الموظف»).
        return rb.response
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل تسجيل المصروف: {exc}"}, status=500)


# =====================================================================
# 💸 إدارة المصاريف — عرض / تعديل الخزنة / حذف
# =====================================================================
def _is_operating_expense(ft):
    """المصروف التشغيلي = سحب (out) مش مرتبط بفاتورة/مورد/عميل ولا تحويل بين خزائن."""
    return (ft.transaction_type == 'out'
            and ft.sale_invoice_id is None
            and ft.purchase_invoice_id is None
            and ft.vendor_id is None
            and ft.customer_id is None
            and not ft.equity_kind   # مسحوبات المالك حقوق ملكية مش مصروف
            and not (ft.description or "").startswith(_TRANSFER_TAG)
            and not is_advance_disbursement(ft))   # سلفة الموظف أصل مش مصروف


def _delete_expense_ft(ft):
    """يحذف حركة مصروف/حركة يدوية + يشيل قيودها المحاسبية.

    🛡️ رصيد الخزنة بيترجّع تلقائياً عبر signal post_delete
    (reverse_balance_on_delete) — فمابنعدّلوش يدوياً هنا عشان ما يترجعش مرتين.
    """
    # قيود الحركة بتتشال (أو تتعكس لو فترتها مقفولة) في signal pre_delete.
    ft.delete()  # الـ signal بيرجّع رصيد الخزنة


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('expenses')
def expense_list(request):
    """قائمة المصاريف التشغيلية للفرع النشط مع تعديل/حذف."""
    branch = _get_branch_for_user(request.user)
    qs = (FinancialTransaction.objects
          .filter(transaction_type='out', sale_invoice__isnull=True,
                  purchase_invoice__isnull=True, vendor__isnull=True,
                  customer__isnull=True, equity_kind='')   # المسحوبات مش مصروف
          .exclude(description__startswith=_TRANSFER_TAG)  # التحويلات وسداد الموردين مش مصاريف
          .exclude(employee__isnull=False, description__startswith=_ADVANCE_TAG)  # سلف الموظفين أصل
          .select_related('treasury', 'treasury__branch', 'category', 'employee__user')
          .order_by('-date', '-id'))
    if branch is not None:
        qs = qs.filter(treasury__branch=branch)

    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(category__name__icontains=q))

    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    total = qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    return render(request, 'inventory/expense_list.html', {
        'page': page, 'q': q, 'branch': branch, 'total': total,
        'can_edit': _can_edit_invoices(request.user),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
def expense_edit(request, pk):
    """تعديل مصروف — تقدر تغيّر الخزنة والمبلغ والبيان والبند.

    التغيير بيتم بحذف الحركة القديمة (مع إرجاع رصيد خزنتها) وإنشاء حركة
    جديدة على الخزنة المختارة — فالرصيد بيتظبط صح على الخزنتين.
    """
    ft = (FinancialTransaction.objects
          .select_related('treasury', 'treasury__branch', 'category').filter(pk=pk).first())
    if not ft or not _is_operating_expense(ft):
        return redirect(f"{reverse('inventory:expense_list')}?err=notfound")
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, ft.treasury.branch)):
        return redirect(f"{reverse('inventory:expense_list')}?err=perm")
    # صرف العمولات مربوط برصيد عمولات الموظف — تعديل مبلغه من هنا كان بيفكّ
    # الربط (الرصيد مايتظبطش). يتلغي (حذف) ويتصرف تاني من شاشة العمولات.
    from inventory.services.accounting_service import is_commission_payout
    if is_commission_payout(ft):
        return redirect(f"{reverse('inventory:expense_list')}?err=payout")

    branch = ft.treasury.branch
    treasuries = Treasury.objects.filter(is_active=True, branch=branch).order_by('name')

    if request.method == 'POST':
        new_tid = request.POST.get('treasury_id')
        try:
            new_amount = Decimal(str(request.POST.get('amount') or '0'))
        except InvalidOperation:
            new_amount = Decimal('0')
        new_desc = (request.POST.get('description') or '').strip() or ft.description
        new_cat_id = request.POST.get('category_id') or None
        if not new_amount.is_finite() or new_amount <= 0:
            return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err=amount")
        new_treasury = Treasury.objects.filter(id=new_tid, is_active=True, branch=branch).first()
        if new_treasury is None:
            return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err=treasury")
        if not _user_can_edit_branch(request.user, new_treasury.branch):
            return redirect(f"{reverse('inventory:expense_list')}?err=perm")
        try:
            with transaction.atomic():
                # 🔒 قفل الخزنة عشان مصروفين في نفس اللحظة مايعدّوش فحص الرصيد سوا
                new_treasury = Treasury.objects.select_for_update().get(pk=new_treasury.pk)
                # الرصيد المتاح على الخزنة الجديدة بعد إرجاع القديمة (لو نفس الخزنة)
                available = new_treasury.balance or Decimal('0')
                if new_treasury.pk == ft.treasury_id:
                    available += ft.amount  # هيترجّع أول ما نحذف القديمة
                if available < new_amount:
                    return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err=balance")
                category = ExpenseCategory.objects.filter(id=new_cat_id).first() if new_cat_id else ft.category
                employee = ft.employee
                # 🐛 [FIX]: التعديل بيحذف ويعيد الإنشاء — من غير تمرير التاريخ
                #    المصروف القديم كان بيتنقل لتاريخ النهارده (يبوّظ تقارير
                #    الشهر). بنحافظ على تاريخه الأصلي.
                original_date = ft.date
                _delete_expense_ft(ft)
                FinancialTransaction.objects.create(
                    treasury=new_treasury, transaction_type='out', amount=new_amount,
                    description=new_desc, category=category, employee=employee,
                    date=original_date,
                )
        except Exception as exc:
            from django.core.exceptions import ValidationError as _VE
            code = 'closed' if isinstance(exc, _VE) else 'fail'  # فترة محاسبية مقفولة
            return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err={code}")
        return redirect(f"{reverse('inventory:expense_list')}?ok=edited")

    return render(request, 'inventory/expense_edit.html', {
        'ft': ft, 'treasuries': treasuries, 'branch': branch,
        'categories': ExpenseCategory.objects.all().order_by('name'),
        'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@require_POST
def expense_delete(request, pk):
    """🗑️ حذف مصروف مع إرجاع قيمته لرصيد الخزنة."""
    ft = FinancialTransaction.objects.select_related('treasury__branch').filter(pk=pk).first()
    if not ft or not _is_operating_expense(ft):
        return redirect(f"{reverse('inventory:expense_list')}?err=notfound")
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, ft.treasury.branch)):
        return redirect(f"{reverse('inventory:expense_list')}?err=perm")
    try:
        with transaction.atomic():
            _delete_expense_ft(ft)
    except Exception:
        return redirect(f"{reverse('inventory:expense_list')}?err=fail")
    return redirect(f"{reverse('inventory:expense_list')}?ok=deleted")
