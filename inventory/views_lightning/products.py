"""📦 الأصناف: إضافة سريعة وبالجملة، المعرض، التعديل، ورفع الصور بالجملة الذكي.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
import re as _re
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from inventory.models import Branch, Inventory, InventoryMovement, Product
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required
from ._common import _finite_dec, _product_price_visibility


# =====================================================================
# 2. QUICK PRODUCT ENTRY
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
def quick_product_entry(request):
    branch = _get_branch_for_user(request.user)
    branches = Branch.objects.all() if branch is None else None
    return render(request, "inventory/quick_product.html", {
        "branch": branch,
        "branches": branches,
        "category_choices": Product.PART_CATEGORY_CHOICES,
        "condition_choices": Product.CONDITION_CHOICES,
    })


@login_required(login_url='/login/')
@tenant_required
@require_POST
def quick_product_create(request):
    """
    Create a Product + seed Inventory for the user's branch in one shot.
    Required: part_number, name, retail_price.
    Optional: brand, purchase_price, car_model, starting_qty.
    """
    sku = (request.POST.get("part_number") or "").strip()
    name = (request.POST.get("name") or "").strip()
    if not sku or not name:
        return _json_response_safe({"error": "رقم القطعة والاسم مطلوبان."}, status=400)

    def _money(field, default="0"):
        try:
            return Decimal(str(request.POST.get(field) or default))
        except InvalidOperation:
            return Decimal(default)

    retail = _money("retail_price")
    cost = _money("purchase_price")
    try:
        starting_qty = int(request.POST.get("starting_qty") or 0)
    except (TypeError, ValueError):
        starting_qty = 0
    try:
        min_stock_level = max(int(request.POST.get("min_stock_level") or 2), 0)
    except (TypeError, ValueError):
        min_stock_level = 2
    if starting_qty < 0:
        return _json_response_safe({"error": "كمية البداية لا يمكن أن تكون سالبة."}, status=400)

    branch = _get_branch_for_user(request.user)
    if branch is None:
        bid = request.POST.get("branch_id")
        branch = Branch.objects.filter(id=bid).first() if bid else None
    if branch is None and starting_qty > 0:
        return _json_response_safe({"error": "حدد الفرع لتسجيل كمية البداية."}, status=400)
    if branch is not None and not _user_can_edit_branch(request.user, branch):
        return _json_response_safe({"error": "👁 صلاحيتك في الفرع ده عرض فقط — مش مسموح بالإضافة."}, status=403)

    dup = Product.objects.filter(part_number=sku).first()
    if dup is not None:
        if not dup.is_active:
            return _json_response_safe({"error": (
                f"رقم القطعة '{sku}' لصنف متأرشف — رجّعه من المخزون ← فلتر «المؤرشفة» ← استرجاع.")},
                status=409)
        return _json_response_safe({"error": f"رقم القطعة '{sku}' موجود مسبقاً."}, status=409)

    # 🏷️ الباركود (اختياري، فريد) — نتأكد إنه مش مستخدم في قطعة تانية
    barcode = (request.POST.get("barcode") or "").strip() or None
    if barcode and Product.objects.filter(barcode=barcode).exists():
        return _json_response_safe({"error": f"الباركود '{barcode}' مستخدم في قطعة تانية."}, status=409)

    try:
        with transaction.atomic():
            # 🏷️ تصنيف القطعة — نقبل فقط القيم المعرّفة في الموديل ونتجاهل أي غيرها
            valid_categories = {c[0] for c in Product.PART_CATEGORY_CHOICES}
            part_category = (request.POST.get("part_category") or "").strip()
            if part_category not in valid_categories:
                part_category = ""

            # 🏷️ حالة القطعة (جديد/استيراد/تالف) — نقبل القيم المعرّفة بس
            valid_conditions = {c[0] for c in Product.CONDITION_CHOICES}
            condition = (request.POST.get("condition") or "new").strip()
            if condition not in valid_conditions:
                condition = "new"

            # 🔢 بارت نمبرات إضافية: سطر/فاصلة لكل رقم, تنضيف + إزالة التكرار والأساسي
            raw_pns = request.POST.get("additional_part_numbers") or ""
            extra_pns = []
            for token in raw_pns.replace(",", "\n").replace("،", "\n").splitlines():
                token = token.strip()
                if token and token != sku and token not in extra_pns:
                    extra_pns.append(token)

            product = Product.objects.create(
                part_number=sku,
                name=name,
                brand=(request.POST.get("brand") or "BMW").strip(),
                condition=condition,
                part_category=part_category,
                additional_part_numbers=extra_pns,
                description=(request.POST.get("description") or "").strip(),
                car_model=(request.POST.get("car_model") or "").strip() or "—",
                car_year=(request.POST.get("car_year") or "").strip() or "—",
                barcode=barcode,
                purchase_price=cost,
                retail_price=retail,
                b2b_wholesale_price=_money("b2b_wholesale_price"),
                damaged_price=_money("damaged_price"),
                scrap_price=_money("scrap_price"),
                average_cost=cost,
                min_stock_level=min_stock_level,
            )

            if starting_qty > 0 and branch is not None:
                inv, _ = Inventory.objects.get_or_create(
                    product=product, branch=branch,
                    defaults={"quantity": 0},
                )
                before = inv.quantity
                inv.quantity = before + starting_qty
                inv.save(update_fields=["quantity"])
                InventoryMovement.objects.create(
                    product=product,
                    branch=branch,
                    reason="adjustment",
                    quantity_change=starting_qty,
                    quantity_before=before,
                    quantity_after=inv.quantity,
                    reference_type="QuickProductEntry",
                    reference_id=product.id,
                    note="مخزون افتتاحي عند إنشاء القطعة",
                    created_by=request.user,
                )
                # 🏛️ رسملة المخزون الافتتاحي على الأصول (وإلا المخزون بينزل سالب عند البيع)
                try:
                    from inventory.services.accounting_service import AccountingService
                    AccountingService.post_opening_stock(
                        amount=Decimal(str(starting_qty)) * Decimal(str(cost or 0)),
                        description=f"مخزون افتتاحي — {product.name} ({product.part_number})",
                        reference=f"OPEN-PROD-{product.id}",
                        created_by=request.user, branch=branch,
                    )
                except Exception as _e:  # noqa: BLE001
                    import logging as _lg
                    _lg.getLogger('mouss_tec_core').error(
                        "[OPENING STOCK] GL post failed for product #%s: %s", product.id, _e)

            # 📸 صور القطعة المرفوعة — أول صورة تبقى الأساسية (Product.image)
            from inventory.models import ProductImage
            for idx, f in enumerate(request.FILES.getlist("images")):
                img = ProductImage.objects.create(
                    product=product, image=f, sort_order=idx + 1,
                    is_primary=(idx == 0),
                )
                if idx == 0:
                    product.image = img.image
                    product.save(update_fields=["image"])

        return _json_response_safe({
            "ok": True,
            "product_id": product.id,
            "sku": product.part_number,
            "stock": starting_qty,
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل إنشاء القطعة: {exc}"}, status=500)


# =====================================================================
# 📦 إضافة أصناف بالجملة — إدخال كذا صنف مرّة واحدة بدل صنف صنف
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
def bulk_product_entry(request):
    branch = _get_branch_for_user(request.user)
    return render(request, "inventory/bulk_product.html", {
        "branch": branch,
        "branches": Branch.objects.all().order_by("name") if branch is None else None,
    })


@login_required(login_url='/login/')
@tenant_required
@require_POST
def bulk_product_create(request):
    """💾 إنشاء عدّة أصناف + مخزونها الافتتاحي في عملية واحدة ذرّية.

    الحمولة: {branch_id, items:[{part_number,name,brand,car_model,car_year,
    purchase_price,retail_price,min_stock_level,starting_qty}]}
    كل الصفوف بتتحفظ سوا: لو صف غلط بترجع رسالة وما يتحفظش أي حاجة.
    """
    import json as _json
    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)

    rows = payload.get("items") or []
    if not rows:
        return _json_response_safe({"error": "أضف صنفاً واحداً على الأقل."}, status=400)

    branch = _get_branch_for_user(request.user)
    if branch is None:
        branch = Branch.objects.filter(id=payload.get("branch_id")).first()
    if branch is not None and not _user_can_edit_branch(request.user, branch):
        return _json_response_safe({"error": "👁 صلاحيتك في الفرع ده عرض فقط — مش مسموح بالإضافة."}, status=403)

    def _dec(v, default="0"):
        try:
            return Decimal(str(v if v not in (None, "") else default))
        except InvalidOperation:
            return Decimal(default)

    # تحقّق مبدئي + كشف التكرار (جوه الطلب أو في الداتا)
    cleaned = []
    seen_skus = set()
    for i, raw in enumerate(rows, start=1):
        sku = (raw.get("part_number") or "").strip()
        name = (raw.get("name") or "").strip()
        if not sku and not name:
            continue  # صف فاضي — نتجاهله
        if not sku or not name:
            return _json_response_safe({"error": f"الصف {i}: رقم القطعة والاسم مطلوبان."}, status=400)
        if sku in seen_skus:
            return _json_response_safe({"error": f"الصف {i}: رقم القطعة '{sku}' متكرر في القائمة."}, status=400)
        seen_skus.add(sku)
        try:
            qty = int(raw.get("starting_qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty < 0:
            return _json_response_safe({"error": f"الصف {i}: كمية البداية لا يمكن أن تكون سالبة."}, status=400)
        cleaned.append({
            "sku": sku, "name": name,
            "brand": (raw.get("brand") or "BMW").strip(),
            "car_model": (raw.get("car_model") or "").strip() or "—",
            "car_year": (raw.get("car_year") or "").strip() or "—",
            "cost": _dec(raw.get("purchase_price")),
            "retail": _dec(raw.get("retail_price")),
            "wholesale": _dec(raw.get("b2b_wholesale_price")),
            "damaged": _dec(raw.get("damaged_price")),
            "scrap": _dec(raw.get("scrap_price")),
            "min_stock": int(raw.get("min_stock_level") or 2) if str(raw.get("min_stock_level") or "").strip().isdigit() else 2,
            "qty": qty,
        })

    if not cleaned:
        return _json_response_safe({"error": "أضف صنفاً واحداً على الأقل."}, status=400)

    if any(r["qty"] > 0 for r in cleaned) and branch is None:
        return _json_response_safe({"error": "حدّد الفرع لتسجيل الكميات الافتتاحية."}, status=400)

    # منع الأكواد المكرّرة الموجودة أصلاً في قاعدة البيانات
    existing = set(
        Product.objects.filter(part_number__in=list(seen_skus))
        .values_list("part_number", flat=True)
    )
    if existing:
        return _json_response_safe(
            {"error": f"أرقام قطع موجودة مسبقاً: {'، '.join(sorted(existing))}"}, status=409)

    try:
        with transaction.atomic():
            created = 0
            for r in cleaned:
                product = Product.objects.create(
                    part_number=r["sku"], name=r["name"], brand=r["brand"],
                    car_model=r["car_model"], car_year=r["car_year"],
                    purchase_price=r["cost"], retail_price=r["retail"],
                    b2b_wholesale_price=r["wholesale"],
                    damaged_price=r["damaged"], scrap_price=r["scrap"],
                    average_cost=r["cost"], min_stock_level=r["min_stock"],
                )
                if r["qty"] > 0 and branch is not None:
                    inv, _ = Inventory.objects.get_or_create(
                        product=product, branch=branch, defaults={"quantity": 0})
                    before = inv.quantity
                    inv.quantity = before + r["qty"]
                    inv.save(update_fields=["quantity"])
                    InventoryMovement.objects.create(
                        product=product, branch=branch, reason="adjustment",
                        quantity_change=r["qty"], quantity_before=before,
                        quantity_after=inv.quantity,
                        reference_type="BulkProductEntry", reference_id=product.id,
                        note="مخزون افتتاحي — إدخال بالجملة", created_by=request.user,
                    )
                    # 🏛️ رسملة المخزون الافتتاحي (زي إضافة القطعة الواحدة) — كان ناقص
                    #    فحساب المخزون في الدفتر بينزل بالسالب أول ما القطع تتباع.
                    from inventory.services.accounting_service import AccountingService
                    AccountingService.post_opening_stock(
                        amount=Decimal(str(r["qty"])) * Decimal(str(r["cost"] or 0)),
                        description=f"مخزون افتتاحي — {product.name} ({product.part_number})",
                        reference=f"OPEN-PROD-{product.id}", created_by=request.user, branch=branch)
                created += 1
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ الأصناف: {exc}"}, status=500)

    return _json_response_safe({"ok": True, "created": created})


# =====================================================================
# 📸 معرض صور المنتج — رفع أكثر من صورة + تعيين الأساسية
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
def product_gallery(request, pk):
    product = Product.objects.filter(pk=pk).first()
    if product is None:
        return redirect(reverse('inventory:product_list') + '?err=notfound')
    branch = _get_branch_for_user(request.user)
    show_cost, _show_sale = _product_price_visibility(request, branch)
    return render(request, 'inventory/product_gallery.html', {
        'product': product,
        'images': product.images.all(),
        # 🐛 [FIX]: التكلفة كانت ظاهرة لأي موظف حتى اللي مش مسموح له يشوفها
        'show_cost': show_cost,
        'can_edit_prices': _user_can_edit_branch(request.user, branch),
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def product_prices_update(request, pk):
    """تحديث أسعار المنتج (بيع/جملة/هالك/خردة/تكلفة) من صفحة المنتج.

    🐛 [FIX]: كان مفيش أي فحص — «المراجع (عرض فقط)» وأي حد يقدر يغيّر الأسعار،
    والحقل اللي مش مبعوث كان بيتسجّل صفر (التكلفة بتتمسح لو الفورم مافيهاش).
    دلوقتي: صلاحية تعديل على الفرع، والحقل الغايب/الغلط بيفضل زي ما هو، والتكلفة
    مايغيّرهاش غير اللي مسموح له يشوفها.
    """
    product = Product.objects.filter(pk=pk).first()
    if product is None:
        return redirect(reverse('inventory:product_list') + '?err=notfound')
    branch = _get_branch_for_user(request.user)
    if not _user_can_edit_branch(request.user, branch):
        return redirect(reverse('inventory:product_gallery', args=[pk]) + '?err=perm')
    show_cost, _show_sale = _product_price_visibility(request, branch)

    def _money(field, current):
        raw = request.POST.get(field)
        if raw in (None, ""):
            return current
        v = _finite_dec(raw)
        return v if v is not None and v >= 0 else current
    if show_cost:
        product.purchase_price = _money("purchase_price", product.purchase_price)
    product.retail_price = _money("retail_price", product.retail_price)
    product.b2b_wholesale_price = _money("b2b_wholesale_price", product.b2b_wholesale_price)
    product.damaged_price = _money("damaged_price", product.damaged_price)
    product.scrap_price = _money("scrap_price", product.scrap_price)
    product.save(update_fields=["purchase_price", "retail_price",
                                "b2b_wholesale_price", "damaged_price", "scrap_price"])
    return redirect(reverse('inventory:product_gallery', args=[pk]) + '?ok=prices')


# =====================================================================
# ✏️ تعديل منتج داخل التطبيق — بديل لصفحة الـ Admin اللي بتفشل على الموبايل
# وبتقول "المنتج غير موجود". فورم واحد بيعدّل بيانات القطعة + رصيدها في الفرع.
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
def product_edit(request, pk):
    """صفحة تعديل قطعة كاملة (GET يعرض الفورم، POST يحفظ) — داخل بورتال الفرع.

    بتعدّل بيانات القطعة الأساسية + الأسعار + حد التنبيه، وكمان تقدر تظبط
    رصيد القطعة في الفرع النشط (بيتسجّل كحركة تسوية جرد في سجل المخزون).
    """
    product = Product.objects.filter(pk=pk).first()
    if product is None:
        return redirect(reverse('inventory:product_list') + '?err=notfound')

    branch = _get_branch_for_user(request.user)
    # سجل مخزون القطعة في الفرع النشط (لو المستخدم مركّز على فرع)
    inv = None
    if branch is not None:
        inv = Inventory.objects.filter(product=product, branch=branch).first()

    if request.method == 'POST':
        if branch is not None and not _user_can_edit_branch(request.user, branch):
            return redirect(reverse('inventory:product_edit', args=[pk]) + '?err=perm')

        def _money(field, keep=None):
            # 🛡️ لو الحقل فاضي/غير صالح، نحافظ على القيمة الحالية بدل ما نصفّرها —
            #    ده بيمنع "مسح" أسعار المنتج لو الحقل ظهر فاضي (مثلاً بسبب تنسيق
            #    الأرقام). بنشيل فواصل الآلاف كمان عشان "1,500" تتقرأ صح.
            fallback = keep if keep is not None else Decimal("0")
            raw = (request.POST.get(field) or "").strip().replace(",", "").replace("٬", "")
            if raw == "":
                return fallback
            try:
                v = Decimal(raw)
                return v if v >= 0 else fallback
            except InvalidOperation:
                return fallback

        sku = (request.POST.get("part_number") or "").strip()
        name = (request.POST.get("name") or "").strip()
        if not sku or not name:
            return redirect(reverse('inventory:product_edit', args=[pk]) + '?err=required')
        # رقم القطعة فريد — نتأكد إنه مش مستخدم في قطعة تانية
        if Product.objects.filter(part_number=sku).exclude(pk=product.pk).exists():
            return redirect(reverse('inventory:product_edit', args=[pk]) + '?err=dupsku')

        barcode = (request.POST.get("barcode") or "").strip() or None
        if barcode and Product.objects.filter(barcode=barcode).exclude(pk=product.pk).exists():
            return redirect(reverse('inventory:product_edit', args=[pk]) + '?err=dupbarcode')

        valid_categories = {c[0] for c in Product.PART_CATEGORY_CHOICES}
        part_category = (request.POST.get("part_category") or "").strip()
        if part_category not in valid_categories:
            part_category = ""
        valid_conditions = {c[0] for c in Product.CONDITION_CHOICES}
        condition = (request.POST.get("condition") or "new").strip()
        if condition not in valid_conditions:
            condition = "new"

        try:
            min_stock = int(request.POST.get("min_stock_level") or product.min_stock_level or 2)
        except (TypeError, ValueError):
            min_stock = product.min_stock_level or 2

        with transaction.atomic():
            product.part_number = sku
            product.name = name
            product.brand = (request.POST.get("brand") or "").strip() or "BMW"
            product.condition = condition
            product.part_category = part_category
            product.car_model = (request.POST.get("car_model") or "").strip() or "—"
            product.car_year = (request.POST.get("car_year") or "").strip() or "—"
            product.barcode = barcode
            product.description = (request.POST.get("description") or "").strip()
            # 🔢 بارت نمبرات إضافية: سطر/فاصلة لكل رقم، تنضيف + إزالة التكرار والأساسي
            raw_pns = request.POST.get("additional_part_numbers") or ""
            extra_pns = []
            for token in raw_pns.replace(",", "\n").replace("،", "\n").splitlines():
                token = token.strip()
                if token and token != sku and token not in extra_pns:
                    extra_pns.append(token)
            product.additional_part_numbers = extra_pns
            # التكلفة مايغيّرهاش غير اللي مسموح له يشوفها (الحقل مخفي عنه أصلاً)
            if _product_price_visibility(request, branch)[0]:
                product.purchase_price = _money("purchase_price", product.purchase_price)
            product.retail_price = _money("retail_price", product.retail_price)
            product.b2b_wholesale_price = _money("b2b_wholesale_price", product.b2b_wholesale_price)
            product.damaged_price = _money("damaged_price", product.damaged_price)
            product.scrap_price = _money("scrap_price", product.scrap_price)
            product.min_stock_level = max(min_stock, 0)
            wants_active = (request.POST.get("is_active") == "on")
            # 🛡️ قطعة ليها رصيد ماتتأرشفش — كانت بتختفي من قيمة المخزون والدفتر شايلها
            blocked_archive = (not wants_active and product.is_active
                               and (product.total_inventory_qty or 0) > 0)
            product.is_active = wants_active or blocked_archive
            product.save()

            # 📦 تعديل رصيد الفرع النشط — الفرق بيتسجّل حركة تسوية جرد + قيدها.
            # 🐛 [FIX]: الصفحة بتبعت الرصيد اللي كان ظاهر وقت فتحها؛ لو اتباع حاجة
            #    في النص وحفظت (عشان الاسم مثلاً) الرصيد كان بيرجع للرقم القديم.
            #    دلوقتي بنطبّق اللي غيّرته انت بس (الجديد − اللي كان ظاهر).
            if branch is not None and request.POST.get("quantity") not in (None, ""):
                try:
                    new_qty = int(request.POST.get("quantity"))
                    orig_qty = int(request.POST.get("orig_qty", new_qty))
                except (TypeError, ValueError):
                    new_qty = orig_qty = None
                if new_qty is not None and new_qty >= 0 and new_qty != orig_qty:
                    from inventory.services.inventory_service import set_branch_stock
                    current = (Inventory.objects.select_for_update()
                               .filter(product=product, branch=branch)
                               .values_list('quantity', flat=True).first()) or 0
                    set_branch_stock(product, branch, current + (new_qty - orig_qty),
                                     user=request.user, reference_type='ProductEdit',
                                     note='تعديل الرصيد من صفحة تعديل القطعة', opening=False)
        if blocked_archive:
            return redirect(reverse('inventory:product_edit', args=[pk]) + '?err=hasstock')
        return redirect(reverse('inventory:product_edit', args=[pk]) + '?ok=1')

    return render(request, "inventory/product_edit.html", {
        "product": product,
        "branch": branch,
        "inv": inv,
        "category_choices": Product.PART_CATEGORY_CHOICES,
        "condition_choices": Product.CONDITION_CHOICES,
        "can_edit": _user_can_edit_branch(request.user, branch) if branch is not None else True,
        "show_cost": _product_price_visibility(request, branch)[0],
        "flash": request.GET.get("ok"),
        "err": request.GET.get("err"),
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def product_gallery_upload(request, pk):
    """رفع صورة أو أكثر دفعة واحدة لمنتج. أول صورة تبقى الأساسية لو مفيش."""
    from inventory.models import ProductImage
    product = Product.objects.filter(pk=pk).first()
    if product is None:
        return redirect(reverse('inventory:product_list') + '?err=notfound')
    files = request.FILES.getlist('images')
    if not files:
        return redirect(reverse('inventory:product_gallery', args=[pk]) + '?err=nofiles')
    has_primary = product.images.filter(is_primary=True).exists() or bool(product.image)
    last_order = (product.images.aggregate(m=Sum('sort_order'))['m'] or 0)
    for i, f in enumerate(files):
        img = ProductImage.objects.create(
            product=product, image=f, sort_order=last_order + i + 1,
            is_primary=(not has_primary and i == 0),
        )
        if not has_primary and i == 0:
            product.image = img.image
            product.save(update_fields=['image'])
            has_primary = True
    return redirect(reverse('inventory:product_gallery', args=[pk]) + '?ok=uploaded')


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def product_image_primary(request, pk, image_id):
    """تعيين صورة كأساسية — بتظهر في الـ POS والقوائم والطباعة."""
    from inventory.models import ProductImage
    product = Product.objects.filter(pk=pk).first()
    img = ProductImage.objects.filter(pk=image_id, product=product).first()
    if product is None or img is None:
        return redirect(reverse('inventory:product_list') + '?err=notfound')
    product.images.update(is_primary=False)
    img.is_primary = True
    img.save(update_fields=['is_primary'])
    product.image = img.image
    product.save(update_fields=['image'])
    return redirect(reverse('inventory:product_gallery', args=[pk]) + '?ok=primary')


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def product_image_delete(request, pk, image_id):
    from inventory.models import ProductImage
    product = Product.objects.filter(pk=pk).first()
    img = ProductImage.objects.filter(pk=image_id, product=product).first()
    if product is None or img is None:
        return redirect(reverse('inventory:product_list') + '?err=notfound')
    was_primary = img.is_primary
    img.delete()
    # لو المحذوفة كانت الأساسية، رقّي أول صورة باقية لتبقى الأساسية
    if was_primary:
        nxt = product.images.first()
        if nxt:
            nxt.is_primary = True
            nxt.save(update_fields=['is_primary'])
            product.image = nxt.image
        else:
            product.image = None
        product.save(update_fields=['image'])
    return redirect(reverse('inventory:product_gallery', args=[pk]) + '?ok=deleted')


def _match_product_by_code(code):
    """يطابق كود (رقم قطعة/باركود/OEM) بمنتج — **مطابقة دقيقة فقط** لتفادي
    الأخطاء (رقم على الاستيكر زي تاريخ/باركود كان يتطابق بالصدفة بجزء من SKU
    قطعة تانية).

    بيجرّب صيغ مُطبّعة من الكود ويطابقها **بالكامل** (iexact) على
    part_number/barcode/بارت نمبر إضافي:
      • النص زي ما هو، ونسخة مضغوطة (بدون مسافات/شرط/نقط)
      • قاعدة بعد إزالة لاحقة تسلسل/فهرس (‎-01‎ / ‎_2‎)
      • أرقام فقط للكود وللقاعدة (بيشيل بادئة زي AV ولاحقة ‎-01‎ فيطلع
        رقم البارت النضيف زي 9187798)
    بنتجاهل الصيغ الأقصر من ٥ خانات (تواريخ/فهارس) عشان ما تطابقش بالغلط.
    مفيش مطابقة بالتشابه الجزئي (substring) — دقة أهم من التقاط أكتر.
    """
    code = (code or '').strip()
    if not code:
        return None
    compact = _re.sub(r'[\s\-_.]+', '', code)
    base = _re.sub(r'[ _\-]+\d+$', '', code)        # شيل ‎-01‎ / ‎_2‎ من الآخر
    digits_code = _re.sub(r'\D', '', code)          # أرقام الكود كلها
    digits_base = _re.sub(r'\D', '', base)          # أرقام القاعدة (رقم البارت النضيف)
    forms = []
    for c in (code, compact, base, digits_base, digits_code):
        c = (c or '').strip()
        if len(c) >= 5 and c not in forms:          # نتجاهل الأجزاء القصيرة
            forms.append(c)
    for c in forms:
        p = (Product.objects.filter(part_number__iexact=c).first()
             or Product.objects.filter(barcode__iexact=c).first()
             or Product.objects.filter(additional_part_numbers__contains=c).first())
        if p:
            return p
    return None


def _match_product_by_filename(stem):
    """يطابق اسم ملف صورة بمنتج (رقم القطعة/الباركود/بارت نمبر إضافي)."""
    return _match_product_by_code(stem)


def _downscale_to_jpeg_b64(raw_bytes, max_dim=1400, quality=82):
    """يصغّر الصورة ويحوّلها JPEG base64 قبل إرسالها للـ AI (أسرع وأخف).

    بيرجّع None لو الصورة مش مقروءة (مثلاً HEIC من غير دعم) — ساعتها بنتجاهل
    OCR للصورة دي بأمان بدل ما نكسر الرفع كله.
    """
    try:
        import io as _io
        import base64 as _b64
        from PIL import Image as _Image
        im = _Image.open(_io.BytesIO(raw_bytes))
        im = im.convert('RGB')
        w, h = im.size
        scale = min(1.0, float(max_dim) / float(max(w, h) or 1))
        if scale < 1.0:
            im = im.resize((max(int(w * scale), 1), max(int(h * scale), 1)))
        buf = _io.BytesIO()
        im.save(buf, format='JPEG', quality=quality)
        return _b64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return None


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
def product_bulk_images(request):
    """📸🗂️ رفع صور بالجملة: كل صورة اسمها = رقم القطعة (أو الباركود)، والسيستم
    يطابقها ويحطها للمنتج تلقائياً. أول صورة للقطعة تبقى الأساسية لو مفيش،
    وبيدعم أكتر من صورة للقطعة بلاحقة تسلسل (BP-123_1 / BP-123_2)."""
    import os
    from inventory.models import ProductImage

    if request.method == 'POST':
        from django.conf import settings as _st
        files = request.FILES.getlist('images')
        # 🤖 OCR شغّال بس لو الذكاء الاصطناعي مفعّل ومفتاح رؤية موجود (مفتاح
        #    الرؤية أو مفتاح Gemini العام — نفس اللي بيشغّل المستشار الذكي).
        ai_on = (bool(getattr(_st, 'ENABLE_AI_PREDICTIONS', False))
                 and bool(str(getattr(_st, 'AI_VISION_API_KEY', '') or '').strip()
                          or str(getattr(_st, 'GEMINI_API_KEY', '') or '').strip()))
        matched, unmatched, ai_matched_count = [], [], 0
        for f in files:
            stem = os.path.splitext(os.path.basename(f.name))[0]
            product = _match_product_by_filename(stem)
            via = 'filename'
            read_codes = []  # الأرقام اللي قراها الـ OCR من جوه الصورة (للتشخيص)

            # 📸 لو اسم الملف ملوش قطعة، نقرا الرقم اللي **جوه الصورة** (OCR) ونطابق بيه.
            if product is None and ai_on:
                try:
                    raw = f.read()
                    b64 = _downscale_to_jpeg_b64(raw)
                    if b64:
                        from inventory.ai_services import read_part_codes_from_image_ai
                        read_codes = read_part_codes_from_image_ai(b64).get('codes', [])
                        for code in read_codes:
                            product = _match_product_by_code(code)
                            if product is not None:
                                via = 'ai'
                                break
                except Exception:  # noqa: BLE001 — OCR فشل لصورة ما يوقّفش الباقي
                    product = product
                finally:
                    f.seek(0)  # نرجّع المؤشّر لأول الملف عشان الحفظ يبقى سليم

            if product is None:
                # بنعرض الأرقام اللي اتقرت عشان المستخدم يشوف: قرأنا إيه ومفيش قطعة بيها،
                # ولا مقريناش رقم أصلاً. أول ٦ أكواد تكفي للعرض.
                unmatched.append({'name': f.name, 'codes': read_codes[:6]})
                continue

            f.seek(0)
            has_primary = product.images.filter(is_primary=True).exists() or bool(product.image)
            last_order = product.images.aggregate(m=Sum('sort_order'))['m'] or 0
            img = ProductImage.objects.create(
                product=product, image=f, sort_order=last_order + 1,
                is_primary=not has_primary,
            )
            if not has_primary:
                product.image = img.image
                product.save(update_fields=['image'])
            if via == 'ai':
                ai_matched_count += 1
            matched.append({'name': f.name, 'product': product, 'via': via})
        return render(request, 'inventory/bulk_images.html', {
            'done': True, 'matched': matched, 'unmatched': unmatched,
            'matched_count': len(matched), 'unmatched_count': len(unmatched),
            'ai_matched_count': ai_matched_count, 'ai_on': ai_on,
        })
    return render(request, 'inventory/bulk_images.html', {})


# =====================================================================
# 📸🤖 رفع صور بالجملة الذكية — مطابقة بالترتيب + مراجعة الصورة بالـAI
# =====================================================================
# فكرة الشاشة: مش لازم تسمّي كل صورة برقم قطعتها. رتّب الصور بأرقام بسيطة
# (١،٢،٣… بنفس ترتيب الإكسل) والنظام يطابقها بالترتيب على القطع، والـAI
# يراجع كل صورة ويقرا البارت نمبر منها ويحذّرك لو صورة في مكان غلط، وكمان
# يقترح اسم للقطعة لو ناقص — وكله بمعاينة قبل الحفظ.
_NAME_PLACEHOLDERS = {
    '', '-', '—', '--', '...', '.', '؟', '?', 'بدون اسم', 'بدون', 'غير معروف',
    'unknown', 'n/a', 'na', 'tbd',
}


def _product_needs_name(product):
    """القطعة محتاجة اسم لو الاسم فاضي/placeholder أو نفس رقم البارت."""
    name = (product.name or '').strip()
    if name.lower() in _NAME_PLACEHOLDERS:
        return True
    return name == (product.part_number or '').strip()


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
def bulk_images_smart(request):
    """📸🤖 صفحة رفع الصور بالترتيب + المراجعة الذكية (راجع bulk_images_smart.html)."""
    from inventory.ai_services import _vision_unavailable
    return render(request, 'inventory/bulk_images_smart.html', {
        'ai_available': not _vision_unavailable(),
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_GET
def bulk_images_smart_targets(request):
    """قائمة القطع المستهدفة بالترتيب (الأقدم أولاً = ترتيب الاستيراد) عشان
    نطابق الصور عليها بالترتيب. scope=no_image (الافتراضي) → القطع اللي لسه
    من غير صورة؛ scope=all → كل القطع النشطة."""
    scope = (request.GET.get('scope') or 'no_image').strip()
    qs = Product.objects.filter(is_active=True).order_by('id')
    if scope != 'all':
        qs = (qs.filter(Q(image='') | Q(image__isnull=True))
                .filter(images__isnull=True))
    qs = qs.distinct()
    items = [{
        'id': p.id,
        'name': (p.name or '').strip(),
        'part_number': p.part_number or '',
        'brand': p.brand or '',
        'needs_name': _product_needs_name(p),
    } for p in qs[:2000]]
    return _json_response_safe({'items': items, 'count': len(items)})


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def bulk_images_smart_review_one(request):
    """يراجع صورة واحدة بالـAI: يقرا رقم البارت منها، يقول تطابق القطعة المخصّصة
    ولا لأ، ويقترح اسم لو ناقص. بيتنده مرة لكل صورة عشان مايعدّيش timeout السيرفر."""
    import base64
    from inventory.ai_services import read_part_from_image
    f = request.FILES.get('image')
    if f is None:
        return _json_response_safe({'error': 'مافيش صورة مرفقة'}, status=400)
    expected_pn = (request.POST.get('expected_part_number') or '').strip()
    try:
        b64 = base64.b64encode(f.read()).decode('ascii')
    except Exception:
        return _json_response_safe({'error': 'تعذّر قراءة الصورة'}, status=400)
    res = read_part_from_image(b64, expected_part_number=expected_pn)
    if not res.get('available'):
        return _json_response_safe({'ok': True, 'available': False})
    # لو الـAI قرا رقم بيطابق قطعة تانية، نرجّعها عشان نقترح «انقل الصورة لها»
    ai_product = None
    seen = []
    for cand in [res.get('best_part_number')] + list(res.get('visible_part_numbers') or []):
        cand = (cand or '').strip()
        if not cand or cand in seen:
            continue
        seen.append(cand)
        p = _match_product_by_filename(cand)
        if p:
            ai_product = {'id': p.id, 'name': p.name, 'part_number': p.part_number}
            break
    return _json_response_safe({
        'ok': True,
        'available': True,
        'read_part_numbers': res.get('visible_part_numbers') or [],
        'best_part_number': res.get('best_part_number') or '',
        'matches_expected': res.get('matches_expected'),
        'suggested_name': (res.get('suggested_name') or '').strip(),
        'confidence': res.get('confidence') or 0,
        'ai_product': ai_product,
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def bulk_images_smart_save(request):
    """يحفظ الصور على القطع حسب الخريطة المؤكّدة من المستخدم (لكل ملف: رقم القطعة
    أو product_id)، ويملأ الأسماء الناقصة اللي وافق عليها. الملفات بتترفع تاني
    هنا مع الخريطة (مافيش تخزين مؤقّت على السيرفر)."""
    import json as _json
    from inventory.models import ProductImage
    try:
        mapping = _json.loads(request.POST.get('mapping') or '[]')
    except ValueError:
        return _json_response_safe({'error': 'خريطة المطابقة غير صالحة'}, status=400)
    by_name = {}
    for m in mapping:
        fn = (m.get('filename') or '').strip()
        if fn:
            by_name[fn] = m
    files = request.FILES.getlist('images')
    attached, skipped, named = 0, 0, 0
    with transaction.atomic():
        for f in files:
            m = by_name.get(f.name)
            if not m:
                skipped += 1
                continue
            # رقم القطعة (الحقل القابل للتعديل) أولاً، وإلا الـproduct_id المخصّص
            pn = (m.get('part_number') or '').strip()
            product = _match_product_by_filename(pn) if pn else None
            if product is None and m.get('product_id'):
                product = Product.objects.filter(pk=m.get('product_id')).first()
            if product is None:
                skipped += 1
                continue
            has_primary = product.images.filter(is_primary=True).exists() or bool(product.image)
            last_order = product.images.aggregate(mx=Sum('sort_order'))['mx'] or 0
            img = ProductImage.objects.create(
                product=product, image=f, sort_order=last_order + 1,
                is_primary=not has_primary,
            )
            if not has_primary:
                product.image = img.image
                product.save(update_fields=['image'])
            new_name = (m.get('set_name') or '').strip()
            if new_name and _product_needs_name(product):
                product.name = new_name[:200]
                product.save(update_fields=['name'])
                named += 1
            attached += 1
    return _json_response_safe({
        'ok': True, 'attached': attached, 'skipped': skipped, 'named': named,
    })
