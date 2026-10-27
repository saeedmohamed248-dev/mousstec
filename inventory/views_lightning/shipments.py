"""📦 الشحنة المجمّعة: مصاريف مشتركة على كذا فاتورة شراء.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import Branch, PurchaseInvoice, Treasury
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required, role_required


# =====================================================================
# 📦 الشحنة المجمّعة — مصاريف مشتركة على كذا فاتورة شراء
# =====================================================================
def _shipment_candidates(branch, shipment=None):
    """فواتير شراء ممكن تدخل الشحنة: معتمدة، من غير مرتجع، ومش تبع شحنة تانية."""
    qs = (PurchaseInvoice.objects.select_related('vendor', 'branch')
          .filter(status='posted', is_applied=True, returned_amount=0)
          .order_by('-date_created'))
    if branch is not None:
        qs = qs.filter(branch=branch)
    if shipment is not None and shipment.pk:
        qs = qs.filter(Q(shipment__isnull=True) | Q(shipment=shipment))
    else:
        qs = qs.filter(shipment__isnull=True)
    return qs[:300]


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
def shipment_list(request):
    """📦 قائمة الشحنات المجمّعة."""
    from inventory.models import PurchaseShipment
    branch = _get_branch_for_user(request.user)
    qs = (PurchaseShipment.objects.select_related('branch')
          .annotate(n_invoices=Count('invoices', distinct=True))
          .order_by('-date', '-id'))   # ترقيم صفحات ثابت (كان من غير ترتيب)
    if branch is not None:
        qs = qs.filter(branch=branch)
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    rows = [{'s': s, 'goods': s.goods_total, 'costs': s.costs_total} for s in page.object_list]
    return render(request, 'inventory/shipment_list.html', {
        'page': page, 'rows': rows, 'branch': branch,
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
def shipment_form(request, pk=None):
    """📦 إنشاء/تعديل شحنة مجمّعة: اختيار الفواتير + المصاريف المشتركة."""
    import json as _json
    from inventory.models import PurchaseShipment, PurchaseInvoiceExtraCost
    branch = _get_branch_for_user(request.user)
    shipment = None
    if pk is not None:
        shipment = PurchaseShipment.objects.filter(pk=pk).select_related('branch').first()
        if shipment is None:
            return redirect(reverse('inventory:shipment_list') + '?err=notfound')
        if branch is not None and shipment.branch_id != branch.id:
            return redirect(reverse('inventory:shipment_list') + '?err=branch')
    treasury_qs = Treasury.objects.filter(is_active=True).select_related('branch')
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    invoices = [{
        'id': inv.id, 'vendor': inv.vendor.name, 'branch_id': inv.branch_id,
        'branch': inv.branch.name, 'date': inv.date_created.strftime('%Y-%m-%d'),
        'total': float(inv.total_amount or 0),
    } for inv in _shipment_candidates(branch, shipment)]
    edit = None
    if shipment is not None:
        edit = {
            'id': shipment.id, 'name': shipment.name, 'notes': shipment.notes,
            'branch_id': shipment.branch_id,
            'invoice_ids': list(shipment.invoices.values_list('id', flat=True)),
            'costs': [{'kind': c.kind, 'behavior': c.behavior, 'label': c.label,
                       'amount': float(c.amount), 'treasury_id': c.treasury_id}
                      for c in shipment.costs.all()],
        }
    return render(request, 'inventory/shipment_form.html', {
        'shipment': shipment, 'branch': branch,
        'branches': Branch.objects.all().order_by('name') if branch is None else None,
        'treasuries': treasury_qs.order_by('name'),
        'kinds': PurchaseInvoiceExtraCost.KIND_CHOICES,
        # «<» متهرّب عشان اسم مورد زي «</script>» مايقفلش السكريبت
        'invoices_json': _json.dumps(invoices, ensure_ascii=False).replace('<', '\\u003c'),
        'edit_json': (_json.dumps(edit, ensure_ascii=False).replace('<', '\\u003c')
                      if edit else 'null'),
        # 📦 جاي من قائمة المشتريات بفواتير متعلّم عليها (?invoices=6,7,8)
        'preselect_json': _json.dumps(sorted({
            int(x) for x in (request.GET.get('invoices') or '').split(',') if x.strip().isdigit()
        })),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
@require_POST
def shipment_save(request):
    """💾 حفظ الشحنة: يوزّع المصاريف على الفواتير بالقيمة ويعيد اعتمادها."""
    import json as _json
    from django.core.exceptions import ValidationError
    from inventory.models import PurchaseShipment, PurchaseInvoiceExtraCost
    from inventory.services.shipment_service import apply_shipment
    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)

    name = (payload.get("name") or "").strip()[:120]
    if not name:
        return _json_response_safe({"error": "اكتب اسم للشحنة (مثلاً: شحنة الإمارات أكتوبر)."}, status=400)
    try:
        invoice_ids = sorted({int(i) for i in (payload.get("invoice_ids") or [])})
    except (TypeError, ValueError):
        return _json_response_safe({"error": "فواتير غير صالحة."}, status=400)
    if len(invoice_ids) < 1:
        return _json_response_safe({"error": "اختار فاتورة واحدة على الأقل."}, status=400)

    shipment = None
    if payload.get("id"):
        shipment = PurchaseShipment.objects.filter(pk=payload.get("id")).first()
        if shipment is None:
            return _json_response_safe({"error": "الشحنة مش موجودة."}, status=404)
        branch = shipment.branch
    else:
        branch = _get_branch_for_user(request.user)
        if branch is None:
            branch = Branch.objects.filter(id=payload.get("branch_id") or 0).first()
        if branch is None:
            return _json_response_safe({"error": "حدّد الفرع."}, status=400)
    scoped = _get_branch_for_user(request.user)
    if scoped is not None and branch.id != scoped.id:
        return _json_response_safe({"error": "الشحنة تخص فرعاً آخر."}, status=403)
    if not _user_can_edit_branch(request.user, branch):
        return _json_response_safe({"error": "👁 صلاحيتك في الفرع ده عرض فقط."}, status=403)

    valid_kinds = {c[0] for c in PurchaseInvoiceExtraCost.KIND_CHOICES}
    valid_behaviors = {c[0] for c in PurchaseInvoiceExtraCost.BEHAVIOR_CHOICES}
    costs = []
    for raw in (payload.get("costs") or []):
        try:
            amount = Decimal(str(raw.get("amount") or "0"))
        except (InvalidOperation, TypeError):
            return _json_response_safe({"error": "مبلغ مصروف غير صالح."}, status=400)
        if not amount.is_finite() or amount < 0:
            return _json_response_safe({"error": "مبلغ مصروف غير صالح."}, status=400)
        if amount == 0:
            continue
        kind = (raw.get("kind") or "other").strip()
        if kind not in valid_kinds:
            kind = "other"
        behavior = (raw.get("behavior") or "").strip()
        if behavior not in valid_behaviors:
            behavior = PurchaseInvoiceExtraCost.default_behavior_for(kind)
        treasury = None
        if raw.get("treasury_id"):
            treasury = Treasury.objects.filter(id=raw.get("treasury_id"), is_active=True,
                                               branch=branch).first()
            if treasury is None:
                return _json_response_safe({"error": "خزنة مصروف مش في فرع الشحنة."}, status=400)
        costs.append({"kind": kind, "behavior": behavior, "amount": amount,
                      "label": (raw.get("label") or "").strip()[:120],
                      "treasury": treasury, "expense_category": None})

    try:
        with transaction.atomic():
            if shipment is None:
                shipment = PurchaseShipment.objects.create(
                    name=name, branch=branch, notes=(payload.get("notes") or "").strip(),
                    created_by=request.user)
            else:
                shipment.name = name
                shipment.notes = (payload.get("notes") or "").strip()
                shipment.save(update_fields=["name", "notes"])
            apply_shipment(shipment, invoice_ids, costs)
    except ValidationError as exc:
        return _json_response_safe({"error": "؛ ".join(exc.messages)}, status=409)
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ الشحنة: {exc}"}, status=500)
    return _json_response_safe({"ok": True, "id": shipment.id})


# نوع المصروف من كلام الـ AI (أو الإيصال) → kind بتاعنا
_EXPENSE_KIND_WORDS = (
    ('customs', ('custom', 'جمرك', 'جمارك', 'تخليص')),
    ('shipping', ('ship', 'freight', 'cargo', 'شحن', 'نولون')),
    ('loading', ('load', 'تحميل', 'تنزيل', 'عتال', 'ونش')),
    ('insurance', ('insur', 'تأمين', 'تامين')),
    ('travel', ('travel', 'flight', 'ticket', 'hotel', 'taxi', 'سفر', 'طيران', 'تذكر', 'تذاكر', 'فندق', 'مواصلات', 'تاكسي')),
    ('food', ('food', 'meal', 'restaurant', 'أكل', 'اكل', 'إعاشة', 'اعاشة', 'مطعم')),
)


def _normalize_expense_kind(raw_kind, label=''):
    from inventory.models import PurchaseInvoiceExtraCost
    valid = {c[0] for c in PurchaseInvoiceExtraCost.KIND_CHOICES}
    k = str(raw_kind or '').strip().lower()
    if k in valid:
        return k
    text = f"{k} {label or ''}".lower()
    for kind, words in _EXPENSE_KIND_WORDS:
        if any(w in text for w in words):
            return kind
    return 'other'


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
@require_POST
def shipment_expenses_extract(request):
    """📷 يقرا بنود مصاريف الشحنة من صورة (أو أكتر) ويرجّعها للمراجعة قبل الحفظ."""
    import base64 as _b64
    from inventory.models import PurchaseInvoiceExtraCost
    from inventory.ai_services import scan_expenses_image_ai
    files = request.FILES.getlist('files') or ([request.FILES['file']] if 'file' in request.FILES else [])
    if not files:
        return _json_response_safe({"error": "ارفع صورة المصاريف."}, status=400)
    from inventory.services.image_normalize import normalize_image_to_jpeg
    image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.heic', '.heif')
    items = []
    for up in files[:10]:
        is_image = ((getattr(up, 'content_type', '') or '').startswith('image/')
                    or (getattr(up, 'name', '') or '').lower().endswith(image_exts))
        if not is_image:
            return _json_response_safe({"error": f"«{up.name}» مش صورة — ارفع صور بس."}, status=400)
        # 📱 صورة الموبايل (كبيرة/HEIC) → JPEG ≤1600px قبل الـ AI: أسرع بكتير،
        #    وHEIC كان بيتبعت للـ AI على إنه JPEG فبيترفض.
        small = normalize_image_to_jpeg(up, max_dim=1600)
        raw = small.read() if small is not None else up.read()
        try:
            data = scan_expenses_image_ai(_b64.b64encode(raw).decode())
        except Exception:  # noqa: BLE001 — فشل القراءة مايوقّعش الصفحة
            data = {}
        for raw in (data or {}).get('items') or []:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get('label') or '').strip()[:120]
            amt_raw = str(raw.get('amount') or '').replace(',', '').replace('٬', '').strip()
            amt_raw = amt_raw.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩٫', '0123456789.'))
            try:
                amount = Decimal(amt_raw)
            except (InvalidOperation, ValueError):
                continue
            if not amount.is_finite() or amount <= 0:
                continue
            kind = _normalize_expense_kind(raw.get('kind'), label)
            currency = str(raw.get('currency') or '').strip()[:10]
            items.append({
                "kind": kind,
                "behavior": PurchaseInvoiceExtraCost.default_behavior_for(kind),
                "label": label,
                "amount": float(amount.quantize(Decimal('0.01'))),
                "currency": currency,
            })
    if not items:
        return _json_response_safe({"error": "مقدرتش أقرا مصاريف من الصورة — جرّب صورة أوضح أو اكتبها بإيدك."}, status=422)
    return _json_response_safe({"ok": True, "items": items})


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('purchases')
@require_POST
def shipment_delete(request, pk):
    """🗑️ حذف الشحنة: يشيل أنصبة مصاريفها من الفواتير ويعيد اعتمادها، ثم يحذفها."""
    from django.core.exceptions import ValidationError
    from inventory.models import PurchaseShipment
    from inventory.services.shipment_service import apply_shipment
    shipment = PurchaseShipment.objects.filter(pk=pk).select_related('branch').first()
    if shipment is None:
        return redirect(reverse('inventory:shipment_list') + '?err=notfound')
    scoped = _get_branch_for_user(request.user)
    if (scoped is not None and shipment.branch_id != scoped.id) or \
            not _user_can_edit_branch(request.user, shipment.branch):
        return redirect(reverse('inventory:shipment_list') + '?err=perm')
    try:
        with transaction.atomic():
            apply_shipment(shipment, [], [])
            shipment.delete()
    except ValidationError as exc:
        from urllib.parse import quote
        return redirect(reverse('inventory:shipment_list') + '?err=' + quote("؛ ".join(exc.messages)))
    return redirect(reverse('inventory:shipment_list') + '?ok=deleted')
