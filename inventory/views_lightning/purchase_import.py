"""📥 رفع فاتورة شراء (Excel/PDF/CSV/صورة) واستخراج البنود ومراجعتها.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render
from django.views.decorators.http import require_POST
from inventory.models import Branch, EmployeeProfile, Product, PurchaseInvoice, Treasury, Vendor
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required
from ._common import _RollbackWith, _finite_dec


# =====================================================================
# 📥 رفع فاتورة شراء (Excel / PDF / CSV / صورة) — استخراج البنود ومراجعتها قبل الحفظ
# (القراءة نفسها في inventory/services/purchase_import.py)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@module_required('purchases')
def invoice_import(request):
    """📥 رفع فاتورة شراء: Excel / PDF / CSV / صورة → مراجعة البنود → حفظ."""
    branch = _get_branch_for_user(request.user)
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    prof = getattr(request.user, 'employee_profile', None)
    can_add_vendor = request.user.is_superuser or (
        prof is not None and EmployeeProfile.canonical_role(prof.role) in ('admin', 'manager', 'accountant'))
    return render(request, 'inventory/invoice_import.html', {
        'branch': branch,
        'branches': Branch.objects.all().order_by('name') if branch is None else None,
        'can_add_vendor': can_add_vendor,
        'vendors': Vendor.objects.all().order_by('name'),
        'treasuries': treasury_qs.select_related('branch').order_by('name'),
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('purchases')
@require_POST
def invoice_import_extract(request):
    """يستقبل الملف المرفوع، يستخرج البنود، ويحاول مطابقتها بمنتجات موجودة."""
    from inventory.services.purchase_import import (
        ImportFileError, extract_purchase_items, flipped_code)
    up = request.FILES.get('file')
    if up is None:
        return _json_response_safe({"error": "ارفع ملف Excel أو PDF أو صورة الفاتورة أولاً."}, status=400)
    try:
        vendor_name, items = extract_purchase_items(up)
    except ImportFileError as exc:
        return _json_response_safe({"error": str(exc)}, status=400)
    from_pdf = (up.name or '').lower().endswith('.pdf')

    def _match(sku, nm):
        if sku:
            prod = (Product.objects.filter(part_number__iexact=sku).first()
                    or Product.objects.filter(barcode=sku).first())
            if prod is not None:
                return prod
            alt = flipped_code(sku) if from_pdf else sku
            if alt != sku:   # كود اتقلب في PDF اتعمل من صفحة عربي
                prod = Product.objects.filter(part_number__iexact=alt).first()
                if prod is not None:
                    return prod
        if nm:
            return Product.objects.filter(name__iexact=nm).first()
        return None

    out = []
    for it in items:
        sku = (it.get('part_number') or '').strip()
        nm = (it.get('name') or '').strip()
        prod = _match(sku, nm)
        out.append({
            "product_id": prod.id if prod else None,
            "matched_name": prod.name if prod else '',
            "part_number": prod.part_number if (prod and sku) else sku,
            "name": nm or (prod.name if prod else ''),
            "qty": max(int(it.get('qty') or 1), 1), "cost": max(float(it.get('cost') or 0), 0),
            "sell": max(float(it.get('sell') or 0), 0),
            "wholesale": max(float(it.get('wholesale') or 0), 0),
            "cost_guess": it.get('cost_guess') or '',
        })
    vendor = None
    if vendor_name:
        vendor = (Vendor.objects.filter(name__iexact=vendor_name).first()
                  or Vendor.objects.filter(name__icontains=vendor_name).first())
    return _json_response_safe({"ok": True, "vendor_name": vendor_name,
                                "vendor_id": vendor.id if vendor else None, "items": out})


@login_required(login_url='/login/')
@tenant_required
@module_required('purchases')
@require_POST
def invoice_import_save(request):
    """يحفظ البنود المُراجَعة كفاتورة شراء — بينشئ المنتجات غير الموجودة تلقائياً
    ثم يعتمد الفاتورة (execute_purchase بيزوّد المخزون والمورد والخزنة)."""
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

    rows = payload.get("items") or []
    if not rows:
        return _json_response_safe({"error": "لا توجد بنود للحفظ."}, status=400)

    treasury = None
    paid = Decimal("0")
    if payload.get("treasury_id") and payload.get("paid_amount") not in (None, ""):
        treasury = Treasury.objects.filter(id=payload.get("treasury_id"),
                                           is_active=True, branch=branch).first()
        paid = _finite_dec(payload.get("paid_amount"), Decimal("0"))

    def _gen_sku():
        # 🐛 [FIX]: كان رقم من الوقت بالمللي ثانية — صنفين جداد في نفس المللي
        #    ثانية (لوب سريع) بياخدوا نفس الكود فالحفظ كله بيفشل بتكرار الكود.
        import uuid as _uuid
        while True:
            sku = f"AUTO-{_uuid.uuid4().hex[:8].upper()}"
            if not Product.objects.filter(part_number=sku).exists():
                return sku

    try:
        with transaction.atomic():
            inv = PurchaseInvoice.objects.create(vendor=vendor, branch=branch, status='draft')
            total = Decimal("0")
            for raw in rows:
                try:
                    qty = int(float(raw.get("qty") or 0))
                    cost = Decimal(str(raw.get("cost") or 0))
                except (InvalidOperation, TypeError, ValueError):
                    raise _RollbackWith(_json_response_safe(
                        {"error": "كمية أو سعر غير صالح في أحد البنود."}, status=400))
                if qty <= 0 or cost < 0:
                    continue
                product = None
                pid = raw.get("product_id")
                if pid:
                    product = Product.objects.filter(id=pid).first()
                if product is None:
                    sku = (raw.get("part_number") or '').strip()
                    if sku:
                        product = Product.objects.filter(part_number__iexact=sku).first()
                    if product is None:
                        nm = (raw.get("name") or '').strip() or "صنف مستورد"
                        if not sku or Product.objects.filter(part_number=sku).exists():
                            sku = _gen_sku()
                        try:
                            sell = Decimal(str(raw.get("sell") or 0))
                            whole = Decimal(str(raw.get("wholesale") or 0))
                        except InvalidOperation:
                            sell = whole = Decimal("0")
                        product = Product.objects.create(
                            part_number=sku, name=nm, brand="—",
                            car_model="—", car_year="—",
                            purchase_price=cost, retail_price=sell if sell > 0 else cost,
                            average_cost=cost,
                            # سعر «جملة» مش فوق سعر الشراء = سعر المورد، مش سعر بيعنا للتجار
                            b2b_wholesale_price=whole if whole > cost else Decimal("0"),
                        )
                PurchaseInvoiceItem.objects.create(
                    invoice=inv, product=product, quantity=qty, cost_price=cost)
                total += Decimal(str(qty)) * cost
            inv.update_total()
            if inv.total_amount <= 0:
                raise _RollbackWith(_json_response_safe({"error": "لا توجد بنود صالحة للحفظ."}, status=400))
            if paid > inv.total_amount:
                paid = Decimal(str(inv.total_amount))
            if treasury is not None and paid > 0:
                locked = Treasury.objects.select_for_update().get(pk=treasury.pk)
                if (locked.balance or Decimal("0")) < paid:
                    # 🐛 [FIX]: الـ return من جوه atomic كان بيعمل commit — فاتورة
                    #    مسودة وأصناف جديدة بتفضل في الداتابيز رغم إن الحفظ «فشل».
                    raise _RollbackWith(_json_response_safe(
                        {"error": f"رصيد الخزنة غير كافٍ للدفع (متاح: {locked.balance})."}, status=409))
                inv.treasury = treasury
                inv.paid_amount = paid
                inv.save(update_fields=["treasury", "paid_amount"])
            inv.status = 'posted'
            inv.save()
    except _RollbackWith as rb:
        return rb.response
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ الفاتورة: {exc}"}, status=500)

    return _json_response_safe({"ok": True, "invoice_id": inv.id})
