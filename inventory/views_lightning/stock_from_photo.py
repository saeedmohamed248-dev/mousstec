"""🤖 تحميل مخزون بالتصوير: صورة قائمة ⇒ أصناف ومخزون.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render
from django.views.decorators.http import require_POST
from inventory.models import Branch, Inventory, Product
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required


# =====================================================================
# 🤖 تحميل مخزون بالتصوير — صوّر قائمة منتجات، النظام يحوّلها ويحطّها مخزون
# =====================================================================
def _extract_products_from_upload(up):
    """يرجّع قائمة منتجات من صورة (AI) أو Excel/CSV — للتحميل المباشر للمخزون.

    كل عنصر: {part_number, name, brand, car_model, qty, purchase_price, retail_price}.
    """
    name = (getattr(up, 'name', '') or '').lower()
    image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')
    if name.endswith(image_exts):
        import base64 as _b64
        from inventory.ai_services import scan_products_image_ai
        try:
            data = scan_products_image_ai(_b64.b64encode(up.read()).decode())
        except Exception:
            data = {}
        items = []
        for it in (data or {}).get('items') or []:
            extra = it.get('extra') or {}
            if not isinstance(extra, dict):
                extra = {}
            items.append({
                "part_number": str(it.get('part_number') or '').strip(),
                "name": str(it.get('name') or '').strip(),
                "brand": str(it.get('brand') or '').strip(),
                "car_model": str(it.get('car_model') or '').strip(),
                "qty": it.get('qty') or 1,
                "purchase_price": it.get('purchase_price') or 0,
                "retail_price": it.get('retail_price') or 0,
                "extra": {str(k).strip(): str(v).strip()
                          for k, v in extra.items() if str(v).strip()},
            })
        return items

    # ---- Excel / CSV ----
    rows, header = [], []
    if name.endswith('.csv') or name.endswith('.txt'):
        import csv as _csv
        import io as _io
        text = up.read().decode('utf-8-sig', errors='ignore')
        all_rows = list(_csv.reader(_io.StringIO(text)))
        if all_rows:
            header = [str(h).strip().lower() for h in all_rows[0]]
            rows = all_rows[1:]
    else:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(up, read_only=True, data_only=True)
            data_rows = list(wb.active.iter_rows(values_only=True))
            if data_rows:
                header = [str(h or '').strip().lower() for h in data_rows[0]]
                rows = data_rows[1:]
        except Exception:
            return []

    def _col(keys):
        for idx, h in enumerate(header):
            if any(k in h for k in keys):
                return idx
        return None
    c = {
        'sku': _col(['part', 'sku', 'كود', 'رقم']),
        'name': _col(['name', 'اسم', 'صنف', 'وصف']),
        'brand': _col(['brand', 'ماركة', 'الماركة']),
        'model': _col(['model', 'موديل', 'الموديل']),
        'qty': _col(['qty', 'quantity', 'كمية', 'عدد']),
        'cost': _col(['cost', 'شراء', 'تكلفة']),
        'retail': _col(['retail', 'sale', 'price', 'بيع', 'سعر']),
    }

    def _v(row, idx):
        if idx is None or idx >= len(row):
            return ''
        return row[idx]
    mapped_idx = {i for i in c.values() if i is not None}
    items = []
    for row in rows:
        if not any(str(x).strip() for x in row):
            continue
        # أي عمود مش متطابق مع الحقول المعروفة → حقل إضافي باسم عموده
        extra = {}
        for idx, h in enumerate(header):
            if idx in mapped_idx or not h:
                continue
            val = str(_v(row, idx) or '').strip()
            if val:
                extra[h] = val
        items.append({
            "part_number": str(_v(row, c['sku']) or '').strip(),
            "name": str(_v(row, c['name']) or '').strip(),
            "brand": str(_v(row, c['brand']) or '').strip(),
            "car_model": str(_v(row, c['model']) or '').strip(),
            "qty": _v(row, c['qty']) or 1,
            "purchase_price": _v(row, c['cost']) or 0,
            "retail_price": _v(row, c['retail']) or 0,
            "extra": extra,
        })
    return items


def _apply_extra_fields(product, extra, inv_row=None):
    """يوزّع الحقول الإضافية المكتشَفة على حقول المنتج المعروفة، والباقي يتخزّن
    في product.extra_attributes (JSON) — عشان أي عمود زيادة ما يضيعش."""
    if not isinstance(extra, dict) or not extra:
        return
    leftover = dict(product.extra_attributes or {})
    prod_fields = []
    for raw_key, raw_val in extra.items():
        key = str(raw_key).strip()
        val = str(raw_val).strip()
        if not key or not val:
            continue
        k = key.lower()
        if any(t in k for t in ('barcode', 'باركود', 'باركو')):
            product.barcode = val
            prod_fields.append('barcode')
        elif any(t in k for t in ('engine', 'محرك')):
            product.engine_code = val
            prod_fields.append('engine_code')
        elif any(t in k for t in ('warranty', 'ضمان')):
            digits = ''.join(ch for ch in val if ch.isdigit())
            if digits:
                product.warranty_months = int(digits)
                prod_fields.append('warranty_months')
            else:
                leftover[key] = val
        elif any(t in k for t in ('wholesale', 'جمله', 'جملة')):
            try:
                product.b2b_wholesale_price = Decimal(val)
                prod_fields.append('b2b_wholesale_price')
            except InvalidOperation:
                leftover[key] = val
        elif ('shelf' in k or 'location' in k or 'رف' in k or 'مكان' in k) and inv_row is not None:
            inv_row.shelf_location = val[:50]
        else:
            leftover[key] = val
    if prod_fields:
        product.save(update_fields=list(set(prod_fields)))
    if leftover != (product.extra_attributes or {}):
        product.extra_attributes = leftover
        product.save(update_fields=['extra_attributes'])


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
def inventory_import(request):
    """صفحة تحميل المخزون بالتصوير/Excel مع مراجعة قبل الحفظ."""
    branch = _get_branch_for_user(request.user)
    return render(request, 'inventory/inventory_import.html', {
        'branch': branch,
        'branches': Branch.objects.all().order_by('name') if branch is None else None,
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def inventory_import_extract(request):
    up = request.FILES.get('file')
    if up is None:
        return _json_response_safe({"error": "ارفع صورة أو ملف Excel/CSV أولاً."}, status=400)
    items = _extract_products_from_upload(up)
    out = []
    for it in items:
        sku = (it.get('part_number') or '').strip()
        nm = (it.get('name') or '').strip()
        prod = None
        if sku:
            prod = (Product.objects.filter(part_number__iexact=sku).first()
                    or Product.objects.filter(barcode=sku).first())
        if prod is None and nm:
            prod = Product.objects.filter(name__iexact=nm).first()
        # كمية مكتوبة 0 (خلص) كانت بتتقري 1 ⇒ قطعة وهمية تتحمّل على المخزون
        raw_q = it.get('qty')
        try:
            qty = int(float(raw_q)) if raw_q not in (None, '') else 1
        except (TypeError, ValueError):
            qty = 1
        try:
            cost = float(it.get('purchase_price') or 0)
        except (TypeError, ValueError):
            cost = 0
        try:
            retail = float(it.get('retail_price') or 0)
        except (TypeError, ValueError):
            retail = 0
        extra = it.get('extra') or {}
        if not isinstance(extra, dict):
            extra = {}
        out.append({
            "product_id": prod.id if prod else None,
            "matched_name": prod.name if prod else "",
            "part_number": sku, "name": nm,
            "brand": (it.get('brand') or '').strip(),
            "car_model": (it.get('car_model') or '').strip(),
            "qty": max(qty, 0),
            "purchase_price": max(cost, 0),
            "retail_price": max(retail, 0),
            "extra": {str(k): str(v) for k, v in extra.items()},
        })
    return _json_response_safe({"ok": True, "items": out})


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def inventory_import_save(request):
    """يحفظ البنود المُراجَعة في المخزون: ينشئ المنتجات الجديدة ويزوّد الكميات
    على الفرع مع تسجيل حركة جرد لكل صنف. مش فاتورة شراء — تحميل مخزون مباشر."""
    import json as _json
    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)

    branch = _get_branch_for_user(request.user)
    if branch is None:
        branch = Branch.objects.filter(id=payload.get("branch_id")).first()
    if branch is None:
        return _json_response_safe({"error": "حدّد الفرع اللي هيتحمّل عليه المخزون."}, status=400)
    if not _user_can_edit_branch(request.user, branch):
        return _json_response_safe({"error": "👁 صلاحيتك في الفرع ده عرض فقط."}, status=403)

    rows = payload.get("items") or []
    if not rows:
        return _json_response_safe({"error": "لا توجد بنود للحفظ."}, status=400)

    def _dec(v):
        try:
            d = Decimal(str(v or 0))
            return d if d >= 0 else Decimal("0")
        except InvalidOperation:
            return Decimal("0")

    def _gen_sku():
        # 🐛 [FIX]: كان رقم من الوقت بالمللي ثانية — صنفين جداد في نفس المللي
        #    ثانية (لوب سريع) بياخدوا نفس الكود فالحفظ كله بيفشل بتكرار الكود.
        import uuid as _uuid
        while True:
            sku = f"AUTO-{_uuid.uuid4().hex[:8].upper()}"
            if not Product.objects.filter(part_number=sku).exists():
                return sku

    created, updated = 0, 0
    try:
        with transaction.atomic():
            for raw in rows:
                try:
                    qty = int(float(raw.get("qty") or 0))
                except (TypeError, ValueError):
                    qty = 0
                cost = _dec(raw.get("purchase_price"))
                retail = _dec(raw.get("retail_price"))
                product = None
                pid = raw.get("product_id")
                if pid:
                    product = Product.objects.filter(id=pid).first()
                if product is None:
                    sku = (raw.get("part_number") or '').strip()
                    if sku:
                        product = Product.objects.filter(part_number__iexact=sku).first()
                    if product is None:
                        nm = (raw.get("name") or '').strip()
                        if not nm and not sku:
                            continue  # صف فاضي
                        if not sku or Product.objects.filter(part_number=sku).exists():
                            sku = _gen_sku()
                        product = Product.objects.create(
                            part_number=sku, name=nm or "صنف مستورد",
                            brand=(raw.get("brand") or '').strip() or "—",
                            car_model=(raw.get("car_model") or '').strip() or "—",
                            car_year="—",
                            purchase_price=cost, retail_price=retail, average_cost=cost,
                        )
                        created += 1
                    else:
                        updated += 1
                else:
                    updated += 1
                # زوّد الكمية على الفرع: حركة + متوسط تكلفة مرجّح + قيد بنفس القيمة
                # 🐛 [FIX]: صنف موجود كان بيزيد من غير ما متوسط تكلفته يتحسب، والقيد
                #    بتكلفة الملف (أو صفر لو فاضية) ⇒ الدفتر والمخزن يبعدوا عن بعض.
                inv_row = None
                if qty > 0:
                    from inventory.services.inventory_service import set_branch_stock
                    inv_row, _ = Inventory.objects.select_for_update().get_or_create(
                        product=product, branch=branch, defaults={"quantity": 0})
                    set_branch_stock(product, branch, inv_row.quantity + qty,
                                     unit_cost=cost if cost > 0 else None, user=request.user,
                                     reference_type="InventoryImport", note="تحميل مخزون من صورة/ملف",
                                     opening=True)
                    inv_row.refresh_from_db()
                # 🧩 الحقول الإضافية المكتشَفة → حقول معروفة أو extra_attributes
                _apply_extra_fields(product, raw.get("extra"), inv_row)
                if inv_row is not None:
                    inv_row.save(update_fields=["shelf_location"])
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ المخزون: {exc}"}, status=500)

    return _json_response_safe({"ok": True, "created": created, "updated": updated})
