"""
📋 الجرد الفعلي من البوابة — قبل كده كان API للموبايل بس (api/v1/inventory/cycle-count/).

الموظف بيختار الفرع ← يكتب العدد الفعلي جنب كل صنف (أو يسيبه فاضي لو ماعدّوش)
← «اعتماد الجرد». الأصناف اللي عددها اختلف بس هي اللي بتتعدّل، عن طريق
InventoryService.execute_cycle_count (حركة مخزن + قيد «فروقات جرد» من غير أي
حركة نقدية). النتيجة: عدد الأصناف اللي اتعدّلت + العجز والزيادة بالقطعة والقيمة.
"""
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from .models import Branch, Inventory
from .views import _get_branch_for_user, _user_can_edit_branch, tenant_required
from .views.utils import module_required, role_required

COUNT_ROLES = ('admin', 'manager', 'stock')
PAGE_SIZE = 50


def _branch_for(request):
    """فرع الموظف؛ والأدمن (من غير فرع) يختار من الفروع اللي يقدر يعدّل فيها."""
    own = _get_branch_for_user(request.user)
    if own is not None:
        return own, [own]
    branches = [b for b in Branch.objects.order_by('name') if _user_can_edit_branch(request.user, b)]
    wanted = request.POST.get('branch') or request.GET.get('branch')
    chosen = next((b for b in branches if str(b.pk) == str(wanted)), branches[0] if branches else None)
    return chosen, branches


@login_required(login_url='/login/')
@tenant_required
@role_required(*COUNT_ROLES)
@module_required('inventory')
def stock_count(request):
    from inventory.services.inventory_service import InventoryService

    branch, branches = _branch_for(request)
    result = None
    errors = []
    if request.method == 'POST' and branch is not None:
        if not _user_can_edit_branch(request.user, branch):
            errors.append('perm')
        else:
            short_qty = over_qty = 0
            short_value = over_value = Decimal('0')
            changed = []
            rows = {str(i.product_id): i for i in Inventory.objects.select_related('product')
                    .filter(branch=branch, product_id__in=[k[len('count_'):] for k in request.POST
                                                           if k.startswith('count_') and k[6:].isdigit()])}
            for pid, inv in rows.items():
                raw = (request.POST.get(f'count_{pid}') or '').strip()
                if raw == '':
                    continue
                try:
                    actual = int(raw)
                except ValueError:
                    errors.append(inv.product.name)
                    continue
                if actual < 0:
                    errors.append(inv.product.name)
                    continue
                if actual == inv.quantity:
                    continue
                diff, _new = InventoryService.execute_cycle_count(inv.product, branch, actual)
                cost = inv.product.average_cost or inv.product.purchase_price or Decimal('0')
                if diff < 0:
                    short_qty += -diff
                    short_value += -diff * cost
                else:
                    over_qty += diff
                    over_value += diff * cost
                changed.append({'product': inv.product, 'diff': diff, 'after': actual})
            result = {'changed': changed, 'short_qty': short_qty, 'short_value': short_value,
                      'over_qty': over_qty, 'over_value': over_value}

    q = (request.GET.get('q') or request.POST.get('q') or '').strip()
    qs = Inventory.objects.none()
    if branch is not None:
        qs = Inventory.objects.select_related('product').filter(branch=branch)
        if q:
            qs = qs.filter(Q(product__name__icontains=q) | Q(product__part_number__icontains=q)
                           | Q(product__barcode=q))
        qs = qs.order_by('product__name', 'id')
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    return render(request, 'inventory/stock_count.html', {
        'branch': branch, 'branches': branches, 'page': page, 'q': q,
        'result': result, 'errors': errors,
    })
