"""💰 الخزائن والحركات المالية: كشف الخزنة، إيداع/سحب، تحويل، حذف حركة.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
import re as _re
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _gt
from django.views.decorators.http import require_POST
from inventory.models import Branch, FinancialTransaction, Treasury
from inventory.views import _get_branch_for_user, _user_can_edit_branch, tenant_required
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from inventory.services.accounting_service import (
    TRANSFER_TAG as _TRANSFER_TAG,
    is_advance_disbursement,
)
from .expenses import _delete_expense_ft


# =====================================================================
# 💰 الخزائن — عرض وإضافة خزنة للفرع النشط
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('treasury')
def treasury_list(request):
    """قائمة خزائن الفرع النشط + إضافة خزنة جديدة.

    - الأدمن في وضع «كل الفروع» بيشوف كل الخزائن ويختار الفرع وقت الإضافة.
    - لو مركّز على فرع (أو موظف فرع)، الخزائن والإضافة بتبقى للفرع ده تلقائياً.
    """
    branch = _get_branch_for_user(request.user)
    qs = Treasury.objects.select_related('branch').order_by('branch__name', 'name')
    if branch is not None:
        qs = qs.filter(branch=branch)
    rows = list(qs)
    total = sum((t.balance or Decimal('0')) for t in rows)
    return render(request, "inventory/treasury_list.html", {
        "rows": rows,
        "branch": branch,
        "total": total,
        "type_choices": Treasury.TYPE_CHOICES,
        # لما الأدمن في وضع «كل الفروع» نعرض قائمة الفروع لاختيار مكان الخزنة
        "branches": Branch.objects.all().order_by('name') if branch is None else None,
        "can_edit": _user_can_edit_branch(request.user, branch) if branch is not None else True,
        "flash": request.GET.get("ok"),
        "flash_err": request.GET.get("err"),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_create(request):
    """إنشاء خزنة جديدة للفرع النشط (أو فرع مختار في وضع كل الفروع)."""
    name = (request.POST.get("name") or "").strip()
    ttype = (request.POST.get("type") or "cash").strip()
    branch = _get_branch_for_user(request.user)
    if branch is None:
        bid = request.POST.get("branch")
        branch = Branch.objects.filter(id=bid).first() if bid else None

    if not name:
        return redirect(f"{reverse('inventory:treasury_list')}?err=name")
    if branch is None:
        return redirect(f"{reverse('inventory:treasury_list')}?err=branch")
    if not _user_can_edit_branch(request.user, branch):
        return redirect(f"{reverse('inventory:treasury_list')}?err=perm")
    valid_types = {t[0] for t in Treasury.TYPE_CHOICES}
    if ttype not in valid_types:
        ttype = "cash"
    if Treasury.objects.filter(name__iexact=name, branch=branch).exists():
        return redirect(f"{reverse('inventory:treasury_list')}?err=dup")

    # Plan-quota guard (tenancy.signals.quota) raises ValidationError when the
    # tenant hits their plan's treasury limit — catch it and show a friendly
    # message instead of a 500.
    from django.core.exceptions import ValidationError
    try:
        Treasury.objects.create(name=name, type=ttype, branch=branch, balance=Decimal('0'))
    except ValidationError:
        return redirect(f"{reverse('inventory:treasury_list')}?err=quota")
    return redirect(f"{reverse('inventory:treasury_list')}?ok=1")


# =====================================================================
# 💳 الحركات المالية — سجل موحّد + كشف حساب الخزنة + إيداع/سحب/تحويل
# =====================================================================
def _txn_meta(ft):
    """يصنّف الحركة المالية ويحدد مصدرها ورابط تعديلها (من الفاتورة/المصروف).

    - مرتبطة بفاتورة بيع → رابط تعديل دفع الفاتورة.
    - مرتبطة بفاتورة شراء → مصدر شراء (تعديل من الأدمن).
    - سحب تشغيلي (out) غير مرتبط بفاتورة → تعديل كمصروف.
    - إيداع/سحب يدوي أو تحويل → قابل للحذف من مكانه.
    """
    if ft.sale_invoice_id:
        inv = ft.sale_invoice
        if inv is not None and not inv.is_return:
            return {"label": _gt("فاتورة بيع #%(n)s") % {"n": inv.id}, "kind": "sale",
                    "url": reverse('inventory:sale_invoice_edit', args=[inv.id]), "deletable": False}
        if inv is not None:
            return {"label": _gt("مرتجع #%(n)s") % {"n": inv.id}, "kind": "return", "url": None, "deletable": False}
    if ft.purchase_invoice_id or ft.vendor_id:
        label = (_gt("فاتورة شراء #%(n)s") % {"n": ft.purchase_invoice_id} if ft.purchase_invoice_id
                 else _gt("سداد مورد"))
        url = reverse('inventory:vendor_detail', args=[ft.vendor_id]) if ft.vendor_id else None
        return {"label": label, "kind": "purchase", "url": url, "deletable": False}
    if is_advance_disbursement(ft):
        return {"label": _gt("صرف سلفة موظف"), "kind": "advance", "url": None, "deletable": False}
    # تحصيل من عميل — يُدار من كشف حساب العميل (حذفه هنا مش هيرجّع رصيد العميل)
    if ft.customer_id:
        return {"label": _gt("تحصيل عميل"), "kind": "customer",
                "url": reverse('inventory:customer_detail', args=[ft.customer_id]), "deletable": False}
    # 🐛 [FIX]: مسحوبات المالك (وسداد الضريبة) كانت بتظهر «مصروف / سحب» برابط
    #    تعديل المصروف — والرابط بيرجّع «مش موجود» لأنها مش مصروف تشغيلي.
    if ft.equity_kind:
        return {"label": ft.get_equity_kind_display(), "kind": "equity", "url": None, "deletable": True}
    is_transfer = (ft.description or "").startswith(_TRANSFER_TAG)
    if ft.transaction_type == 'out' and not is_transfer:
        return {"label": _gt("مصروف / سحب"), "kind": "expense",
                "url": reverse('inventory:expense_edit', args=[ft.id]), "deletable": True}
    return {"label": (_gt("تحويل") if is_transfer else _gt("إيداع/حركة يدوية")),
            "kind": "transfer" if is_transfer else "manual", "url": None, "deletable": True}


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('transactions')
def transactions_list(request):
    """💳 سجل كل الحركات المالية (إيداع/سحب) على مستوى الفرع النشط، بفلاتر
    وإجماليات، وكل حركة برابط لمصدرها للتعديل."""
    branch = _get_branch_for_user(request.user)
    qs = (FinancialTransaction.objects
          .select_related('treasury', 'treasury__branch', 'sale_invoice', 'category', 'customer')
          .order_by('-date', '-id'))
    if branch is not None:
        qs = qs.filter(treasury__branch=branch)

    ttype = (request.GET.get('type') or '').strip()
    if ttype in ('in', 'out'):
        qs = qs.filter(transaction_type=ttype)
    tre_id = (request.GET.get('treasury') or '').strip()
    if tre_id.isdigit():
        qs = qs.filter(treasury_id=int(tre_id))
    q = (request.GET.get('q') or '').strip()
    if q:
        cond = Q(description__icontains=q) | Q(customer__name__icontains=q)
        if q.isdigit():
            cond |= Q(sale_invoice_id=int(q))
        qs = qs.filter(cond)

    agg = qs.aggregate(
        tin=Sum('amount', filter=Q(transaction_type='in')),
        tout=Sum('amount', filter=Q(transaction_type='out')),
    )
    total_in = agg['tin'] or Decimal('0')
    total_out = agg['tout'] or Decimal('0')

    _exp = export_report(
        request, "transactions", "الحركات المالية",
        (branch.name if branch else "كل الفروع"),
        [{
            "columns": ["التاريخ", "الخزنة", "البيان", "وارد", "صادر"],
            "rows": [[ft.date.strftime("%Y-%m-%d %H:%M"), ft.treasury.name, ft.description,
                      (ft.amount if ft.transaction_type == 'in' else Decimal('0')),
                      (ft.amount if ft.transaction_type == 'out' else Decimal('0'))]
                     for ft in qs[:5000]],
            "total": ["", "", "الإجمالي", total_in, total_out],
        }])
    if _exp:
        return _exp

    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    rows = [{"ft": ft, "meta": _txn_meta(ft)} for ft in page.object_list]

    treasuries = Treasury.objects.select_related('branch')
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)

    return render(request, 'inventory/transactions_list.html', {
        'page': page, 'rows': rows, 'branch': branch,
        'total_in': total_in, 'total_out': total_out, 'net': total_in - total_out,
        'ttype': ttype, 'q': q, 'tre_id': tre_id,
        'treasuries': treasuries.order_by('name'),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def treasury_detail(request, pk):
    """🧾 كشف حساب خزنة واحدة — كل حركاتها + رصيد جاري + روابط تعديل المصدر."""
    branch = _get_branch_for_user(request.user)
    t = Treasury.objects.select_related('branch').filter(pk=pk).first()
    if not t or (branch is not None and t.branch_id != branch.id):
        return redirect(f"{reverse('inventory:treasury_list')}?err=notfound")

    base = (FinancialTransaction.objects
            .select_related('sale_invoice', 'category', 'customer')
            .filter(treasury=t))

    # رصيد جاري لكل حركة (نحسبه من كل السجل تصاعدياً مرة واحدة)
    running = {}
    ledger = list(base.order_by('date', 'id').values('id', 'transaction_type', 'amount'))
    if len(ledger) <= 8000:
        bal = Decimal('0')
        for r in ledger:
            bal += r['amount'] if r['transaction_type'] == 'in' else -r['amount']
            running[r['id']] = bal

    qs = base.order_by('-date', '-id')
    ttype = (request.GET.get('type') or '').strip()
    if ttype in ('in', 'out'):
        qs = qs.filter(transaction_type=ttype)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(customer__name__icontains=q))

    agg = qs.aggregate(
        tin=Sum('amount', filter=Q(transaction_type='in')),
        tout=Sum('amount', filter=Q(transaction_type='out')),
    )
    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    rows = [{"ft": ft, "meta": _txn_meta(ft), "running": running.get(ft.id)}
            for ft in page.object_list]

    can_edit = _user_can_edit_branch(request.user, t.branch)
    other_treasuries = (Treasury.objects.filter(is_active=True, branch=t.branch)
                        .exclude(pk=t.pk).order_by('name'))
    return render(request, 'inventory/treasury_statement.html', {
        'treasury': t, 'rows': rows, 'page': page, 'branch': branch,
        'total_in': agg['tin'] or Decimal('0'), 'total_out': agg['tout'] or Decimal('0'),
        'ttype': ttype, 'q': q, 'can_edit': can_edit,
        'other_treasuries': other_treasuries,
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_movement(request, pk):
    """➕ إيداع أو سحب يدوي على خزنة (رأس مال، تسوية، سحب شخصي… إلخ)."""
    t = Treasury.objects.filter(pk=pk, is_active=True).first()
    if not t:
        return redirect(f"{reverse('inventory:treasury_list')}?err=notfound")
    if not _user_can_edit_branch(request.user, t.branch):
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=perm")
    # 💼 التصنيف: عادي / رأس مال (إيداع مالك) / مسحوبات المالك. رأس المال
    #    والمسحوبات بيتقيّدوا على حساب رأس المال (٣٠٠١) بدل الإيراد/المصروف.
    kind = (request.POST.get('kind') or 'normal').strip()
    equity_kind = ''
    if kind == 'capital':
        direction, equity_kind = 'in', 'capital'
    elif kind == 'drawings':
        direction, equity_kind = 'out', 'drawings'
    else:
        direction = (request.POST.get('direction') or '').strip()
    if direction not in ('in', 'out'):
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=dir")
    try:
        amount = Decimal(str(request.POST.get('amount') or '0'))
    except InvalidOperation:
        amount = Decimal('0')
    if not amount.is_finite() or amount <= 0:   # NaN كان بيوقّع الصفحة بـ 500
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=amount")
    _default_desc = {
        'capital': 'إيداع رأس مال (مالك)',
        'drawings': 'مسحوبات المالك',
    }.get(kind, 'إيداع يدوي' if direction == 'in' else 'سحب يدوي')
    desc = (request.POST.get('description') or '').strip() or _default_desc
    with transaction.atomic():
        locked = Treasury.objects.select_for_update().get(pk=t.pk)
        if direction == 'out' and (locked.balance or Decimal('0')) < amount:
            return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=balance")
        FinancialTransaction.objects.create(
            treasury=locked, transaction_type=direction, amount=amount,
            description=desc, equity_kind=equity_kind)
    return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?ok=moved")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_transfer(request, pk):
    """🔁 تحويل مبلغ من خزنة لخزنة تانية (نفس الفرع) — حركتين مربوطتين."""
    src = Treasury.objects.filter(pk=pk, is_active=True).first()
    if not src:
        return redirect(f"{reverse('inventory:treasury_list')}?err=notfound")
    if not _user_can_edit_branch(request.user, src.branch):
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=perm")
    dst = Treasury.objects.filter(id=request.POST.get('to_id'), is_active=True,
                                  branch=src.branch).first()
    if not dst or dst.pk == src.pk:
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=dest")
    try:
        amount = Decimal(str(request.POST.get('amount') or '0'))
    except InvalidOperation:
        amount = Decimal('0')
    if not amount.is_finite() or amount <= 0:   # NaN كان بيوقّع الصفحة بـ 500
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=amount")
    import uuid as _uuid
    ref = f"{_TRANSFER_TAG}{_uuid.uuid4().hex[:8]}]"
    with transaction.atomic():
        locked = Treasury.objects.select_for_update().get(pk=src.pk)
        if (locked.balance or Decimal('0')) < amount:
            return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=balance")
        FinancialTransaction.objects.create(
            treasury=locked, transaction_type='out', amount=amount,
            description=f"{ref} تحويل إلى {dst.name}")
        FinancialTransaction.objects.create(
            treasury=dst, transaction_type='in', amount=amount,
            description=f"{ref} تحويل من {src.name}")
    return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?ok=transferred")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_txn_delete(request, pk):
    """🗑️ حذف حركة يدوية/تحويل (مش مرتبطة بفاتورة) مع إرجاع الرصيد.

    التحويل بيتحذف بطرفيه معاً. الحركات المرتبطة بفاتورة بتتعدّل من الفاتورة.
    """
    from django.utils.http import url_has_allowed_host_and_scheme
    # 🐛 [FIX]: «next» كان بيتاخد زي ما هو → أي حد يقدر يحط رابط موقع برّه
    #    (open redirect). دلوقتي مسار داخلي بس، ومن غير ok/err قديمة متراكمة.
    back = request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        back = reverse('inventory:transactions_list')
    back = _re.sub(r'([?&])(ok|err)=[^&]*&?', r'\1', back).rstrip('?&')

    def _go(param):
        sep = '&' if '?' in back else '?'
        return redirect(f"{back}{sep}{param}")

    ft = FinancialTransaction.objects.select_related('treasury__branch').filter(pk=pk).first()
    if not ft:
        return _go("err=notfound")
    # الحركات المرتبطة بفاتورة/عميل/مورد/سلفة موظف بتتدار من مصدرها (عشان الرصيد يتظبط صح)
    if (ft.sale_invoice_id or ft.purchase_invoice_id or ft.customer_id or ft.vendor_id
            or is_advance_disbursement(ft)):
        return _go("err=linked")
    if not _user_can_edit_branch(request.user, ft.treasury.branch):
        return _go("err=perm")
    desc = ft.description or ""
    with transaction.atomic():
        # لو تحويل: احذف الطرف التاني كمان (نفس مرجع التحويل)
        if desc.startswith(_TRANSFER_TAG):
            ref = desc[:desc.find(']') + 1]
            legs = list(FinancialTransaction.objects.filter(description__startswith=ref))
        else:
            legs = [ft]
        # 🐛 [FIX]: حذف إيداع (أو طرف الوارد في تحويل) اتصرف منه بعد كده كان
        #    بيوقّع الخزنة تحت الصفر من غير أي تنبيه.
        for leg in legs:
            if leg.transaction_type != 'in':
                continue
            locked = Treasury.objects.select_for_update().get(pk=leg.treasury_id)
            if (locked.balance or Decimal('0')) < leg.amount:
                return _go("err=balance")
        for leg in legs:
            _delete_expense_ft(leg)
    return _go("ok=deleted")
