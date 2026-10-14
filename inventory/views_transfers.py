"""
🚚 تحويلات المخزون بين الفروع — من البوابة (مش من لوحة الأدمن بس).

الدورة الكاملة (نفس محرّك InventoryService.apply_transfer_transition):
    تحويل جديد ← «في الطريق» (البضاعة خرجت من فرع المصدر)
               ← «تم الاستلام» (دخلت فرع الوصول)
    أو «إلغاء» وهو في الطريق ← اللي خرج بيرجع لفرع المصدر.

* الخروج والإلغاء من صلاحية فرع المصدر، والاستلام من صلاحية فرع الوصول —
  فكل فرع بيأكّد بنفسه اللي خرج منه أو اللي وصله فعلاً.
* كذا صنف في تحويل واحد بيتعمل ذرّياً: لو صنف واحد رصيده مايكفيش، مفيش ولا
  صنف بيتحرّك.
"""
import json
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import Branch, Inventory, Product, StockTransfer
from .views import _get_branch_for_user, _json_response_safe, _user_can_edit_branch, tenant_required
from .views.utils import module_required, role_required

TRANSFER_ROLES = ('admin', 'manager', 'accountant', 'stock', 'purchasing')
MAX_LINES = 200


def _visible_transfers(user):
    """التحويلات اللي الموظف يشوفها: الطالعة من فرعه أو الداخلة له (الأدمن: الكل)."""
    qs = StockTransfer.objects.select_related('product', 'from_branch', 'to_branch')
    branch = _get_branch_for_user(user)
    if branch is not None:
        qs = qs.filter(Q(from_branch=branch) | Q(to_branch=branch))
    return qs, branch


def _editable_branches(user):
    return [b for b in Branch.objects.order_by('name') if _user_can_edit_branch(user, b)]


