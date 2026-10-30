"""📥 استيراد الأصناف من Excel/CSV مع كمية الفرع.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
import re as _re
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render
from inventory.models import Branch, Inventory, Product
from inventory.views import _get_branch_for_user, _user_can_edit_branch, tenant_required
from inventory.views.utils import role_required
from ._common import _finite_dec


# =====================================================================
# 📥 استيراد المنتجات من Excel/CSV — دفعة واحدة مع كمية الفرع النشط
# =====================================================================
_IMPORT_HEADERS = ["part_number", "name", "brand", "car_model",
                   "purchase_price", "retail_price", "quantity", "min_stock_level"]


# 🧠 كلمات مفتاحية لكل حقل — بنطابق أي عنوان عمود مهما كان اسمه أو ترتيبه
# (عربي/إنجليزي/مختصر) عشان السيستم «ينظّم» أي ملف تلقائياً.
_FIELD_KEYWORDS = {
    "part_number": ["product code", "productcode", "part", "sku", "code", "رقم", "كود"],
    "barcode": ["barcode", "بار كود", "باركود", "الباركود"],
    "name": ["name", "desc", "item", "product", "اسم", "الصنف", "صنف", "وصف", "بيان", "المنتج"],
    "brand": ["brand", "make", "ماركة", "الماركة", "شركة"],
    "car_model": ["model", "موديل", "الموديل", "موديلات", "توافق", "سياره", "سيارة"],
    "purchase_price": ["purchase", "cost", "buy", "شراء", "تكلفة", "التكلفة", "الشراء"],
    "retail_price": ["retail", "sell", "sale", "price", "بيع", "البيع", "سعر", "السعر"],
    # ملحوظة: مفيش "stock"/"count" لوحدهم عشان ما يتلغبطوش مع TrackStock/Discount.
    "quantity": ["quantity", "qty", "stock balance", "stock qty", "balance", "on hand", "onhand",
                 "available", "in stock", "instock",
                 "كمية", "الكمية", "الكميه", "كميه", "عدد", "العدد", "رصيد", "الرصيد", "المتاح", "متاح",
                 "متوفر", "المتوفر", "مخزون", "المخزون", "بالمخزن", "عدد القطع"],
    "min_stock_level": ["low stock", "lowstock", "reorder", "threshold", "threshol", "min stock", "minstock",
                        "alert", "تنبيه", "حد التنبيه", "حد الأمان", "حد الامان", "الحد الادنى", "الحد الأدنى"],
}


# ترتيب الأولوية عند التطابق (part_number قبل name عشان "رقم الصنف" ما يتاخدش كـ name)
_FIELD_ORDER = ["part_number", "barcode", "quantity", "purchase_price", "retail_price",
                "min_stock_level", "brand", "car_model", "name"]


def _resolve_columns(headers):
    """يرجّع dict {field: index} بمطابقة عناوين الأعمدة مهما كانت مسمّياتها.

    أولاً تطابق مباشر بالاسم القانوني، وبعدين مطابقة بالكلمات المفتاحية.
    كل عمود يتربط بحقل واحد بس (أول تطابق يكسب)، وكل حقل يتاخد مرة واحدة.
    """
    def _n(h):
        h = str(h or "").strip()
        # نفصل camelCase: StockBalance → "stock balance"، LowStockThreshol → "low stock threshol"
        h = _re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', h)
        return h.lower().replace("_", " ").replace("-", " ")
    norm = [_n(h) for h in headers]
    mapping = {}
    used_idx = set()

    # 1) تطابق مباشر بالاسم القانوني الإنجليزي
    for field in _FIELD_ORDER:
        for i, h in enumerate(norm):
            if i in used_idx:
                continue
            if h.replace(" ", "_") == field or h.replace(" ", "") == field.replace("_", ""):
                mapping[field] = i
                used_idx.add(i)
                break

    # 2) مطابقة بالكلمات المفتاحية للباقي
    for field in _FIELD_ORDER:
        if field in mapping:
            continue
        for i, h in enumerate(norm):
            if i in used_idx:
                continue
            if any(kw in h for kw in _FIELD_KEYWORDS[field]):
                mapping[field] = i
                used_idx.add(i)
                break
    return mapping


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'stock')
def product_import(request):
    """استيراد منتجات من ملف Excel (.xlsx) أو CSV، ويحط الكمية على الفرع النشط."""
    branch = _get_branch_for_user(request.user)

    if request.method == 'GET':
        # قالب CSV جاهز (يفتح في Excel)
        if request.GET.get('template'):
            import csv as _csv
            from django.http import HttpResponse
            resp = HttpResponse(content_type='text/csv; charset=utf-8')
            resp['Content-Disposition'] = 'attachment; filename="mousstec_products_template.csv"'
            resp.write('﻿')  # BOM عشان العربي يظهر صح في Excel
            w = _csv.writer(resp)
            w.writerow(_IMPORT_HEADERS)
            w.writerow(["SKU-001", "فلتر زيت", "BMW", "X5", "100", "150", "10", "2"])
            w.writerow(["SKU-002", "طلمبة مياه", "BMW", "E90", "800", "1200", "4", "1"])
            return resp
        return render(request, 'inventory/product_import.html', {
            'branch': branch,
            'branches': Branch.objects.all().order_by('name') if branch is None else None,
            'headers': _IMPORT_HEADERS,
        })

    # ---- POST: معالجة الملف ----
    up = request.FILES.get('file')
    if not up:
        return render(request, 'inventory/product_import.html',
                      {'branch': branch, 'headers': _IMPORT_HEADERS,
                       'branches': Branch.objects.all().order_by('name') if branch is None else None,
                       'error': 'اختر ملف Excel أو CSV أولاً.'})

    # الفرع اللي هيتحط عليه المخزون
    if branch is None:
        bid = request.POST.get('branch')
        branch = Branch.objects.filter(id=bid).first() if bid else None
    if branch is None:
        return render(request, 'inventory/product_import.html',
                      {'branch': None, 'headers': _IMPORT_HEADERS,
                       'branches': Branch.objects.all().order_by('name'),
                       'error': 'اختر الفرع اللي هترفع عليه المنتجات.'})
    if not _user_can_edit_branch(request.user, branch):
        return render(request, 'inventory/product_import.html',
                      {'branch': branch, 'headers': _IMPORT_HEADERS,
                       'error': '👁 صلاحيتك في الفرع ده عرض فقط — مش مسموح بالرفع.'})

    import tablib
    raw = up.read()
    fname = (up.name or '').lower()
    ds = tablib.Dataset()
    try:
        if fname.endswith('.csv'):
            ds.load(raw.decode('utf-8-sig'), format='csv')
        elif fname.endswith(('.tsv', '.txt')):
            ds.load(raw.decode('utf-8-sig'), format='tsv')
        elif fname.endswith('.json'):
            ds.load(raw.decode('utf-8-sig'), format='json')
        elif fname.endswith(('.xlsx', '.xlsm')):
            ds.load(raw, format='xlsx')
        elif fname.endswith('.xls'):
            ds.load(raw, format='xls')
        elif fname.endswith('.ods'):
            ds.load(raw, format='ods')
        else:
            # آخر محاولة: جرّب CSV (بيغطي أغلب الملفات النصية)
            ds.load(raw.decode('utf-8-sig'), format='csv')
    except Exception:
        return render(request, 'inventory/product_import.html',
                      {'branch': branch, 'headers': _IMPORT_HEADERS,
                       'error': 'تعذّر قراءة الملف. المدعوم: Excel (.xlsx/.xls)، CSV، TSV، ODS، JSON. '
                                'لو الملف PDF أو صورة، ابعتهولي وأنا أحوّله لك.'})

    # 🧠 نظّم الأعمدة تلقائياً مهما كانت مسمّياتها أو ترتيبها
    colmap = _resolve_columns(ds.headers or [])
    if 'part_number' not in colmap and 'name' not in colmap:
        return render(request, 'inventory/product_import.html',
                      {'branch': branch, 'headers': _IMPORT_HEADERS,
                       'error': 'مقدرتش أتعرّف على أعمدة الملف. استخدم القالب، أو خلّي فيه عمود '
                                'لرقم القطعة/الكود وعمود للاسم.'})

    def cell(row, key):
        idx = colmap.get(key)
        if idx is None:
            return ''
        try:
            return row[idx]
        except Exception:
            return ''

    def to_dec(v):
        """رقم موجب أو None لو الخانة فاضية/مش رقم (عشان مانصفّرش أسعار مش في الملف)."""
        d = _finite_dec(v)
        return d if d is not None and d >= 0 else None

    def to_int(v):
        try:
            return int(float(str(v).strip() or '0'))
        except Exception:
            return 0

    created = updated = stock_set = 0
    errors = []
    from tenancy.services.plan_limits import inventory_items_block

    # 🛡️ أثناء الاستيراد بالجملة نوقف مزامنة B2B ومزامنة الموقع لكل صنف —
    # كانت بتطلق آلاف مهام Celery/طلبات HTTP وتغرق Redis وتوقّع السيرفر.
    # (المخزون بيتحدّث عادي؛ المزامنة للستورفرونت مش حرجة وقت الاستيراد.)
    from django.db.models.signals import post_save as _post_save
    from inventory import signals as _inv_signals
    _muted = [_inv_signals.sync_to_global_b2b_marketplace,
              _inv_signals.sync_stock_to_fixit_website]
    for _fn in _muted:
        _post_save.disconnect(_fn, sender=Inventory)

    try:
      with transaction.atomic():
        import hashlib
        for i, row in enumerate(ds, start=2):  # صف 1 = العناوين
            sku = str(cell(row, 'part_number')).strip()
            barcode = str(cell(row, 'barcode')).strip()
            name = str(cell(row, 'name')).strip()
            if not sku and not name and not barcode:
                continue  # صف فاضي
            if not name:
                name = sku or barcode
            # 🐛 [FIX]: أي عمود مش في الملف (أو خانته فاضية) كان بيتكتب صفر/«BMW» فوق
            #    بيانات الصنف — دلوقتي بنكتب بس اللي موجود فعلاً.
            defaults = {'name': name}
            for key in ('brand', 'car_model'):
                val = str(cell(row, key)).strip()
                if val:
                    defaults[key] = val
            for key in ('purchase_price', 'retail_price'):
                val = to_dec(cell(row, key))
                if val is not None:
                    defaults[key] = val
            msl = cell(row, 'min_stock_level')
            if str(msl).strip():
                defaults['min_stock_level'] = to_int(msl)
            try:
              with transaction.atomic():   # savepoint: صف بايظ مايبوّظش باقي الملف
                # 🎯 التعرّف على الصنف: الباركود أولاً (مميّز)، وإلا الكود+الاسم.
                # ده بيخلّي أصناف مختلفة بنفس الكود القديم تنزل كل واحد لوحده
                # بدل ما يتوّحدوا (سبب إن بعض الأصناف مكانتش بتنزل).
                prod = None
                if barcode:
                    prod = Product.objects.filter(barcode=barcode).first()
                if prod is None and sku:
                    prod = Product.objects.filter(part_number=sku, name=name).first()
                if prod is None and not sku and not barcode:
                    sku = "AUTO-" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10].upper()
                    prod = Product.objects.filter(part_number=sku).first()

                if prod is not None:
                    for k, v in defaults.items():
                        setattr(prod, k, v)
                    if barcode:
                        prod.barcode = barcode
                    prod.save()
                    updated += 1
                else:
                    # 📏 حد الأصناف في الباقة — الصنف الجديد بعد الحد مايتضافش (التحديثات تكمل)
                    blocked = inventory_items_block()
                    if blocked:
                        errors.append(f"صف {i} ({sku or barcode}): {blocked}")
                        continue
                    # صنف جديد — نضمن part_number مميّز (نزوّد لاحقة لو الكود متكرر)
                    base = (sku or (("BC-" + barcode) if barcode else "AUTO"))[:90]
                    pn = base
                    _k = 2
                    while Product.objects.filter(part_number=pn).exists():
                        pn = f"{base}-{_k}"
                        _k += 1
                    new_fields = {'brand': 'BMW', 'car_model': '—', **defaults}
                    # متوسط التكلفة = سعر الشراء (كان بيفضل صفر فأول شراء بعده يقسم التكلفة)
                    new_fields['average_cost'] = defaults.get('purchase_price') or Decimal('0')
                    prod = Product.objects.create(
                        part_number=pn, barcode=(barcode or None), **new_fields)
                    created += 1

                # كمية الفرع النشط — الفرق بيتسجّل حركة + قيد ومتوسط التكلفة بيتحسب صح
                qraw = cell(row, 'quantity')
                if str(qraw).strip() != '':
                    from inventory.services.inventory_service import set_branch_stock
                    set_branch_stock(prod, branch, to_int(qraw),
                                     unit_cost=defaults.get('purchase_price'), user=request.user,
                                     reference_type='ProductImport', note='رفع مخزون من ملف',
                                     opening=True)
                    stock_set += 1
            except Exception as exc:
                errors.append(f"صف {i} ({sku or barcode}): {exc}")
    finally:
        for _fn in _muted:
            _post_save.connect(_fn, sender=Inventory)

    return render(request, 'inventory/product_import.html', {
        'branch': branch,
        'headers': _IMPORT_HEADERS,
        'result': {'created': created, 'updated': updated,
                   'stock_set': stock_set, 'errors': errors[:20],
                   'error_count': len(errors)},
    })
