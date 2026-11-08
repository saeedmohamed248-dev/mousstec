"""
🔢 أرقام السيريال — /system/serials/

* بحث بأي سيريال: الصنف، جه من أنهي فاتورة شراء/مورد، اتباع لمين بأنهي فاتورة وإمتى،
  والضمان لسه ساري ولا لأ (ده سؤال العميل اللي راجع بقطعة).
* تسجيل سيريالات وقت الاستلام: الصنف (رقم القطعة أو الباركود) + سيريال في كل سطر، ومعاها
  فاتورة الشراء لو موجودة. المتكرر بيتعدّى وبيتقال.
* ربط سيريال بفاتورة بيع (لازم الصنف يكون في الفاتورة) ⇒ «اتباع» والضمان من الفاتورة.
* تغيير الحالة: مرتجع / تالف (ضمان مورد) / رجع المخزن.
مالوش أثر على كميات المخزون ولا الحسابات.
"""
from datetime import timedelta
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Branch, Product, PurchaseInvoice, SaleInvoice, SerialNumber
from .views import _get_branch_for_user, tenant_required
from .views.utils import module_required, role_required

VIEW_ROLES = ('admin', 'manager', 'accountant', 'stock', 'cashier', 'purchasing')
EDIT_ROLES = ('admin', 'manager', 'stock', 'purchasing')
MAX_BATCH = 500


def _back(**params):
    url = reverse('inventory:serials')
    return redirect(url + ('?' + '&'.join(f'{k}={quote(str(v))}' for k, v in params.items()) if params else ''))


@login_required(login_url='/login/')
@tenant_required
@role_required(*VIEW_ROLES)
@module_required('inventory')
def serials(request):
    q = (request.GET.get('q') or '').strip()
    qs = SerialNumber.objects.select_related('product', 'branch', 'purchase_invoice__vendor',
                                            'sale_invoice__customer')
    results = list(qs.filter(Q(serial__iexact=q) | Q(serial__icontains=q))[:50]) if q else []
    branch = _get_branch_for_user(request.user)
    return render(request, 'inventory/serials.html', {
        'q': q, 'results': results, 'recent': [] if q else list(qs[:20]),
        'branches': [branch] if branch else list(Branch.objects.order_by('name')),
        'status_choices': SerialNumber.STATUS_CHOICES,
        'flash': request.GET.get('ok', ''), 'err': request.GET.get('err', ''),
        'n': request.GET.get('n', ''), 'dup': request.GET.get('dup', ''),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required(*EDIT_ROLES)
@module_required('inventory')
@require_POST
def serials_register(request):
    P = request.POST
    code = (P.get('product') or '').strip()
    product = (Product.objects.filter(part_number__iexact=code).first()
               or Product.objects.filter(barcode=code).first()) if code else None
    if product is None:
        return _back(err='product')
    branch = _get_branch_for_user(request.user) or Branch.objects.filter(pk=P.get('branch') or 0).first()
    purchase = None
    if (P.get('purchase_invoice') or '').strip():
        purchase = PurchaseInvoice.objects.filter(pk=(P.get('purchase_invoice') or '').strip().lstrip('#') or 0).first()
        if purchase is None:
            return _back(err='purchase')
    lines = [s.strip() for s in (P.get('serials') or '').replace(',', '\n').splitlines() if s.strip()]
    lines = list(dict.fromkeys(lines))[:MAX_BATCH]
    if not lines:
        return _back(err='empty')
    created, dup = 0, []
    for serial in lines:
        try:
            with transaction.atomic():
                SerialNumber.objects.create(product=product, serial=serial[:120], branch=branch,
                                            purchase_invoice=purchase)
            created += 1
        except IntegrityError:
            dup.append(serial)
    return _back(ok='registered', n=created, dup=', '.join(dup[:10]))


@login_required(login_url='/login/')
@tenant_required
@role_required(*VIEW_ROLES)
@module_required('inventory')
@require_POST
def serial_sell(request, pk):
    """ربط السيريال بفاتورة بيع — لازم الصنف يكون من أصناف الفاتورة."""
    sn = get_object_or_404(SerialNumber.objects.select_related('product'), pk=pk)
    inv = SaleInvoice.objects.filter(pk=(request.POST.get('invoice') or '').strip().lstrip('#') or 0,
                                     is_return=False).first()
    if inv is None:
        return _back(q=sn.serial, err='invoice')
    item = inv.items.filter(product=sn.product).first()
    if item is None:
        return _back(q=sn.serial, err='not_in_invoice')
    if sn.status == 'sold' and sn.sale_invoice_id and sn.sale_invoice_id != inv.pk:
        return _back(q=sn.serial, err='already_sold')
    sn.status, sn.sale_invoice, sn.sold_at = 'sold', inv, inv.date_created
    months = sn.product.warranty_months or 0
    sn.warranty_end = item.warranty_end_date or (
        (timezone.localtime(inv.date_created).date() + timedelta(days=30 * months)) if months else None)
    sn.save(update_fields=['status', 'sale_invoice', 'sold_at', 'warranty_end'])
    return _back(q=sn.serial, ok='sold')


@login_required(login_url='/login/')
@tenant_required
@role_required(*EDIT_ROLES)
@module_required('inventory')
@require_POST
def serial_status(request, pk):
    sn = get_object_or_404(SerialNumber, pk=pk)
    status = request.POST.get('status')
    if status not in ('in_stock', 'returned', 'defective'):
        return _back(q=sn.serial, err='status')
    sn.status = status
    if status == 'in_stock':
        sn.sale_invoice, sn.sold_at, sn.warranty_end = None, None, None
    note = (request.POST.get('note') or '').strip()[:255]
    if note:
        sn.note = note
    sn.save()
    return _back(q=sn.serial, ok='status')
