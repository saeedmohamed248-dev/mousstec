"""👥 العملاء (الآجل): كشف الحساب، التحصيل، إلغاء دفعة.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from urllib.parse import quote
from django.views.decorators.http import require_POST
from inventory.models import Customer, FinancialTransaction, SaleInvoice, Treasury
from inventory.views import _get_branch_for_user, _user_can_edit_branch, tenant_required
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from ._common import _ar_field_expr, _can_edit_invoices, _is_finance_manager, _norm_ar


# =====================================================================
# 👥 كشف حساب العملاء (الآجل) + تحصيل الدفعات
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant', 'cashier')
@module_required('customers')
def customers_receivables(request):
    """قائمة العملاء وأرصدتهم (الآجل) — مين عليه فلوس وكام، مع بحث وإجمالي.

    🐛 [FIX]: الصفحة كانت بتعرض رصيد العميل على مستوى الشركة كلها حتى لو مختار
    فرع معيّن من المبدّل — فآجل فرع مصر بيظهر في فرع الإمارات. دلوقتي لو فيه فرع
    نشط، العملاء = اللي ليهم فواتير في الفرع ده، والرصيد = المتبقّي من فواتير
    الفرع ده بس (بعد المرتجعات). من غير فرع (الأدمن على «كل الفروع») زي الأول.
    """
    branch = _get_branch_for_user(request.user)
    qs = Customer.objects.all()
    q = (request.GET.get('q') or '').strip()
    if q:
        # 🔎 بحث عربي مُطبَّع (زي بحث العملاء في أمر الشغل): «محمد» تلاقي «محمّد»
        qs = (qs.annotate(_nname=_ar_field_expr('name'))
              .filter(Q(_nname__contains=_norm_ar(q)) | Q(name__icontains=q) | Q(phone__icontains=q)))
    flt = (request.GET.get('filter') or 'debt').strip()

    if branch is not None:
        rows, total_debt, total_credit = _branch_customer_balances(qs, branch, flt)
    else:
        if flt == 'debt':
            qs = qs.filter(balance__gt=0)
        elif flt == 'credit':
            qs = qs.filter(balance__lt=0)
        rows = qs.order_by('-balance', 'name')
        # إجمالي المديونية (كل العملاء اللي عليهم) — مش متأثر بالفلتر/البحث
        total_debt = Customer.objects.filter(balance__gt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')
        total_credit = Customer.objects.filter(balance__lt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')

    _exp = export_report(
        request, "customers", "العملاء والآجل", None,
        [{
            "columns": ["العميل", "الهاتف", "الرصيد"],
            "rows": [[c.name, c.phone, c.balance] for c in rows[:5000]],
        }])
    if _exp:
        return _exp

    page = Paginator(rows, 40).get_page(request.GET.get('page'))
    return render(request, 'inventory/customers_list.html', {
        'page': page, 'q': q, 'filter': flt, 'branch': branch,
        'total_debt': total_debt, 'total_credit': abs(total_credit),
    })


def _branch_customer_balances(qs, branch, flt):
    """(عملاء الفرع برصيدهم في الفرع، إجمالي عليهم، إجمالي ليهم).

    الرصيد في الفرع = مجموع open_due لفواتير العميل في الفرع (غير عروض الأسعار
    والمرتجعات). الرصيد الدائن (ليهم) مش متقسّم على الفروع في الدفاتر، فبيتعرض
    بالرصيد العام للعملاء اللي اتعاملوا مع الفرع ده.
    """
    invoices = (SaleInvoice.objects.filter(branch=branch, customer__in=qs)
                .exclude(status='quotation').select_related('maintenance_contract')
                .prefetch_related('return_invoices'))
    due = {}
    seen = set()
    for inv in invoices:
        seen.add(inv.customer_id)
        if inv.is_return:
            continue
        d = inv.open_due
        if d > 0:
            due[inv.customer_id] = due.get(inv.customer_id, Decimal('0')) + d
    customers = list(Customer.objects.filter(pk__in=seen))
    total_debt = sum(due.values(), Decimal('0'))
    total_credit = sum((c.balance for c in customers if c.balance < 0), Decimal('0'))
    rows = []
    for c in customers:
        if c.pk in due:
            c.balance = due[c.pk]          # للعرض بس — مابيتحفظش
        elif c.balance > 0:
            c.balance = Decimal('0')       # مديونيته من فرع تاني
        if flt == 'debt' and c.balance <= 0:
            continue
        if flt == 'credit' and c.balance >= 0:
            continue
        rows.append(c)
    rows.sort(key=lambda c: (-c.balance, c.name))
    return rows, total_debt, total_credit


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant', 'cashier')
def customer_detail(request, pk):
    """🧾 كشف حساب عميل — فواتيره اللي عليها متبقي + دفعات التحصيل + رصيده."""
    customer = Customer.objects.filter(pk=pk).first()
    if not customer:
        return redirect(f"{reverse('inventory:customers_receivables')}?err=notfound")

    invoices = (SaleInvoice.objects.select_related('branch')
                .filter(customer=customer).exclude(status='quotation')
                .prefetch_related('return_invoices')
                .order_by('-date_created'))
    # فواتير المرتجع مش «متبقّي على العميل» — كانت بتظهر هنا بإجماليها كأنها دين،
    # والمتبقّي بعد خصم مرتجعات الفاتورة (open_due)
    open_invoices = [inv for inv in invoices
                     if not inv.is_return and inv.open_due > Decimal('0.00')]

    # دفعات التحصيل (in) + المبالغ اللي رجعت للعميل (out، زي رد فلوس مرتجع)
    # — من غير الـ out الكشف كان بيبان ناقص قدام الرصيد.
    payments = (FinancialTransaction.objects.select_related('treasury')
                .filter(customer=customer, transaction_type__in=('in', 'out'))
                .order_by('-date', '-id')[:100])

    branch = _get_branch_for_user(request.user)
    treasuries = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)

    # ↩️ التحصيلات اللي ينفع تتلغي (تحصيل غلط) — للأدمن/المدير/المحاسب بس
    from inventory.services.customer_payments import can_void
    payments = list(payments.select_related('customer', 'sale_invoice'))
    can_manage = _is_finance_manager(request.user)
    for p in payments:
        p.voidable = can_manage and can_void(p)[0]

    return render(request, 'inventory/customer_statement.html', {
        'customer': customer,
        'open_invoices': open_invoices,
        'invoices': invoices[:50],
        'payments': payments,
        'treasuries': treasuries.select_related('branch').order_by('branch__name', 'name'),
        'can_collect': _can_edit_invoices(request.user) or (
            getattr(getattr(request.user, 'employee_profile', None), 'role', '') == 'cashier'),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
        'msg': request.GET.get('msg', ''), 'amt': request.GET.get('amt', ''),
        # ✏️ تعديل بيانات العميل من البوابة (حد الائتمان للإدارة المالية بس)
        'can_edit_customer': _can_edit_invoices(request.user) and not customer.is_walk_in,
        'can_set_credit': can_manage,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant', 'cashier')
@require_POST
def customer_edit(request, pk):
    """✏️ بيانات العميل من البوابة: الاسم والموبايل والرقم الضريبي والقومي والعنوان (للفاتورة
    الإلكترونية)، وحد الائتمان للأدمن/المدير/المحاسب بس. قبل كده كله كان من الأدمن بس.
    «عميل نقدي» مشترك بين ناس كتير — مابيتعدّلش."""
    back = reverse('inventory:customer_detail', args=[pk])
    customer = Customer.objects.filter(pk=pk).first()
    if customer is None:
        return redirect(f"{reverse('inventory:customers_receivables')}?err=notfound")
    if customer.is_walk_in or not _can_edit_invoices(request.user):
        return redirect(f"{back}?err=perm")
    P = request.POST
    name = (P.get('name') or '').strip()[:200]
    phone = Customer.normalize_phone((P.get('phone') or '').strip())
    if not name or not phone:
        return redirect(f"{back}?err=msg&msg=" + quote('الاسم والموبايل مطلوبين.'))
    if Customer.objects.filter(phone=phone).exclude(pk=customer.pk).exists():
        return redirect(f"{back}?err=msg&msg=" + quote('الموبايل ده متسجّل لعميل تاني.'))
    customer.name, customer.phone = name, phone
    customer.tax_id = (P.get('tax_id') or '').strip()[:50] or None
    customer.national_id = (P.get('national_id') or '').strip()[:20]
    for field in ('eta_governate', 'eta_region_city', 'eta_street', 'eta_building_number'):
        setattr(customer, field, (P.get(field) or '').strip()[:Customer._meta.get_field(field).max_length])
    fields = ['name', 'phone', 'tax_id', 'national_id', 'eta_governate', 'eta_region_city',
              'eta_street', 'eta_building_number']
    if _is_finance_manager(request.user) and 'credit_limit' in P:
        try:
            limit = Decimal((P.get('credit_limit') or '0').strip() or '0')
        except InvalidOperation:
            return redirect(f"{back}?err=msg&msg=" + quote('حد الائتمان لازم يكون رقم.'))
        if limit < 0 or not limit.is_finite():
            return redirect(f"{back}?err=msg&msg=" + quote('حد الائتمان لازم يكون صفر أو أكتر.'))
        customer.credit_limit = limit.quantize(Decimal('0.01'))
        fields.append('credit_limit')
    customer.save(update_fields=fields)
    return redirect(f"{back}?ok=edited")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant', 'cashier')
@require_POST
def customer_collect(request, pk):
    """💵 تحصيل دفعة من العميل على حسابه (الآجل) — يقلّل رصيده ويدخل الفلوس الخزنة."""
    customer = Customer.objects.filter(pk=pk).first()
    if not customer:
        return redirect(f"{reverse('inventory:customers_receivables')}?err=notfound")
    treasury = Treasury.objects.filter(id=request.POST.get('treasury_id'), is_active=True).first()
    if treasury is None:
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=treasury")
    if not _user_can_edit_branch(request.user, treasury.branch):
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=perm")
    try:
        amount = Decimal(str(request.POST.get('amount') or '0'))
    except InvalidOperation:
        amount = Decimal('0')
    if not amount.is_finite() or amount <= 0:   # NaN كان بيوقّع الصفحة بـ 500
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=amount")
    if customer.is_walk_in:
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=walkin")
    note = (request.POST.get('note') or '').strip()[:120]
    from inventory.services.customer_payments import collect
    # 🐛 [FIX]: الحد الأقصى كان في المتصفح بس — تحصيل 13500 بدل 1350 كان
    #    بيدخل الخزنة فلوس وهمية ويعمل للعميل رصيد دائن. الخدمة بتقفل صف العميل
    #    (ضغطتين ورا بعض مايعدّوش الفحص سوا) وبترفض أي مبلغ أكبر من المديونية،
    #    وبتوزّع التحصيل على فواتيره المفتوحة من الأقدم (زي سداد الموردين).
    from django.core.exceptions import ValidationError
    try:
        collect(customer, amount, treasury, note)
    except ValidationError:   # المبلغ أكبر من المديونية (الوحيد اللي ممكن يوصل هنا)
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=over")
    return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?ok=collected")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def customer_payment_void(request, pk, ft_id):
    """↩️ إلغاء تحصيل غلط من العميل — الفلوس تخرج من الخزنة والمبلغ يرجع عليه."""
    from urllib.parse import urlencode
    from django.core.exceptions import ValidationError
    from inventory.services.customer_payments import void_collection
    back = reverse('inventory:customer_detail', args=[pk])
    ft = (FinancialTransaction.objects.select_related('treasury__branch', 'customer', 'sale_invoice')
          .filter(pk=ft_id, customer_id=pk).first())
    if ft is None:
        return redirect(f"{back}?err=notfound")
    if not _user_can_edit_branch(request.user, ft.treasury.branch):
        return redirect(f"{back}?err=perm")
    try:
        amount = void_collection(ft)
    except ValidationError as exc:
        return redirect(f"{back}?{urlencode({'err': 'msg', 'msg': exc.messages[0]})}")
    _audit_payment_void(request, 'customer_payment', ft_id, amount, ft.description)
    return redirect(f"{back}?{urlencode({'ok': 'voided', 'amt': amount})}")


def _audit_payment_void(request, kind, ft_id, amount, description):
    """سجل مراجعة لإلغاء الدفعات (مين لغى إيه وإمتى ومن أي IP)."""
    try:
        from inventory.models import AuditLog
        AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action='delete', model_name=kind, object_id=str(ft_id),
            object_repr=(description or '')[:255],
            changes_json={'voided_amount': str(amount)},
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                        or request.META.get('REMOTE_ADDR')) or None)
    except Exception:  # noqa: BLE001 — السجل مايوقفش العملية
        pass
