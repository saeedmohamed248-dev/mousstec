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
        if qty <= 0 or cost < 0 or not cost.is_finite():
            return redirect(back + '?err=number')
        product = Product.objects.filter(pk=pid).first()
        if product is not None:
            lines.append((product, qty, cost))
    if not lines:
        return redirect(back + '?err=empty')
    with transaction.atomic():
        po = PurchaseOrder.objects.create(vendor=vendor, branch=branch, created_by=request.user,
                                          notes=(request.POST.get('notes') or '').strip()[:1000])
        PurchaseOrderItem.objects.bulk_create(
            [PurchaseOrderItem(order=po, product=p, quantity=q, unit_cost=c) for p, q, c in lines])
    return redirect(reverse('inventory:purchase_order_detail', args=[po.pk]))


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
            po.status = 'sent'
        elif action == 'cancel' and po.is_open:
            po.status = 'cancelled'
        else:
            return redirect(detail + '?err=state')
        po.save(update_fields=['status'])
    return redirect(detail)