@login_required(login_url='/login/')
@tenant_required
@role_required(*TRANSFER_ROLES)
@module_required('inventory')
def stock_transfer_list(request):
    """📋 قائمة التحويلات + فورم تحويل جديد."""
    qs, branch = _visible_transfers(request.user)
    status = (request.GET.get('status') or '').strip()
    if status in dict(StockTransfer.STATUS_CHOICES):
        qs = qs.filter(status=status)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(product__name__icontains=q) | Q(product__part_number__icontains=q))
    page = Paginator(qs.order_by('-date_transferred', '-id'), 30).get_page(request.GET.get('page'))
    rows = []
    for t in page.object_list:
        rows.append({
            't': t,
            'can_ship': t.status == 'pending' and _user_can_edit_branch(request.user, t.from_branch),
            'can_receive': (t.status in ('pending', 'in_transit')
                            and _user_can_edit_branch(request.user, t.to_branch)),
            'can_cancel': (t.status in ('pending', 'in_transit')
                           and _user_can_edit_branch(request.user, t.from_branch)),
        })
    counts = {k: n for k, n in ((s, _visible_transfers(request.user)[0].filter(status=s).count())
                                for s in ('pending', 'in_transit'))}
    from_branches = _editable_branches(request.user)
    if branch is not None:
        from_branches = [b for b in from_branches if b.pk == branch.pk] or from_branches
    return render(request, 'inventory/stock_transfers.html', {
        'page': page, 'rows': rows, 'branch': branch,
        'status': status, 'q': q, 'counts': counts,
        'status_choices': StockTransfer.STATUS_CHOICES,
        'from_branches': from_branches,
        'all_branches': Branch.objects.order_by('name'),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
        'msg': request.GET.get('msg', ''), 'n': request.GET.get('n', ''),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required(*TRANSFER_ROLES)
@module_required('inventory')
@require_POST
def stock_transfer_create(request):
    """➕ تحويل جديد (صنف أو أكتر) — JSON:
    {from_branch_id, to_branch_id, ship: bool, items: [{product_id, qty}]}"""
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _json_response_safe({'error': 'بيانات غير صالحة.'}, status=400)

    branch = _get_branch_for_user(request.user)
    from_branch = branch or Branch.objects.filter(pk=payload.get('from_branch_id')).first()
    to_branch = Branch.objects.filter(pk=payload.get('to_branch_id')).first()
    if from_branch is None or to_branch is None:
        return _json_response_safe({'error': 'اختار فرع المصدر وفرع الوصول.'}, status=400)
    if from_branch.pk == to_branch.pk:
        return _json_response_safe({'error': 'فرع المصدر وفرع الوصول لازم يبقوا مختلفين.'}, status=400)
    if not _user_can_edit_branch(request.user, from_branch):
        return _json_response_safe({'error': '👁 صلاحيتك على فرع المصدر عرض فقط.'}, status=403)

    items = payload.get('items') or []
    if not items:
        return _json_response_safe({'error': 'أضف صنف واحد على الأقل.'}, status=400)
    if len(items) > MAX_LINES:
        return _json_response_safe({'error': f'أقصى عدد أصناف في التحويل {MAX_LINES}.'}, status=400)

    wanted = {}
    for raw in items:
        try:
            pid, qty = int(raw.get('product_id')), int(raw.get('qty') or 0)
        except (TypeError, ValueError, AttributeError):
            return _json_response_safe({'error': 'صنف أو كمية غير صالحة.'}, status=400)
        if qty <= 0:
            return _json_response_safe({'error': 'الكمية لازم تبقى أكبر من صفر.'}, status=400)
        wanted[pid] = wanted.get(pid, 0) + qty   # نفس الصنف على سطرين = سطر واحد

    products = Product.objects.in_bulk(list(wanted))
    missing = [pid for pid in wanted if pid not in products]
    if missing:
        return _json_response_safe({'error': f'صنف #{missing[0]} مش موجود.'}, status=404)

    status = 'in_transit' if payload.get('ship', True) else 'pending'
    created = []
    try:
        with transaction.atomic():
            for pid in sorted(wanted):
                qty = wanted[pid]
                have = (Inventory.objects.select_for_update()
                        .filter(product_id=pid, branch=from_branch)
                        .values_list('quantity', flat=True).first()) or 0
                if have < qty:
                    raise ValidationError(
                        f"رصيد «{products[pid].name}» في {from_branch.name} ({have}) "
                        f"مايكفيش لتحويل {qty}.")
                created.append(StockTransfer.objects.create(
                    product_id=pid, from_branch=from_branch, to_branch=to_branch,
                    quantity=qty, status=status))
    except ValidationError as exc:
        return _json_response_safe({'error': '؛ '.join(exc.messages)}, status=409)
    return _json_response_safe({
        'ok': True, 'ids': [t.pk for t in created], 'status': status,
        'redirect': f"{reverse('inventory:stock_transfer_list')}?ok=created&n={len(created)}",
    })


_ACTIONS = {
    # action: (allowed from-statuses, new status, which branch must be editable)
    'ship': (('pending',), 'in_transit', 'from'),
    'receive': (('pending', 'in_transit'), 'completed', 'to'),
    'cancel': (('pending', 'in_transit'), 'cancelled', 'from'),
}


@login_required(login_url='/login/')
@tenant_required
@role_required(*TRANSFER_ROLES)
@module_required('inventory')
@require_POST
def stock_transfer_action(request, pk, action):
    """📦 خروج / ✅ استلام / ↩️ إلغاء تحويل."""
    back = reverse('inventory:stock_transfer_list')
    nxt = request.POST.get('next') or ''
    if nxt.startswith('/') and url_has_allowed_host_and_scheme(
            nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        back = nxt.split('#')[0]
    sep = '&' if '?' in back else '?'
    if action not in _ACTIONS:
        return redirect(f"{back}{sep}err=action")
    allowed, new_status, side = _ACTIONS[action]
    qs, _branch = _visible_transfers(request.user)
    transfer = qs.filter(pk=pk).first()
    if transfer is None:
        return redirect(f"{back}{sep}err=notfound")
    branch = transfer.from_branch if side == 'from' else transfer.to_branch
    if not _user_can_edit_branch(request.user, branch):
        return redirect(f"{back}{sep}err=perm")
    try:
        with transaction.atomic():
            locked = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
            if locked.status not in allowed:
                raise ValidationError('حالة التحويل اتغيّرت — حدّث الصفحة.')
            locked.status = new_status
            locked.save()   # الإشارة بتحرّك المخزون (InventoryService)
    except ValidationError as exc:
        return redirect(f"{back}{sep}{urlencode({'err': 'msg', 'msg': '؛ '.join(exc.messages)[:200]})}")
    return redirect(f"{back}{sep}ok={action}")
