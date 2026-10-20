"""🛒 فواتير الشراء: إنشاء/تعديل/حذف/مرتجع، والقائمة والتصدير.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import (
    Branch,
    Inventory,
    InventoryMovement,
    Product,
    PurchaseInvoice,
    Treasury,
    Vendor,
)
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from ._common import _RollbackWith, _can_bulk_delete_products, _finite_dec
from .invoice_edits import _purge_invoice_payments


# =====================================================================
# 🛒 فواتير الشراء — استلام بضاعة من مورد (يزوّد المخزون + مستحقات المورد)
# =====================================================================
def _purchase_list_qs(request, branch):
    """فواتير الشراء بفلاتر الصفحة (بحث/من/إلى/حالة الدفع) — نفسها للعرض والتصدير."""
    from django.utils.dateparse import parse_date
    data = request.GET
    qs = (PurchaseInvoice.objects.select_related('vendor', 'branch', 'treasury')
          .order_by('-date_created'))
    if branch is not None:
        qs = qs.filter(branch=branch)
    f = {'q': (data.get('q') or '').strip(), 'from': (data.get('from') or '').strip(),
         'to': (data.get('to') or '').strip(), 'pay': (data.get('pay') or '').strip()}
    if f['q']:
        cond = Q(vendor__name__icontains=f['q'])
        if f['q'].lstrip('#').isdecimal():
            cond |= Q(id=int(f['q'].lstrip('#')))
        qs = qs.filter(cond)
    def _day(v):
        try:
            return parse_date(v) if v else None
        except ValueError:   # 2026-13-45
            return None
    d_from, d_to = _day(f['from']), _day(f['to'])
    if d_from:
        qs = qs.filter(date_created__date__gte=d_from)
    if d_to:
        qs = qs.filter(date_created__date__lte=d_to)
    if f['pay'] in ('due', 'paid'):
        qs = qs.annotate(_due=F('total_amount') - F('returned_amount') - F('paid_amount'))
        qs = qs.filter(_due__gt=Decimal('0.005')) if f['pay'] == 'due' else qs.filter(_due__lte=Decimal('0.005'))
    elif f['pay'] == 'draft':
        qs = qs.filter(is_applied=False)
    else:
        f['pay'] = ''
    return qs, f


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
def purchase_list(request):
    """قائمة فواتير الشراء + فلاتر + رفع فاتورة (Excel/PDF) + تصدير."""
    from urllib.parse import urlencode
    branch = _get_branch_for_user(request.user)
    qs, f = _purchase_list_qs(request, branch)
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    rows = [{"inv": inv, "due": inv.net_due} for inv in page.object_list]
    treasuries = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)
    from inventory.services.purchase_repair import broken_purchase_edits
    return render(request, 'inventory/purchase_list.html', {
        'broken_edits': broken_purchase_edits(branch),
        'can_repair': _can_bulk_delete_products(request.user),
        'page': page, 'rows': rows, 'q': f['q'], 'f': f, 'branch': branch,
        'keep': urlencode({k: v for k, v in f.items() if v}),
        'filtered': any(f.values()), 'total_count': page.paginator.count,
        'treasuries': treasuries.select_related('branch').order_by('branch__name', 'name'),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
        'returned_amt': request.GET.get('amt'),
        'paid_amt': request.GET.get('amt'), 'paid_inv': request.GET.get('inv'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
def purchase_export(request):
    """📤 تصدير فواتير الشراء Excel/PDF — المحدد أو كل نتائج الفلتر.

    mode=summary: فاتورة في كل سطر (إجمالي/مدفوع/مرتجع/متبقّي).
    mode=lines:   كل صنف في سطر (الكود/الكمية/اللي اترجع/سعر الشراء).
    """
    from django.utils import timezone as _tz
    from inventory.models import PurchaseInvoiceItem
    branch = _get_branch_for_user(request.user)
    qs, f = _purchase_list_qs(request, branch)
    if (request.GET.get('all') or '') != '1':
        ids = {int(x) for x in str(request.GET.get('ids') or '').split(',') if x.strip().isdigit()}
        qs = qs.filter(pk__in=ids)
    mode = 'lines' if request.GET.get('mode') == 'lines' else 'summary'
    fmt = (request.GET.get('fmt') or 'xlsx').strip().lower()
    request.GET = request.GET.copy()
    request.GET['export'] = fmt if fmt in ('xlsx', 'pdf') else 'xlsx'
    Z = Decimal('0')

    if mode == 'summary':
        cols = ['#', 'التاريخ', 'المورد', 'الفرع', 'عدد الأصناف', 'الإجمالي', 'المدفوع',
                'المرتجع', 'المتبقّي', 'الحالة', 'الخزنة']
        rows, sums = [], [Z, Z, Z, Z]
        for inv in qs.annotate(_n=Count('items'))[:20000]:
            vals = [Decimal(str(inv.total_amount or 0)), Decimal(str(inv.paid_amount or 0)),
                    Decimal(str(inv.returned_amount or 0)), inv.net_due]
            sums = [a + b for a, b in zip(sums, vals)]
            rows.append([inv.id, inv.date_created, inv.vendor.name, inv.branch.name, inv._n, *vals,
                         'معتمدة' if inv.is_applied else 'مسودة',
                         inv.treasury.name if inv.treasury_id else 'آجل'])
        total = ['', '', 'الإجمالي', '', '', *sums, '', '']
        title = 'فواتير الشراء'
    else:
        cols = ['#', 'التاريخ', 'المورد', 'رقم القطعة', 'الصنف', 'الكمية', 'اترجع للمورد',
                'سعر الشراء', 'الإجمالي']
        items = (PurchaseInvoiceItem.objects.filter(invoice__in=qs.values('pk'))
                 .select_related('invoice__vendor', 'product')
                 .order_by('-invoice__date_created', 'invoice_id', 'id'))[:100000]
        rows, q_sum, r_sum, v_sum = [], 0, 0, Z
        for it in items:
            line = Decimal(str(it.quantity)) * Decimal(str(it.cost_price or 0))
            q_sum += it.quantity
            r_sum += it.returned_quantity
            v_sum += line
            rows.append([it.invoice_id, it.invoice.date_created, it.invoice.vendor.name,
                         it.product.part_number, it.product.name, it.quantity,
                         it.returned_quantity, Decimal(str(it.cost_price or 0)), line])
        total = ['', '', 'الإجمالي', '', '', q_sum, r_sum, '', v_sum]
        title = 'أصناف فواتير الشراء'

    period = ' → '.join(x for x in (f['from'], f['to']) if x)
    subtitle = ' · '.join(x for x in (branch.name if branch else 'كل الفروع', period,
                                      f"{len(rows)} {'فاتورة' if mode == 'summary' else 'سطر'}") if x)
    return export_report(request, f"purchases_{_tz.localdate():%Y%m%d}", title, subtitle,
                         [{"columns": cols, "rows": rows, "total": total}])


@login_required(login_url='/login/')
@tenant_required
@module_required('purchases')
def purchase_import_template(request):
    """📄 نموذج Excel لفاتورة شراء (العناوين اللي صفحة الرفع بتفهمها)."""
    from django.http import HttpResponse
    from inventory.services.purchase_import import template_xlsx
    resp = HttpResponse(template_xlsx(),
                        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="purchase_invoice_template.xlsx"'
    return resp


def _reverse_purchase_posting(inv, keep_payments=False):
    """↩️ يعكس أثر فاتورة شراء معتمدة بالكامل عشان نقدر نعدّلها أو نحذفها:
      - يرجّع الكميات من المخزون (مع إعادة حساب متوسط التكلفة) ويسجّل حركة عكسية
      - يقلّل مستحقات المورد بمقدار الآجل
      - يمسح دفعات الخزنة (الـ signal بيرجّع الرصيد) + قيودها. مع
        ``keep_payments`` (التعديل/الشحنة): دفعات الفاتورة نفسها اللي اتدفعت فعلاً
        (عند الإنشاء أو من «سداد») بتفضل زي ما هي — بس دفعات بنود المصاريف
        بتتشال لإن البنود بتتبني من جديد.
      - يمسح قيد الاستلام المحاسبي عشان الاعتماد التاني يعيد التقييد نظيف
      - يرجّع الفاتورة لحالة draft (is_applied=False)

    بيرفض لو أي صنف اتباع (الكمية المتاحة أقل من المستلَم) حمايةً للمخزون.
    لازم يتنادى جوه transaction.atomic.
    """
    from inventory.models import AccountingEntry, JournalEntry
    if not inv.is_applied:
        return
    if inv.returned_amount or inv.items.filter(returned_quantity__gt=0).exists():
        raise ValueError(
            "الفاتورة دي عليها مرتجع للمورد — مينفعش تتعدّل أو تتحذف. "
            "اعمل مرتجع للأصناف الباقية أو فاتورة جديدة بدل التعديل.")
    from inventory.services.purchase_repair import needs_repair, repair_purchase
    if needs_repair(inv):
        # 🩹 فاتورة كسرها التعديل القديم (بضاعتها مارجعتش للمخزن) — نصلّحها الأول
        #    وبعدين نعكسها، بدل ما العكس يرفض لإن الكميات «مش موجودة».
        repair_purchase(inv)
        inv.refresh_from_db()
    items = list(inv.items.select_related('product').all())
    # 1) تحقّق إن الكميات لسه موجودة (ما اتباعتش) قبل ما نرجّعها — بإجمالي
    #    الصنف في الفاتورة (نفس الصنف ممكن يبقى على أكتر من سطر).
    received = {}
    for item in items:
        received[item.product_id] = received.get(item.product_id, 0) + item.quantity
    for item in items:
        if item.product_id not in received:
            continue
        need = received.pop(item.product_id)
        row = (Inventory.objects.select_for_update()
               .filter(product=item.product, branch=inv.branch).first())
        current = row.quantity if row else 0
        if current < need:
            raise ValueError(
                f"لا يمكن التعديل/الحذف: المتاح من «{item.product.name}» ({current}) "
                f"أقل من المستلَم في الفاتورة ({need}) — على الأرجح اتباع. "
                f"اعمل فاتورة تسوية بدل التعديل."
            )
    # 2) رجّع الكميات + أعِد حساب متوسط التكلفة (عكس المعادلة المرجّحة)
    for item in items:
        row = (Inventory.objects.select_for_update()
               .get(product=item.product, branch=inv.branch))
        before = row.quantity
        row.quantity = before - item.quantity
        row.save(update_fields=['quantity'])
        prod = Product.objects.select_for_update().get(pk=item.product_id)
        remaining = prod.valued_qty  # بعد الخصم (+ اللي في الطريق بين الفروع)
        total_before = remaining + item.quantity
        if remaining > 0:
            # نعكس بنفس تكلفة الوصول اللي اتحسبت وقت الاعتماد (لو موجودة)،
            # وإلا سعر المورد — عشان العكس يطابق الاعتماد بالظبط.
            unit_cost = item.effective_unit_cost
            new_avg = (
                (Decimal(str(total_before)) * Decimal(str(prod.average_cost)))
                - (Decimal(str(item.quantity)) * Decimal(str(unit_cost)))
            ) / Decimal(str(remaining))
            prod.average_cost = max(new_avg, Decimal('0'))
            prod.save(update_fields=['average_cost'])
        InventoryMovement.objects.create(
            product=item.product, branch=inv.branch, reason='adjustment',
            quantity_change=-item.quantity, quantity_before=before,
            quantity_after=row.quantity, reference_type='PurchaseReverse',
            reference_id=inv.id, note=f"عكس استلام فاتورة شراء #{inv.id} (تعديل/حذف)",
        )
    # 3) رجّع مستحقات المورد (الجزء الآجل — أو الرصيد اللي لينا لو مدفوع زيادة)
    due = Decimal(str(inv.total_amount)) - Decimal(str(inv.paid_amount))
    if due != Decimal('0.00'):
        inv.vendor.balance = F('balance') - due
        inv.vendor.save(update_fields=['balance'])
    # 4) امسح دفعات الخزنة (الـ signal بيرجّع الرصيد) + قيودها
    if keep_payments:
        # 🐛 [FIX]: التعديل كان بيمسح كل الدفعات (حتى اللي اتدفعت بعدين من «سداد»
        #    ومن خزنة فرع تاني) ويعمل دفعة واحدة من خانة «المدفوع» في صفحة التعديل ⇒
        #    دفعات بتضيع/بتتنقل لخزنة غلط ومستحقات المورد بترجع.
        for ft in list(inv.payments.filter(purchase_extra_cost__isnull=False)):
            ft.delete()
    else:
        # 🔁 اللي اتخصم من رصيد دائن للمورد (دفعة مقدّمة) مش فلوس اتدفعت على الفاتورة
        #    دي — بيرجع رصيد دائن على الحساب بدل ما يتمسح ويرجع للخزنة.
        from inventory.services.vendor_payments import release_credit
        release_credit(inv)
        _purge_invoice_payments(inv)
    # 5) شيل قيد الاستلام المحاسبي عشان إعادة الاعتماد تعيد التقييد
    #    (فترة مقفولة → قيد عكسي بدل مسح أرقام اتقفلت).
    from inventory.services.accounting_service import AccountingService
    AccountingService.unpost(JournalEntry.objects.filter(purchase_invoice=inv),
                             reason=f"عكس اعتماد فاتورة الشراء #{inv.id}")
    AccountingEntry.objects.filter(purchase_invoice=inv, journal_entry__isnull=True).delete()
    # 6) رجّعها draft (في الداتابيز والذاكرة) — 🐛 [FIX]: كانت بتفضل «posted» مع
    #    is_applied=False، فمسح بنودها بعد كده (signal إعادة حساب الإجمالي بيحفظ
    #    الفاتورة) كان بيعتمدها «فاضية» ويعلّمها معتمدة، والاعتماد الحقيقي في
    #    الآخر بيتخطّى ⇒ تعديل الفاتورة كان بيصفّر المخزون ومستحقات المورد.
    reset = {} if keep_payments else {'paid_amount': Decimal('0'), 'treasury': None}
    PurchaseInvoice.objects.filter(pk=inv.pk).update(is_applied=False, status='draft', **reset)
    inv.refresh_from_db()


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def purchase_create(request):
    """صفحة إنشاء فاتورة شراء (اختيار المورد + الأصناف + الدفع)."""
    branch = _get_branch_for_user(request.user)
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    return render(request, 'inventory/purchase_create.html', {
        'branch': branch,
        'branches': Branch.objects.all().order_by('name') if branch is None else None,
        'vendors': Vendor.objects.all().order_by('name'),
        'treasuries': treasury_qs.select_related('branch').order_by('name'),
        'part_categories': Product.PART_CATEGORY_CHOICES,
        'conditions': Product.CONDITION_CHOICES,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def purchase_edit(request, pk):
    """صفحة تعديل فاتورة شراء (تُعبّأ بالبيانات الحالية وتُحفَظ عبر نفس purchase_save)."""
    branch = _get_branch_for_user(request.user)
    inv = PurchaseInvoice.objects.filter(pk=pk).select_related('vendor', 'branch', 'treasury').first()
    if inv is None:
        return redirect(reverse('inventory:purchase_list') + '?err=notfound')
    if branch is not None and inv.branch_id != branch.id:
        return redirect(reverse('inventory:purchase_list') + '?err=branch')
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    import json as _json
    edit_items = [{
        "product_id": it.product_id,
        "name": it.product.name,
        "sku": it.product.part_number,
        "qty": it.quantity,
        "cost": float(it.cost_price),
        "sell": float(it.product.retail_price or 0),
        "extra_unit": (float(it.manual_extra_unit) if it.manual_extra_unit is not None else None),
        "item_id": it.id,
    } for it in inv.items.select_related('product').order_by('id')]
    edit_extra_costs = [{
        "kind": ec.kind,
        "behavior": ec.behavior or ec.default_behavior_for(ec.kind),
        "label": ec.label,
        "amount": float(ec.amount),
        "treasury_id": ec.treasury_id,
        "category_id": ec.expense_category_id,
    } for ec in inv.extra_costs.filter(shipment__isnull=True)]
    # 📦 نصيب الفاتورة من شحنة مجمّعة — عرض بس (بيتعدّل من صفحة الشحنة)
    shipment_extras = [{
        "kind": ec.get_kind_display(),
        "landed": ec.is_landed,
        "amount": float(ec.amount),
    } for ec in inv.extra_costs.filter(shipment__isnull=False)]
    edit_ctx = {
        "id": inv.id,
        "vendor_id": inv.vendor_id,
        "branch_id": inv.branch_id,
        "treasury_id": inv.treasury_id,
        "paid_amount": float(inv.paid_amount or 0),
        "is_applied": inv.is_applied,
        "cost_allocation": inv.cost_allocation,
        "items": edit_items,
        "extra_costs": edit_extra_costs,
        "shipment_extras": shipment_extras,
        "shipment": ({"id": inv.shipment_id, "name": inv.shipment.name} if inv.shipment_id else None),
    }
    return render(request, 'inventory/purchase_create.html', {
        'branch': branch,
        'branches': Branch.objects.all().order_by('name') if branch is None else None,
        'vendors': Vendor.objects.all().order_by('name'),
        'treasuries': treasury_qs.select_related('branch').order_by('name'),
        'part_categories': Product.PART_CATEGORY_CHOICES,
        'conditions': Product.CONDITION_CHOICES,
        'edit_invoice': inv,
        'edit_json': _json.dumps(edit_ctx, ensure_ascii=False),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def purchase_delete(request, pk):
    """🗑️ حذف فاتورة شراء نهائياً: يعكس كل أثرها (مخزون/مورد/خزنة/قيود) ثم يحذفها."""
    branch = _get_branch_for_user(request.user)
    inv = PurchaseInvoice.objects.filter(pk=pk).select_related('vendor', 'branch').first()
    if inv is None:
        return redirect(reverse('inventory:purchase_list') + '?err=notfound')
    if branch is not None and inv.branch_id != branch.id:
        return redirect(reverse('inventory:purchase_list') + '?err=branch')
    if not _user_can_edit_branch(request.user, inv.branch):
        return redirect(reverse('inventory:purchase_list') + '?err=perm')
    if inv.shipment_id:
        # حذفها كان هيضيّع نصيبها من مصاريف الشحنة (الباقي مايتوزّعش تاني)
        return redirect(reverse('inventory:purchase_list') + '?err=shipment')
    try:
        with transaction.atomic():
            _reverse_purchase_posting(inv)
            inv.items.all().delete()
            inv.delete()
            # الرصيد الدائن اللي رجع على الحساب يتخصم من باقي فواتير المورد
            from inventory.services.vendor_payments import apply_vendor_credit
            apply_vendor_credit(inv.vendor)
    except ValueError as ve:
        return redirect(reverse('inventory:purchase_list') + f'?err={ve}')
    except Exception as exc:  # noqa: BLE001
        return redirect(reverse('inventory:purchase_list') + f'?err={exc}')
    return redirect(reverse('inventory:purchase_list') + '?ok=deleted')


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
def purchase_return(request, pk):
    """↩️ مرتجع مشتريات — ترجيع أصناف (كلها أو جزء) من فاتورة شراء للمورد.

    بيطلّع البضاعة من المخزن، يقلّل مستحقات المورد، ويقيّد القيد المحاسبي؛
    ولو المورد رجّع فلوس كاش بتدخل الخزنة المختارة (والباقي رصيد لنا عنده).
    """
    from django.core.exceptions import ValidationError
    from inventory.services.purchase_return_service import return_to_vendor

    branch = _get_branch_for_user(request.user)
    inv = PurchaseInvoice.objects.filter(pk=pk).select_related('vendor', 'branch').first()
    if inv is None:
        return redirect(reverse('inventory:purchase_list') + '?err=notfound')
    if branch is not None and inv.branch_id != branch.id:
        return redirect(reverse('inventory:purchase_list') + '?err=branch')
    if not _user_can_edit_branch(request.user, inv.branch):
        return redirect(reverse('inventory:purchase_list') + '?err=perm')

    treasuries = Treasury.objects.filter(is_active=True, branch=inv.branch).order_by('name')
    error = None
    if request.method == 'POST':
        lines = []
        for key, val in request.POST.items():
            if key.startswith('qty_') and key[4:].isdecimal() and (val or '').strip():
                lines.append((key[4:], val.strip()))
        refund_treasury = treasuries.filter(pk=request.POST.get('treasury_id') or 0).first()
        refund = _finite_dec(request.POST.get('refund_amount'), Decimal('0'))
        if refund is None:
            refund = Decimal('-1')
        try:
            result = return_to_vendor(
                inv, lines, refund_treasury=refund_treasury, refund_amount=refund,
                note=(request.POST.get('note') or '').strip()[:200], user=request.user,
            )
        except ValidationError as exc:
            error = '؛ '.join(exc.messages)
        else:
            return redirect(reverse('inventory:purchase_list')
                            + f"?ok=returned&amt={result['vendor_value']}")

    items = [{'item': it, 'returnable': it.quantity - it.returned_quantity}
             for it in inv.items.select_related('product').order_by('id')]
    stock = {row.product_id: row.quantity for row in
             Inventory.objects.filter(branch=inv.branch, product_id__in=[r['item'].product_id for r in items])}
    posted = request.POST if request.method == 'POST' else {}
    for r in items:
        r['on_hand'] = stock.get(r['item'].product_id, 0)
        r['max'] = max(min(r['returnable'], r['on_hand']), 0)
        # لو حصل خطأ مانمسحش اللي المستخدم كتبه
        r['entered'] = (posted.get(f"qty_{r['item'].id}") or '').strip() if posted else ''
    return render(request, 'inventory/purchase_return.html', {
        'inv': inv, 'items': items, 'treasuries': treasuries, 'error': error,
        'can_return': inv.status == 'posted' and inv.is_applied,
        'form': {k: (posted.get(k) or '') for k in ('refund_amount', 'treasury_id', 'note')} if posted else {},
    })


def _get_or_create_purchase_product(raw, cost):
    """🆕 ينشئ صنفاً جديداً وقت الشراء لأول مرة (أو يعيد الموجود بنفس الكود).

    يرجّع كائن Product، أو نص رسالة خطأ لو الاسم ناقص. الكود (SKU) اختياري —
    لو فاضي بنولّد كود تلقائي فريد. سعر الشراء بيتسجّل كتكلفة، وسعر البيع
    الابتدائي بيتحط = التكلفة (المستخدم يعدّله بعدين من تعديل القطعة).
    """
    name = (raw.get("name") or "").strip()
    if not name:
        return "اكتب اسم الصنف الجديد."
    sku = (raw.get("sku") or "").strip()
    if sku:
        # لو الكود موجود بالفعل، نستخدم نفس الصنف بدل ما نرفض العملية
        existing = Product.objects.filter(part_number=sku).first()
        if existing is not None:
            return existing
    else:
        # 🔗 الصنف ممكن يكون موجود في الكتالوج (متسجّل في فرع تاني) — شاشة الشراء
        #    بتعرض أصناف الفرع بس، فلو اتكتب بنفس الاسم بالظبط نربطه بنفس الصنف
        #    بدل ما نعمل نسخة مكررة في الكتالوج.
        existing = Product.objects.filter(name__iexact=name).order_by('id').first()
        if existing is not None:
            return existing
        # كود تلقائي فريد: NEW- + جزء من الوقت، ونتأكد إنه مش متكرر
        import uuid as _uuid
        sku = f"NEW-{_uuid.uuid4().hex[:8].upper()}"
        while Product.objects.filter(part_number=sku).exists():
            sku = f"NEW-{_uuid.uuid4().hex[:8].upper()}"

    # الباركود فريد — لو متسجّل على صنف تاني نستخدم الصنف ده
    barcode = (raw.get("barcode") or "").strip()[:100] or None
    if barcode:
        existing = Product.objects.filter(barcode=barcode).first()
        if existing is not None:
            return existing

    def _txt(key, limit, default=""):
        return (str(raw.get(key) or "").strip()[:limit]) or default

    def _money(key, default):
        try:
            val = Decimal(str(raw.get(key)))
        except (InvalidOperation, TypeError, ValueError):
            return default
        return val if val >= 0 else default

    def _int(key, default):
        try:
            val = int(raw.get(key))
        except (TypeError, ValueError):
            return default
        return val if val >= 0 else default

    valid_categories = {c[0] for c in Product.PART_CATEGORY_CHOICES}
    part_category = _txt("part_category", 20)
    if part_category not in valid_categories:
        part_category = ""
    valid_conditions = {c[0] for c in Product.CONDITION_CHOICES}
    condition = _txt("condition", 20, "new")
    if condition not in valid_conditions:
        condition = "new"
    return Product.objects.create(
        part_number=sku[:100],
        name=name[:200],
        barcode=barcode,
        brand=_txt("brand", 100, "BMW"),
        condition=condition,
        part_category=part_category,
        car_model=_txt("car_model", 100, "—"),
        car_year=_txt("car_year", 100, "—"),
        engine_code=_txt("engine_code", 100),
        description=str(raw.get("description") or "").strip()[:2000],
        purchase_price=cost,
        average_cost=cost,
        # سعر البيع لو متكتبش = التكلفة (يتعدّل بعدين من تعديل القطعة)
        retail_price=_money("retail_price", cost),
        b2b_wholesale_price=_money("b2b_wholesale_price", Decimal("0")),
        min_stock_level=_int("min_stock_level", 2),
        warranty_months=_int("warranty_months", 0),
    )


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def purchase_save(request):
    """💾 حفظ واعتماد فاتورة شراء: بينشئ الأصناف ثم يعتمد الفاتورة فيشتغل
    execute_purchase (signal) اللي بيزوّد المخزون ومتوسط التكلفة، ويقيّد
    مستحقات المورد، ويسحب المدفوع من الخزنة."""
    import json as _json
    from inventory.models import PurchaseInvoiceItem
    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)

    branch = _get_branch_for_user(request.user)
    if branch is None:
        branch = Branch.objects.filter(id=payload.get("branch_id")).first()
    if branch is None:
        return _json_response_safe({"error": "حدّد الفرع."}, status=400)
    if not _user_can_edit_branch(request.user, branch):
        return _json_response_safe({"error": "👁 صلاحيتك في الفرع ده عرض فقط."}, status=403)

    vendor = Vendor.objects.filter(id=payload.get("vendor_id")).first()
    if vendor is None:
        return _json_response_safe({"error": "اختر المورد."}, status=400)

    items = payload.get("items") or []
    if not items:
        return _json_response_safe({"error": "أضف أصنافاً للفاتورة."}, status=400)

    # ✏️ وضع التعديل: فاتورة موجودة — نعكس أثرها القديم ونعيد بناءها بالكامل
    edit_inv = None
    if payload.get("invoice_id"):
        edit_inv = PurchaseInvoice.objects.filter(id=payload.get("invoice_id")).first()
        if edit_inv is None:
            return _json_response_safe({"error": "فاتورة الشراء غير موجودة."}, status=404)
        if branch is not None and edit_inv.branch_id != branch.id:
            return _json_response_safe({"error": "الفاتورة تخص فرعاً آخر."}, status=403)

    # 🚢 توزيع مصاريف الوصول: بالقيمة أو حسب ربح كل صنف
    allocation = payload.get("cost_allocation")
    if allocation not in ('value', 'margin'):
        allocation = edit_inv.cost_allocation if edit_inv is not None else 'value'

    # الدفع
    treasury = None
    paid = Decimal("0")
    if payload.get("treasury_id") and payload.get("paid_amount") not in (None, ""):
        treasury = Treasury.objects.filter(id=payload.get("treasury_id"),
                                           is_active=True, branch=branch).first()
        paid = _finite_dec(payload.get("paid_amount"), Decimal("0"))

    try:
        with transaction.atomic():
            was_applied = False
            old_vendor = None
            if edit_inv is not None:
                # 🔒 اقفل الفاتورة: ضغطتين «حفظ» ورا بعض كانوا بيعكسوا نفس الأرقام القديمة مرتين
                edit_inv = PurchaseInvoice.objects.select_for_update().select_related('vendor').get(pk=edit_inv.pk)
                was_applied = edit_inv.is_applied
                # اعكس الأثر القديم (بيرفض لو أي صنف اتباع) وامسح البنود القديمة —
                # الدفعات اللي اتدفعت فعلاً بتفضل زي ما هي (بتتغيّر من «سداد» بس)
                _reverse_purchase_posting(edit_inv, keep_payments=True)
                if edit_inv.vendor_id != vendor.pk:
                    # 🔁 اللي اتخصم من رصيد المورد القديم الدائن بيرجع له (مش للمورد الجديد)
                    from inventory.services.vendor_payments import release_credit
                    release_credit(edit_inv)
                    old_vendor = edit_inv.vendor
                    # المورد اتغيّر — دفعات الفاتورة تمشي معاها (كشف حساب المورد الجديد)
                    edit_inv.payments.update(vendor=vendor)
                edit_inv.items.all().delete()
                inv = edit_inv
                inv.vendor = vendor
                inv.status = 'draft'
                inv.cost_allocation = allocation
                inv.save(update_fields=['vendor', 'status', 'cost_allocation'])
            else:
                inv = PurchaseInvoice.objects.create(
                    vendor=vendor, branch=branch, status='draft', cost_allocation=allocation)
            total = Decimal("0")
            for raw in items:
                try:
                    qty = int(raw.get("qty") or 0)
                except (TypeError, ValueError):
                    raise _RollbackWith(_json_response_safe({"error": "الكمية لازم تبقى رقم صحيح."}, status=400))
                cost = _finite_dec(raw.get("cost"))
                if cost is None:
                    raise _RollbackWith(_json_response_safe({"error": "سعر شراء غير صالح."}, status=400))
                if qty <= 0 or cost < 0:
                    raise _RollbackWith(_json_response_safe({"error": "كمية أو سعر غير صالح."}, status=400))

                # 🆕 صنف جديد بيتشترى لأول مرة — ننشئه في الكتالوج فوراً.
                if raw.get("new") and not raw.get("product_id"):
                    product = _get_or_create_purchase_product(raw, cost)
                    if isinstance(product, str):  # رسالة خطأ
                        raise _RollbackWith(_json_response_safe({"error": product}, status=400))
                else:
                    try:
                        pid = int(raw.get("product_id"))
                    except (TypeError, ValueError):
                        raise _RollbackWith(_json_response_safe({"error": "صنف غير صالح في الفاتورة."}, status=400))
                    product = Product.objects.filter(id=pid).first()
                    if product is None:
                        raise _RollbackWith(_json_response_safe({"error": f"صنف #{pid} غير موجود."}, status=404))

                # ✍️ مصاريف القطعة اليدوي (لو اتحدّدت من «إعادة توزيع التكلفة») — بتفضل مع التعديل
                manual_extra = None
                if raw.get("extra_unit") not in (None, ""):
                    try:
                        manual_extra = max(Decimal(str(raw.get("extra_unit"))), Decimal("0"))
                    except (InvalidOperation, TypeError):
                        manual_extra = None
                PurchaseInvoiceItem.objects.create(
                    invoice=inv, product=product, quantity=qty, cost_price=cost,
                    manual_extra_unit=manual_extra)
                total += Decimal(str(qty)) * cost
            inv.update_total()

            # 🚢 مصاريف الوصول (تحميل/جمارك/شحن…) — بنود بتتوزّع على الأصناف
            #    بالقيمة وقت الاعتماد. في وضع التعديل اتمسحت مع reverse فبنعيد بناءها.
            from inventory.models import PurchaseInvoiceExtraCost, ExpenseCategory
            # 📦 أنصبة الشحنة المجمّعة بتتعدّل من صفحة الشحنة — مانمسحهاش هنا
            inv.extra_costs.filter(shipment__isnull=True).delete()
            valid_kinds = {c[0] for c in PurchaseInvoiceExtraCost.KIND_CHOICES}
            valid_behaviors = {c[0] for c in PurchaseInvoiceExtraCost.BEHAVIOR_CHOICES}
            for raw_ec in (payload.get("extra_costs") or []):
                try:
                    amt = Decimal(str(raw_ec.get("amount") or "0"))
                except (InvalidOperation, TypeError):
                    continue
                if not amt.is_finite() or amt <= 0:
                    continue
                kind = (raw_ec.get("kind") or "other").strip()
                if kind not in valid_kinds:
                    kind = "other"
                behavior = (raw_ec.get("behavior") or "").strip()
                if behavior not in valid_behaviors:
                    behavior = PurchaseInvoiceExtraCost.default_behavior_for(kind)
                # خزنة الدفع — لازم تكون في نفس الفرع (وإلا آجل/مستحق)
                ec_treasury = None
                if raw_ec.get("treasury_id"):
                    ec_treasury = Treasury.objects.filter(
                        id=raw_ec.get("treasury_id"), is_active=True, branch=branch).first()
                ec_category = None
                if raw_ec.get("category_id"):
                    ec_category = ExpenseCategory.objects.filter(id=raw_ec.get("category_id")).first()
                PurchaseInvoiceExtraCost.objects.create(
                    invoice=inv, kind=kind, behavior=behavior,
                    label=(raw_ec.get("label") or "").strip()[:120],
                    amount=amt, treasury=ec_treasury, expense_category=ec_category)

            if paid > total:
                paid = total  # المدفوع لا يزيد عن الإجمالي
            # فاتورة معتمدة: دفعاتها بتفضل زي ما هي (خانة الدفع مش ظاهرة في تعديلها)؛
            # مسودة لسه بتتعتمد لأول مرة: الدفع من الصفحة زي الفاتورة الجديدة.
            if not was_applied and treasury is not None and paid > 0:
                locked = Treasury.objects.select_for_update().get(pk=treasury.pk)
                if (locked.balance or Decimal("0")) < paid:
                    raise _RollbackWith(_json_response_safe({
                        "error": f"رصيد الخزنة غير كافٍ للدفع (متاح: {locked.balance})."
                    }, status=409))
                inv.treasury = treasury
                inv.paid_amount = paid
                inv.save(update_fields=["treasury", "paid_amount"])

            # الاعتماد → execute_purchase (signal) بيعمل كل الأثر
            inv.status = 'posted'
            inv.save()
            if old_vendor is not None:
                from inventory.services.vendor_payments import apply_vendor_credit
                apply_vendor_credit(old_vendor)
    except _RollbackWith as rb:
        return rb.response
    except ValueError as exc:
        # رفض مفهوم (صنف اتباع / عليها مرتجع) — نعرض السبب، مش «حدث خطأ داخلي»
        return _json_response_safe({"error": str(exc)}, status=409)
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ فاتورة الشراء: {exc}"}, status=500)

    return _json_response_safe({"ok": True, "invoice_id": inv.id,
                                "total": float(inv.total_amount)})
