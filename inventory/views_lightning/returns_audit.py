"""🔎 مراجعة المرتجعات القديمة (سطور اترجعت زيادة بالغلط) وتصحيحها.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import SaleInvoice, SaleInvoiceItem, Treasury
from inventory.views import _get_branch_for_user, _user_can_edit_branch, tenant_required
from inventory.views.utils import module_required, role_required
from ._common import _is_walk_in, _report_branch
from .sales import _flash_int


# =====================================================================
# 🔎 مراجعة المرتجعات القديمة — مرتجعات اترجع فيها سطور زيادة بالغلط
# =====================================================================
def _audit_tag(ret_id, line_id):
    return f"تصحيح مرتجع #{ret_id} — سطر {line_id}"


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def returns_audit(request):
    """🔎 مرتجعات على فواتير فيها أكتر من صنف ورجّعت أكتر من سطر.

    قبل التصليح، المرتجع الجزئي كان بيرجّع كل سطور الفاتورة التانية كمان
    (مخزون + رد فلوس عن قطع مارجعتش). الصفحة دي بتعرضها عشان تتراجع، ولكل
    سطر زرار «تصحيح» بيعمل فاتورة بيع تصحيحية بنفس السعر.
    """
    branch, branch_options, can_pick_branch = _report_branch(request)
    rets = (SaleInvoice.objects
            .filter(is_return=True, status='posted', original_invoice__isnull=False)
            .select_related('customer', 'branch', 'original_invoice')
            .prefetch_related('items__product', 'original_invoice__items')
            .order_by('-date_created'))
    if branch is not None:
        rets = rets.filter(branch=branch)
    done = set(SaleInvoice.objects.filter(notes__startswith='تصحيح مرتجع #')
               .values_list('notes', flat=True))
    rows = []
    for r in rets[:1000]:
        lines = list(r.items.all())
        if len(lines) < 2 or len(r.original_invoice.items.all()) < 2:
            continue
        rows.append({
            'ret': r,
            'lines': [{'line': ln, 'value': Decimal(ln.quantity) * ln.unit_price - (ln.discount or 0),
                       'corrected': _audit_tag(r.pk, ln.pk) in done} for ln in lines],
        })
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    return render(request, 'inventory/returns_audit.html', {
        'rows': rows, 'branch': branch, 'branch_options': branch_options,
        'can_pick_branch': can_pick_branch, 'treasuries': treasury_qs.order_by('name'),
        'flash_ok': _flash_int(request.GET.get('corrected')),
        'flash_err': request.GET.get('err'), 'flash_msg': (request.GET.get('msg') or '')[:200],
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
@require_POST
def returns_audit_correct(request, ret_id, line_id):
    """↩️↩️ سطر اترجع بالغلط: فاتورة بيع تصحيحية بنفس سعره — المخزون بيطلع تاني،
    والمبلغ يرجع على حساب العميل (آجل) أو للخزنة لو الفلوس ماخرجتش فعلاً."""
    from django.core.exceptions import ValidationError
    from urllib.parse import quote
    back = reverse('inventory:returns_audit')
    ret = (SaleInvoice.objects.select_related('customer', 'branch')
           .filter(pk=ret_id, is_return=True, status='posted').first())
    if ret is None or not _user_can_edit_branch(request.user, ret.branch):
        return redirect(f"{back}?err=notfound")
    branch = _get_branch_for_user(request.user)
    if branch is not None and ret.branch_id != branch.id:
        return redirect(f"{back}?err=notfound")
    line = ret.items.select_related('product').filter(pk=line_id).first()
    if line is None:
        return redirect(f"{back}?err=notfound")
    tag = _audit_tag(ret.pk, line.pk)
    treasury = None
    if request.POST.get('treasury_id'):
        treasury = Treasury.objects.filter(pk=request.POST.get('treasury_id'), is_active=True,
                                           branch=ret.branch).first()
    if treasury is None and _is_walk_in(ret.customer):
        return redirect(f"{back}?err=walkin")
    try:
        with transaction.atomic():
            # 🔒 مرة واحدة بس لكل سطر
            SaleInvoice.objects.select_for_update().get(pk=ret.pk)
            if SaleInvoice.objects.filter(notes=tag).exists():
                return redirect(f"{back}?err=done")
            inv = SaleInvoice.objects.create(
                invoice_type='sale', status='quotation', customer=ret.customer,
                branch=ret.branch, tax_percentage=ret.tax_percentage, notes=tag,
            )
            SaleInvoiceItem.objects.create(
                invoice=inv, product=line.product, quantity=line.quantity,
                unit_price=line.unit_price, discount=line.discount or 0,
                cost_at_sale=line.cost_at_sale, is_billable=line.is_billable,
            )
            # نصيب السطر من خصم المرتجع الإجمالي (لو فيه)
            ret_disc = Decimal(str(ret.discount or 0))
            if ret_disc > 0:
                items_net, _c, services, core = ret.billable_breakdown()
                base = items_net + services + core
                if base > 0:
                    line_net = Decimal(line.quantity) * line.unit_price - (line.discount or 0)
                    inv.discount = (ret_disc * line_net / base).quantize(Decimal('0.01'))
                    inv.save(update_fields=['discount'])
            inv.update_total()
            inv.refresh_from_db()
            if treasury is not None:
                inv.treasury = treasury
                inv.paid_amount = inv.total_amount
                inv.save(update_fields=['treasury', 'paid_amount'])
            inv.status = 'posted'
            inv.save()   # execute_sale: يطلّع المخزون + يقيّد الآجل أو يدخّل الفلوس الخزنة
    except ValidationError as exc:
        return redirect(f"{back}?err=fail&msg={quote('؛ '.join(exc.messages)[:200])}")
    return redirect(f"{back}?corrected={inv.pk}")
