"""🚚 الموردين: المستحقات، السداد، إصلاح الرصيد، الرصيد الدائن.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import FinancialTransaction, PurchaseInvoice, Treasury, Vendor
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from ._common import _can_bulk_delete_products, _can_edit_invoices, _finite_dec, _is_finance_manager
from .customers import _audit_payment_void


# =====================================================================
# 🚚 الموردون (SRM) — كشف حساب المستحقات + سداد
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('vendors')
def vendors_payables(request):
    """قائمة الموردين وأرصدتهم (اللي علينا) — مع بحث وإجمالي المستحقات."""
    qs = Vendor.objects.all()
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    # 🐛 [FIX]: الافتراضي كان «علينا لهم فلوس» بس — المورد الجديد (رصيده صفر)
    #    مكانش بيظهر خالص بعد إضافته («لا يوجد موردون»). الكل، والمديونين الأول.
    flt = (request.GET.get('filter') or 'all').strip()
    if flt == 'debt':
        qs = qs.filter(balance__gt=0)
    elif flt == 'credit':
        qs = qs.filter(balance__lt=0)
    qs = qs.order_by('-balance', 'name')

    total_debt = Vendor.objects.filter(balance__gt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')
    total_credit = Vendor.objects.filter(balance__lt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')

    _exp = export_report(
        request, "vendors", "الموردون والمستحقات", None,
        [{
            "columns": ["المورد", "الهاتف", "الرصيد"],
            "rows": [[v.name, v.phone or "", v.balance] for v in qs[:5000]],
        }])
    if _exp:
        return _exp

    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    from inventory.services.vendor_payments import balance_mismatches
    return render(request, 'inventory/vendors_list.html', {
        'page': page, 'q': q, 'filter': flt,
        'total_debt': total_debt, 'total_credit': abs(total_credit),
        'mismatches': balance_mismatches(),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('vendors')
@require_POST
def vendor_create(request):
    """➕ إنشاء مورد جديد بسرعة (AJAX) — من صفحة المشتريات أو قائمة الموردين.

    يقبل JSON أو form: name (مطلوب)، phone، tax_id، company_details.
    يرجّع {ok, id, name} عشان الواجهة تضيفه للقائمة وتختاره فوراً.
    """
    import json as _json
    if request.content_type and 'application/json' in request.content_type:
        try:
            data = _json.loads(request.body or b'{}')
        except (ValueError, TypeError):
            return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)
    else:
        data = request.POST

    name = (data.get('name') or '').strip()
    if not name:
        return _json_response_safe({"error": "اكتب اسم المورد."}, status=400)

    phone = (data.get('phone') or '').strip() or None
    # امنع التكرار: نفس الاسم (أو نفس التليفون لو مكتوب) = المورد موجود بالفعل
    existing = Vendor.objects.filter(name__iexact=name).first()
    if existing is None and phone:
        existing = Vendor.objects.filter(phone=phone).first()
    if existing is not None:
        return _json_response_safe(
            {"error": f"المورد '{existing.name}' موجود بالفعل.",
             "id": existing.id, "name": existing.name, "duplicate": True},
            status=409)

    vendor = Vendor.objects.create(
        name=name,
        phone=phone,
        tax_id=(data.get('tax_id') or '').strip() or None,
        company_details=(data.get('company_details') or '').strip() or None,
    )
    return _json_response_safe({"ok": True, "id": vendor.id, "name": vendor.name})


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def vendor_detail(request, pk):
    """🧾 كشف حساب مورد — فواتير الشراء اللي عليها متبقّي + دفعاته + رصيده."""
    from inventory.services.vendor_payments import expected_balances
    vendor = Vendor.objects.filter(pk=pk).first()
    if not vendor:
        return redirect(f"{reverse('inventory:vendors_payables')}?err=notfound")

    invoices = (PurchaseInvoice.objects.select_related('branch')
                .filter(vendor=vendor).order_by('-date_created'))
    open_invoices = [inv for inv in invoices if inv.net_due > Decimal('0.00')]

    payments = list(FinancialTransaction.objects.select_related('treasury', 'purchase_invoice')
                    .filter(vendor=vendor, transaction_type='out')
                    .order_by('-date', '-id')[:100])
    # ↩️ الدفعات اللي ينفع تتلغي (سداد غلط) — للأدمن/المدير/المحاسب بس
    from inventory.services.vendor_payments import can_void
    can_manage = _is_finance_manager(request.user)
    for p in payments:
        p.voidable = can_manage and can_void(p)[0]

    branch = _get_branch_for_user(request.user)
    treasuries = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)

    # نجهّز المتبقّي لكل فاتورة للعرض (المسودة مالهاش أثر على الرصيد لسه)
    open_rows = [{"inv": inv, "due": inv.net_due} for inv in open_invoices]
    open_due = sum((r["due"] for r in open_rows if r["inv"].is_applied), Decimal('0.00'))
    expected = expected_balances([vendor.pk]).get(vendor.pk, Decimal('0.00'))
    balance = Decimal(str(vendor.balance or 0)).quantize(Decimal('0.01'))

    # 💳 لو لينا رصيد عند المورد (مرتجع من غير فلوس/دفعة مقدّمة) المطلوب فعلاً أقل
    #    من المتبقّي على الفواتير — الفورم كان بيقترح المتبقّي كله فبنزوّد في الدفع.
    suggested = max(min(open_due, balance), Decimal('0.00')) if open_due > 0 else max(balance, Decimal('0.00'))
    vendor_credit = max(open_due - balance, Decimal('0.00')) if open_due > 0 else Decimal('0.00')
    return render(request, 'inventory/vendor_statement.html', {
        'vendor': vendor,
        'open_rows': open_rows, 'open_due': open_due,
        'suggested_pay': suggested, 'vendor_credit': vendor_credit,
        'payments': payments,
        'treasuries': treasuries.select_related('branch').order_by('branch__name', 'name'),
        'can_pay': _can_edit_invoices(request.user),
        'can_fix': _can_bulk_delete_products(request.user),   # أدمن/مدير
        'expected_balance': expected,
        'mismatch': abs(expected - balance) >= Decimal('0.01'),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
        'amt': request.GET.get('amt'), 'msg': request.GET.get('msg'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def vendor_pay(request, pk):
    """💸 دفعة على حساب المورد — بتسدّد فواتيره المفتوحة من الأقدم، والباقي مقدّم."""
    from urllib.parse import urlencode
    from django.core.exceptions import ValidationError
    from inventory.services.vendor_payments import pay_vendor
    vendor = Vendor.objects.filter(pk=pk).first()
    if not vendor:
        return redirect(f"{reverse('inventory:vendors_payables')}?err=notfound")
    back = reverse('inventory:vendor_detail', args=[pk])
    treasury = Treasury.objects.filter(id=request.POST.get('treasury_id'), is_active=True).first()
    if treasury is None:
        return redirect(f"{back}?err=treasury")
    if not _user_can_edit_branch(request.user, treasury.branch):
        return redirect(f"{back}?err=perm")
    amount = _finite_dec(request.POST.get('amount'), Decimal('0'))
    if amount <= 0:
        return redirect(f"{back}?err=amount")
    try:
        allocated, advance = pay_vendor(vendor, amount, treasury, (request.POST.get('note') or '').strip())
    except ValidationError as exc:
        return redirect(f"{back}?{urlencode({'err': 'msg', 'msg': exc.messages[0]})}")
    msg = '، '.join(f"#{inv.id}: {part}" for inv, part in allocated)
    if advance > 0:
        msg = (msg + '، ' if msg else '') + f"مقدّم: {advance}"
    return redirect(f"{back}?{urlencode({'ok': 'paid', 'amt': amount, 'msg': msg})}")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
@require_POST
def purchase_pay(request, pk):
    """💸 دفعة على فاتورة شراء (جزء من المتبقّي أو المبلغ كامل) من خزنة."""
    from urllib.parse import urlencode
    from django.core.exceptions import ValidationError
    from django.utils.http import url_has_allowed_host_and_scheme
    from inventory.services.vendor_payments import pay_invoice
    nxt = request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = reverse('inventory:purchase_list')
    sep = '&' if '?' in nxt else '?'
    branch = _get_branch_for_user(request.user)
    inv = PurchaseInvoice.objects.select_related('vendor').filter(pk=pk).first()
    if inv is None or (branch is not None and inv.branch_id != branch.id):
        return redirect(f"{nxt}{sep}{urlencode({'err': 'الفاتورة مش موجودة.'})}")
    if not _user_can_edit_branch(request.user, inv.branch):
        return redirect(f"{nxt}{sep}{urlencode({'err': 'صلاحيتك عرض فقط على فرع الفاتورة دي.'})}")
    treasury = Treasury.objects.filter(id=request.POST.get('treasury_id'), is_active=True).first()
    if treasury is None:
        return redirect(f"{nxt}{sep}{urlencode({'err': 'اختار الخزنة اللي هتدفع منها.'})}")
    if not _user_can_edit_branch(request.user, treasury.branch):
        return redirect(f"{nxt}{sep}{urlencode({'err': 'صلاحيتك عرض فقط على فرع الخزنة دي.'})}")
    if request.POST.get('full') == '1':
        amount = inv.net_due
    else:
        amount = _finite_dec(request.POST.get('amount'), Decimal('0'))
    try:
        paid = pay_invoice(inv, amount, treasury, (request.POST.get('note') or '').strip())
    except ValidationError as exc:
        return redirect(f"{nxt}{sep}{urlencode({'err': exc.messages[0]})}")
    return redirect(f"{nxt}{sep}{urlencode({'ok': 'paid', 'amt': paid, 'inv': inv.id})}")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
@module_required('purchases')
@require_POST
def purchase_repair(request, pk):
    """🩹 يرجّع بضاعة فاتورة شراء اتعدّلت ومخزونها ماترجعش (قبل إصلاح خطأ التعديل)."""
    from urllib.parse import urlencode
    from inventory.services.purchase_repair import broken_purchase_edits, repair_purchase
    branch = _get_branch_for_user(request.user)
    back = reverse('inventory:purchase_list')
    row = next((r for r in broken_purchase_edits(branch) if r['invoice'].pk == pk), None)
    if row is None:
        return redirect(f"{back}?{urlencode({'err': 'الفاتورة دي مش محتاجة إصلاح.'})}")
    if not _user_can_edit_branch(request.user, row['invoice'].branch):
        return redirect(f"{back}?err=perm")
    try:
        repair_purchase(row['invoice'])
    except Exception as exc:  # noqa: BLE001
        return redirect(f"{back}?{urlencode({'err': f'تعذّر الإصلاح: {exc}'})}")
    return redirect(f"{back}?{urlencode({'ok': 'repaired', 'amt': row['missing'], 'inv': pk})}")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
def purchase_recost(request, pk):
    """🔁 إعادة توزيع مصاريف الوصول على أصناف فاتورة معتمدة: بالقيمة (نسبة
    ثابتة) أو حسب ربح كل صنف (سعر البيع − سعر الشراء). سعر البيع بيتعدّل من
    نفس الصفحة."""
    from urllib.parse import urlencode
    from inventory.services.landed_allocation import recost_blockers, recost_purchase, recost_rows
    branch = _get_branch_for_user(request.user)
    inv = PurchaseInvoice.objects.filter(pk=pk).select_related('vendor', 'branch').first()
    if inv is None:
        return redirect(reverse('inventory:purchase_list') + '?err=notfound')
    if branch is not None and inv.branch_id != branch.id:
        return redirect(reverse('inventory:purchase_list') + '?err=branch')
    if not _user_can_edit_branch(request.user, inv.branch):
        return redirect(reverse('inventory:purchase_list') + '?err=perm')
    method, sells, manual, error = inv.cost_allocation, {}, None, None
    if request.method == 'POST':
        method = 'margin' if request.POST.get('method') == 'margin' else 'value'
        for key, val in request.POST.items():
            if not key.startswith('sell_'):
                continue
            price = _finite_dec(val)
            if not key[5:].isdecimal() or price is None:
                continue
            if price > 0:
                sells[int(key[5:])] = price
        # ✍️ مصاريف القطعة يدوي لكل بند (فاضي = تلقائي)
        manual = {}
        for it in inv.items.all():
            raw = (request.POST.get(f'extra_{it.pk}') or '').strip()
            manual[it.pk] = _finite_dec(raw)
            if raw and manual[it.pk] is None:
                error = "مصاريف القطعة لازم تبقى رقم."
        try:
            if error:
                raise ValueError(error)
            res = recost_purchase(inv, method, sells, manual)
        except ValueError as exc:
            error = str(exc)
        else:
            return redirect(f"{reverse('inventory:purchase_list')}?"
                            f"{urlencode({'ok': 'recosted', 'inv': pk, 'm': res['used']})}")
    rows, _used = recost_rows(inv, method, sells, manual)
    lines = [{'qty': r['qty'], 'cost': float(r['cost']), 'sell': float(r['sell']),
              'old': float(r['old_unit'])} for r in rows]
    return render(request, 'inventory/purchase_recost.html', {
        'inv': inv, 'rows': rows, 'method': method, 'error': error,
        'blockers': recost_blockers(inv), 'extra_total': inv.landed_costs_total,
        'lines_json': lines,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def vendor_payment_void(request, pk, ft_id):
    """↩️ إلغاء دفعة غلط لمورد — الفلوس ترجع الخزنة والمبلغ يرجع على المورد."""
    from urllib.parse import urlencode
    from django.core.exceptions import ValidationError
    from inventory.services.vendor_payments import void_payment
    back = reverse('inventory:vendor_detail', args=[pk])
    ft = (FinancialTransaction.objects.select_related('treasury__branch')
          .filter(pk=ft_id, vendor_id=pk).first())
    if ft is None:
        return redirect(f"{back}?err=notfound")
    if not _user_can_edit_branch(request.user, ft.treasury.branch):
        return redirect(f"{back}?err=perm")
    try:
        amount = void_payment(ft)
    except ValidationError as exc:
        return redirect(f"{back}?{urlencode({'err': 'msg', 'msg': exc.messages[0]})}")
    _audit_payment_void(request, 'vendor_payment', ft_id, amount, ft.description)
    return redirect(f"{back}?{urlencode({'ok': 'voided', 'amt': amount})}")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
@require_POST
def vendor_fix_balance(request, pk):
    """🧮 تصحيح رصيد مورد اتلخبط: يخلّيه = المتبقّي على فواتيره − دفعاته على الحساب.

    (الحفظ بـ save() عشان سجل التدقيق يسجّل القيمة القديمة والجديدة.)"""
    from inventory.services.vendor_payments import expected_balances
    vendor = Vendor.objects.filter(pk=pk).first()
    if vendor is None:
        return redirect(f"{reverse('inventory:vendors_payables')}?err=notfound")
    vendor.balance = expected_balances([vendor.pk]).get(vendor.pk, Decimal('0.00'))
    vendor.save(update_fields=['balance'])
    return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?ok=fixed")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def vendor_apply_credit(request, pk):
    """🔁 خصم رصيد المورد الدائن (دفعة مقدّمة / زيادة بعد مرتجع) من فواتيره المفتوحة —
    من غير ما فلوس تتحرك. (بيحصل لوحده مع كل فاتورة/مرتجع جديد؛ ده للرصيد القديم.)"""
    from urllib.parse import urlencode
    from inventory.services.vendor_payments import apply_vendor_credit
    vendor = Vendor.objects.filter(pk=pk).first()
    if vendor is None:
        return redirect(f"{reverse('inventory:vendors_payables')}?err=notfound")
    if not _can_edit_invoices(request.user):
        return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?err=perm")
    applied = apply_vendor_credit(vendor)
    total = sum((amt for _inv, amt in applied), Decimal('0.00'))
    msg = "، ".join(f"#{inv.pk}: {amt:,.2f}" for inv, amt in applied)
    return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?"
                    + urlencode({'ok': 'credit', 'amt': f"{total:,.2f}", 'msg': msg}))
