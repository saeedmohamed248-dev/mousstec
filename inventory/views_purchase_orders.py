"""
📝 أوامر الشراء — من البوابة.

* القائمة + «أمر جديد من النواقص»: الأصناف اللي رصيدها في الفرع وصل لحد التنبيه
  أو أقل (Product.min_stock_level) بتظهر بكمية مقترحة = ضعف الحد − الرصيد.
* صفحة الأمر: «اتبعت للمورد» (ورسالة واتساب جاهزة) / «إلغاء» /
  «استلام ← فاتورة شراء» بيفتح شاشة فاتورة الشراء متعبّية بأصناف الأمر
  (inventory:purchase_create?po=<id>)، وحفظها بيقفل الأمر ويربطه بالفاتورة
  (views_lightning.purchases.purchase_save).
* الأمر نفسه مالوش أي أثر على المخزون أو الحسابات.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Branch, Inventory, Product, PurchaseOrder, PurchaseOrderItem, Vendor
from .views import _get_branch_for_user, _user_can_edit_branch, tenant_required
from .views.utils import module_required, role_required
from clients.services.entitlements import require_feature

PO_ROLES = ('admin', 'manager', 'accountant', 'purchasing')


def _visible(user):
    qs = PurchaseOrder.objects.select_related('vendor', 'branch', 'purchase_invoice')
    branch = _get_branch_for_user(user)
    return (qs.filter(branch=branch) if branch is not None else qs), branch


def shortage_suggestions(branch):
    """الأصناف اللي رصيدها ≤ حد التنبيه في الفرع، بكمية مقترحة = ضعف الحد − الرصيد."""
    rows = (Inventory.objects.select_related('product')
            .filter(branch=branch, product__min_stock_level__gt=0,
                    quantity__lte=F('product__min_stock_level'))
            .order_by('product__name'))
    return [{'product': r.product, 'stock': r.quantity,
             'qty': max(r.product.min_stock_level * 2 - r.quantity, 1),
             'cost': r.product.purchase_price or r.product.average_cost or Decimal('0')}
            for r in rows]


@login_required(login_url='/login/')
@tenant_required
@role_required(*PO_ROLES)
@module_required('purchases')
@require_feature('purchase_orders', allow_without_subscription=True)  # 💎 Silver وأعلى
def po_list(request):
    qs, own_branch = _visible(request.user)
    status = (request.GET.get('status') or '').strip()
    if status in dict(PurchaseOrder.STATUS_CHOICES):
        qs = qs.filter(status=status)
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    branches = ([own_branch] if own_branch is not None
                else [b for b in Branch.objects.order_by('name') if _user_can_edit_branch(request.user, b)])
    wanted = request.GET.get('branch')
    branch = next((b for b in branches if str(b.pk) == str(wanted)), branches[0] if branches else None)
    return render(request, 'inventory/purchase_orders.html', {
        'page': page, 'status': status, 'status_choices': PurchaseOrder.STATUS_CHOICES,
        'branch': branch, 'branches': branches,
        'vendors': Vendor.objects.order_by('name'),
        'suggestions': shortage_suggestions(branch) if branch is not None else [],
        'err': request.GET.get('err', ''),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required(*PO_ROLES)
@module_required('purchases')
@require_feature('purchase_orders', allow_without_subscription=True)  # 💎 Silver وأعلى
@require_POST
def po_create(request):
    """بيعمل أمر من الأصناف المعلّمة في جدول النواقص (pick_<pid>, qty_<pid>, cost_<pid>)."""
    back = reverse('inventory:purchase_order_list')
    vendor = Vendor.objects.filter(pk=request.POST.get('vendor') or 0).first()
    branch = Branch.objects.filter(pk=request.POST.get('branch') or 0).first()
    if vendor is None:
        return redirect(back + '?err=vendor')
    if branch is None or not _user_can_edit_branch(request.user, branch):
        return redirect(back + '?err=branch')
    lines = []
    for key in request.POST:
        if not key.startswith('pick_') or not key[5:].isdigit():
            continue
        pid = key[5:]
        try:
            qty = int(request.POST.get(f'qty_{pid}') or 0)
            cost = Decimal(request.POST.get(f'cost_{pid}') or '0')
        except (ValueError, InvalidOperation):
            return redirect(back + '?err=number')
        if not _sane(qty, cost):
            return redirect(back + '?err=number')
        product = Product.objects.filter(pk=pid).first()
        if product is not None:
            lines.append((product, qty, cost))
    # 🐛 [FIX — دورة ناقصة]: الأمر كان بيتعمل من «النواقص» بس — مفيش طريقة تطلب صنف لسه ماوصلش لحد
    #    التنبيه أو صنف جديد. «أمر فاضي» بيتعمل مسودة والأصناف بتتضاف من صفحته.
    if not lines and request.POST.get('empty') != '1':
        return redirect(back + '?err=empty')
    with transaction.atomic():
        po = PurchaseOrder.objects.create(vendor=vendor, branch=branch, created_by=request.user,
                                          notes=(request.POST.get('notes') or '').strip()[:1000])
        PurchaseOrderItem.objects.bulk_create(
            [PurchaseOrderItem(order=po, product=p, quantity=q, unit_cost=c) for p, q, c in lines])
    return redirect(reverse('inventory:purchase_order_detail', args=[po.pk]))


def _sane(qty, cost):
    """🐛 [FIX]: كمية/سعر بره حدود العمود (unit_cost max_digits=12) كانت بتوقّع الحفظ بـ 500 بدل رسالة."""
    return (0 < qty <= 1_000_000 and cost.is_finite() and Decimal('0') <= cost < Decimal('10000000000'))


def _whatsapp_url(po):
    lines = [f"أمر شراء {po.number} — {po.branch.name}"]
    lines += [f"• {i.product.name} ({i.product.part_number}) × {i.quantity}" for i in po.items.all()]
    phone = ''.join(ch for ch in (getattr(po.vendor, 'phone', '') or '') if ch.isdigit())
    if phone.startswith('0'):
        phone = '2' + phone
    return f"https://wa.me/{phone}?text={quote(chr(10).join(lines))}"


@login_required(login_url='/login/')
@tenant_required
@role_required(*PO_ROLES)
@module_required('purchases')
@require_feature('purchase_orders', allow_without_subscription=True)  # 💎 Silver وأعلى
def po_detail(request, pk):
    qs, _branch = _visible(request.user)
    po = get_object_or_404(qs, pk=pk)
    return render(request, 'inventory/purchase_order_detail.html', {
        'po': po, 'items': po.items.select_related('product'),
        'can_edit': _user_can_edit_branch(request.user, po.branch),
        'whatsapp_url': _whatsapp_url(po),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required(*PO_ROLES)
@module_required('purchases')
@require_feature('purchase_orders', allow_without_subscription=True)  # 💎 Silver وأعلى
@require_POST
def po_action(request, pk, action):
    qs, _branch = _visible(request.user)
    with transaction.atomic():
        po = get_object_or_404(qs.select_for_update(of=('self',)), pk=pk)
        detail = reverse('inventory:purchase_order_detail', args=[po.pk])
        if not _user_can_edit_branch(request.user, po.branch):
            return redirect(detail + '?err=perm')
        if action == 'send' and po.status == 'draft':
            if not po.items.exists():   # 🐛 [FIX]: أمر فاضي (مسودة) مايتبعتش للمورد
                return redirect(detail + '?err=empty')
            po.status = 'sent'
        elif action == 'cancel' and po.is_open:
            po.status = 'cancelled'
        else:
            return redirect(detail + '?err=state')
        po.save(update_fields=['status'])
    return redirect(detail)


@login_required(login_url='/login/')
@tenant_required
@role_required(*PO_ROLES)
@module_required('purchases')
@require_feature('purchase_orders', allow_without_subscription=True)  # 💎 Silver وأعلى
@require_POST
def po_lines(request, pk):
    """✏️ تعديل أصناف أمر شراء لسه «مسودة»: إضافة صنف (رقم القطعة/الباركود) + تعديل الكمية والسعر + شيل سطر.

    🐛 [FIX — دورة ناقصة]: الأمر كان بيتحفظ زي ما اتعمل من جدول النواقص — مفيش تعديل كمية ولا
    إضافة صنف تاني قبل ما يتبعت للمورد.
    """
    qs, _branch = _visible(request.user)
    with transaction.atomic():
        po = get_object_or_404(qs.select_for_update(of=('self',)), pk=pk)
        detail = reverse('inventory:purchase_order_detail', args=[po.pk])
        if not _user_can_edit_branch(request.user, po.branch):
            return redirect(detail + '?err=perm')
        if po.status != 'draft':
            return redirect(detail + '?err=state')
        P = request.POST
        remove = (P.get('remove') or '').strip()
        if remove.isdigit():
            po.items.filter(pk=int(remove)).delete()
            return redirect(detail)
        # كل الأرقام بتتراجع الأول — غلطة في سطر ماتسيبش الأمر نصه متعدّل
        changed, items = [], list(po.items.all())
        for item in items:
            raw_q, raw_c = P.get(f'qty_{item.pk}'), P.get(f'cost_{item.pk}')
            if raw_q is None and raw_c is None:
                continue
            try:
                qty = int(raw_q if raw_q is not None else item.quantity)
                cost = Decimal(raw_c if raw_c is not None else item.unit_cost)
            except (ValueError, InvalidOperation):
                return redirect(detail + '?err=number')
            if not _sane(qty, cost):
                return redirect(detail + '?err=number')
            if (qty, cost) != (item.quantity, item.unit_cost):
                item.quantity, item.unit_cost = qty, cost
                changed.append(item)
        new_line = None
        code = (P.get('new_code') or '').strip()
        if code:
            active = Product.objects.filter(is_active=True)
            product = active.filter(part_number__iexact=code).first() or active.filter(barcode=code).first()
            if product is None:
                return redirect(detail + '?err=product')
            try:
                qty = int(P.get('new_qty') or 1)
                raw_cost = (P.get('new_cost') or '').strip()
                cost = Decimal(raw_cost) if raw_cost else Decimal(
                    str(product.purchase_price or product.average_cost or 0))
            except (ValueError, InvalidOperation):
                return redirect(detail + '?err=number')
            line = next((i for i in items if i.product_id == product.pk), None)
            total_qty = qty + (line.quantity if line is not None else 0)
            if not (_sane(qty, cost) and _sane(total_qty, cost)):
                return redirect(detail + '?err=number')
            new_line = (line, product, total_qty, cost)
        for item in changed:
            if new_line is None or item is not new_line[0]:
                item.save(update_fields=['quantity', 'unit_cost'])
        if new_line is not None:
            line, product, total_qty, cost = new_line
            if line is not None:   # نفس الصنف ⇒ نزوّد الكمية بدل سطر مكرر
                line.quantity, line.unit_cost = total_qty, cost
                line.save(update_fields=['quantity', 'unit_cost'])
            else:
                PurchaseOrderItem.objects.create(order=po, product=product, quantity=total_qty, unit_cost=cost)
    return redirect(detail)
