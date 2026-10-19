"""⚡ نقطة البيع السريعة (/system/lightning-pos/) — بحث الأصناف واعتماد الفاتورة.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from inventory.models import (
    Branch,
    Customer,
    Inventory,
    InventoryMovement,
    Product,
    SaleInvoice,
    SaleInvoiceItem,
    Treasury,
)
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required
from ._common import (
    _RollbackWith,
    _WALK_IN_CREDIT_ERROR,
    _apply_product_search,
    _can_edit_invoices,
    _finite_dec,
    _is_walk_in,
    _named_walk_in,
    _normalize_tenders,
    _post_sale_to_ledger,
    _record_invoice_payments,
    _resolve_customer,
)


# =====================================================================
# 1. LIGHTNING POS
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@module_required('pos')
def lightning_pos(request):
    branch = _get_branch_for_user(request.user)
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    return render(request, "inventory/lightning_pos.html", {
        "branch": branch,
        "branches": Branch.objects.all() if branch is None else None,
        "treasuries": treasury_qs,
    })


@login_required(login_url='/login/')
@tenant_required
@require_GET
def product_quick_search(request):
    """
    Suggest products for the POS search bar.
    Matches SKU (part_number), barcode (exact), or name (icontains).
    Returns at most 12 rows with live stock for the user's branch.
    """
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return _json_response_safe({"results": []})

    branch = _get_branch_for_user(request.user)
    base = Product.objects.filter(is_active=True)
    # 🛒 scope=all: شاشة فاتورة الشراء. الأدمن (شايف كل الفروع) بيبعت الفرع
    # اللي بيشتري له في branch_id — فالنتايج والرصيد يبقوا للفرع ده بس، مش
    # مخزون الفروع التانية. الصنف اللي مش موجود في الفرع بيتضاف كـ«صنف جديد»
    # (والسيرفر بيربطه بنفس صنف الكتالوج لو الاسم/الكود متطابق).
    scope_all = (request.GET.get("scope") or "").strip() == "all"
    # 🏬 الأدمن (شايف كل الفروع) بيبعت الفرع المختار في الشاشة (بيع أو شراء)
    #    عشان الرصيد اللي يظهر يبقى رصيد الفرع ده — مش مجموع كل الفروع.
    if branch is None:
        bid = (request.GET.get("branch_id") or "").strip()
        if bid.isdigit():
            branch = Branch.objects.filter(id=int(bid)).first()
    # 🏬 عزل الفروع التام: لو المستخدم مركّز على فرع، ابحث بس في منتجات الفرع ده
    # (اللي ليها سجل مخزون فيه) — مش منتجات الفروع التانية. ده كمان بيمنع إن
    # نتايج فروع تانية تزحم أول ١٢ نتيجة فيختفي منتج فرعك (سبب «مش بيلاقي»).
    if branch is not None:
        from django.db.models import Exists, OuterRef
        base = base.filter(Exists(
            Inventory.objects.filter(product=OuterRef("pk"), branch=branch)))
    # بحث عربي مُطبَّع (يلاقي الاسم مهما اختلفت صيغة الحروف) + الكود/الباركود
    qs = _apply_product_search(base, q).distinct()[:12]

    results = []
    # 🛒 شاشة الشراء بتحتاج آخر سعر شراء عشان تملأه بدل صفر — بس للي بيشتروا
    #    (الكاشير في الـ POS مايشوفش التكلفة).
    show_cost = scope_all and _can_edit_invoices(request.user)
    for p in qs:
        stock_qs = p.inventory_set.all()
        if branch is not None:
            stock_qs = stock_qs.filter(branch=branch)
        stock = stock_qs.aggregate(s=Sum("quantity"))["s"] or 0
        row = {
            "id": p.id,
            "sku": p.part_number,
            "name": p.name,
            "brand": p.brand,
            "price": float(p.retail_price or 0),
            "stock": stock,
        }
        if show_cost:
            row["cost"] = float(p.purchase_price or p.average_cost or 0)
        results.append(row)
    return _json_response_safe({"results": results})


@login_required(login_url='/login/')
@tenant_required
@require_POST
def lightning_pos_checkout(request):
    """
    Atomic POS checkout:
      - lock Inventory rows (select_for_update)
      - validate stock per line
      - create SaleInvoice (status=posted, invoice_type=sale)
      - decrement Inventory + log InventoryMovement(reason='sale')
      - recompute totals via SaleInvoice.update_total()
    """
    import json as _json
    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات JSON غير صالحة."}, status=400)

    items = payload.get("items") or []
    if not items:
        return _json_response_safe({"error": "السلة فارغة."}, status=400)

    branch = _get_branch_for_user(request.user)
    if branch is None:
        # superuser must pick a branch from the dropdown
        bid = payload.get("branch_id")
        branch = Branch.objects.filter(id=bid).first() if bid else None
    if branch is None:
        return _json_response_safe({"error": "لم يتم تحديد الفرع."}, status=400)
    if not _user_can_edit_branch(request.user, branch):
        return _json_response_safe({"error": "👁 صلاحيتك في الفرع ده عرض فقط — مش مسموح بالبيع."}, status=403)

    try:
        with transaction.atomic():
            # 🧾 اربط الفاتورة بعميل موجود لو الكاشير اختاره من قائمة البحث
            # (customer_id) — ده بيخلّي تاريخ ومديونية وتقارير العميل مظبوطة.
            # لو مفيش اختيار، ارجع لسلوك «ابحث-أو-أنشئ» بالاسم/الهاتف زي الأول.
            cust_id = payload.get("customer_id")
            customer = None
            if cust_id:
                customer = Customer.objects.filter(id=cust_id).first()
            walk_in_note = None
            if customer is None:
                customer = _resolve_customer(payload.get("customer_name"), payload.get("customer_phone"))
                customer, walk_in_note = _named_walk_in(customer, payload.get("customer_name"))

            # Pre-lock + validate
            line_specs = []
            # 🐛 [FIX]: نفس الصنف على سطرين كان كل سطر بيقرا رصيده لوحده ويكتب
            #    «قبل − الكمية» ⇒ السطر التاني بيمسح خصم الأول (10 − 3 − 3 = 7)
            #    والتحقّق بيعدّي على كل سطر لوحده. دلوقتي صف واحد للصنف وتحقّق بالمجموع.
            stock_rows, wanted = {}, {}
            for raw in items:
                try:
                    pid = int(raw.get("product_id"))
                    qty = int(raw.get("qty") or 0)
                except (TypeError, ValueError):
                    raise _RollbackWith(_json_response_safe({"error": "صنف أو كمية غير صالحة."}, status=400))
                if qty <= 0:
                    raise _RollbackWith(_json_response_safe({"error": "كمية غير صالحة."}, status=400))
                price = _finite_dec(raw.get("price"))
                if price is None:
                    raise _RollbackWith(_json_response_safe({"error": "سعر غير صالح."}, status=400))
                if price < 0:
                    raise _RollbackWith(_json_response_safe({"error": "سعر البيع مينفعش يبقى بالسالب."}, status=400))
                # خصم الصنف (اختياري) — مبلغ على السطر كله
                try:
                    line_disc = Decimal(str(raw.get("discount") or "0"))
                except (InvalidOperation, TypeError):
                    line_disc = Decimal("0")
                if line_disc < 0:
                    line_disc = Decimal("0")
                line_gross = Decimal(str(qty)) * price
                if line_disc > line_gross:
                    line_disc = line_gross  # الخصم لا يتجاوز قيمة السطر

                inv = stock_rows.get(pid)
                if inv is None:
                    inv = (Inventory.objects
                           .select_for_update()
                           .filter(product_id=pid, branch=branch)
                           .first())
                    if inv is not None:
                        stock_rows[pid] = inv
                wanted[pid] = wanted.get(pid, 0) + qty
                if inv is None or inv.quantity < wanted[pid]:
                    available = inv.quantity if inv else 0
                    raise _RollbackWith(_json_response_safe({
                        "error": f"المخزون غير كافٍ للقطعة #{pid} (متاح: {available}, مطلوب: {wanted[pid]})."
                    }, status=409))
                line_specs.append((inv, pid, qty, price, line_disc))

            try:
                discount = Decimal(str(payload.get("discount") or "0"))
            except InvalidOperation:
                discount = Decimal("0")

            # 🔒 حد الخصم حسب صلاحية الموظف (المدير/الأدمن بلا حد) — الإجمالي +
            #    خصومات الأصناف تُحتسب معاً مقابل حد الموظف.
            subtotal = sum((Decimal(str(q)) * Decimal(str(p)) for _, _, q, p, _ in line_specs), Decimal("0"))
            line_disc_total = sum((d for _, _, _, _, d in line_specs), Decimal("0"))
            # 🐛 [FIX]: خصم الفاتورة بالسالب كان بيزوّد الإجمالي، وخصم أكبر من
            #    الفاتورة كان بيطلّع فاتورة بالسالب. نحصره بين صفر وصافي الأصناف.
            discount = min(max(discount, Decimal("0")), subtotal - line_disc_total)
            # 🐛 [FIX]: الكاشير كان يقدر يتخطّى حد الخصم بتاعه بإنه يكتب سعر أقل
            #    من سعر البيع بدل ما يكتب خصم. نزول السعر عن سعر البيع المسجّل
            #    بيتحسب خصم برضه في فحص الحد.
            price_cut = Decimal("0")
            base_total = Decimal("0")
            for inv_row, _pid, q, p, _d in line_specs:
                retail = Decimal(str(inv_row.product.retail_price or 0))
                price = Decimal(str(p))
                price_cut += Decimal(str(q)) * max(retail - price, Decimal("0"))
                base_total += Decimal(str(q)) * max(retail, price)
            # إجمالي الخصم = خصم الفاتورة + خصومات الأصناف + تخفيض السعر (لفحص حد صلاحية الموظف)
            effective_discount = discount + line_disc_total + price_cut
            if effective_discount > 0 and base_total > 0 and not request.user.is_superuser:
                profile = getattr(request.user, "employee_profile", None)
                if profile:
                    disc_pct = (effective_discount / base_total) * Decimal("100")
                    if not profile.can_apply_discount(disc_pct):
                        raise _RollbackWith(_json_response_safe({
                            "error": (f"الخصم ({disc_pct:.1f}%) يتجاوز الحد المسموح لك "
                                      f"({profile.effective_max_discount:.0f}%). راجع المدير.")
                        }, status=403))

            invoice = SaleInvoice.objects.create(
                invoice_type="sale",
                status="posted",
                customer=customer,
                branch=branch,
                discount=discount,
                paid_amount=Decimal("0.00"),
                notes=walk_in_note,
            )

            for inv, pid, qty, price, line_disc in line_specs:
                product = inv.product
                SaleInvoiceItem.objects.create(
                    invoice=invoice,
                    product=product,
                    quantity=qty,
                    unit_price=price,
                    discount=line_disc,
                    cost_at_sale=product.average_cost or Decimal("0.00"),
                )
                before = inv.quantity
                inv.quantity = before - qty
                inv.save(update_fields=["quantity"])
                InventoryMovement.objects.create(
                    product=product,
                    branch=branch,
                    reason="sale",
                    quantity_change=-qty,
                    quantity_before=before,
                    quantity_after=inv.quantity,
                    reference_type="SaleInvoice",
                    reference_id=invoice.id,
                    note=f"فاتورة بيع #{invoice.id}",
                    created_by=request.user,
                )

            invoice.update_total()
            # 📒 قيّد الإيراد/التكلفة/المديونية في دفتر الأستاذ (بعد ضبط الأصناف)
            _post_sale_to_ledger(invoice, request.user)
            # Payment — دفعة واحدة أو مقسّمة (جزء كاش + جزء انستا). أي جزء غير
            # مدفوع بيفضل آجل على العميل (الرصيد بيتزوّد تحت).
            tenders = _normalize_tenders(payload.get("treasury_id"),
                                         payload.get("paid_amount"),
                                         payload.get("payments"))
            if tenders:
                _record_invoice_payments(invoice, tenders, request.user)
                invoice.refresh_from_db()

            # 🛡️ Receivable on the customer for any unpaid portion. The post_save
            # signal that normally does this (InvoiceService.execute_sale) fired
            # at SaleInvoice.create() time when the invoice had no items, so it
            # processed an empty invoice and set is_applied=True. We have to
            # post the receivable explicitly here so the customer's balance
            # reflects credit sales ("آجل") and partial payments.
            if not getattr(invoice, "is_return", False):
                due = invoice.due_amount
                if due > Decimal("0.00") and _is_walk_in(customer):
                    raise _RollbackWith(_json_response_safe({"error": _WALK_IN_CREDIT_ERROR}, status=400))
                if due > Decimal("0.00"):
                    from django.db.models import F as _F
                    Customer.objects.filter(pk=customer.pk).update(
                        balance=_F("balance") + due
                    )

        return _json_response_safe({
            "ok": True,
            "invoice_id": invoice.id,
            "total": float(invoice.total_amount),
            "paid": float(invoice.paid_amount),
            "due": float(invoice.due_amount),
            "print_url": reverse("inventory:print_invoice_thermal", args=[invoice.id]),
        })
    except _RollbackWith as rb:
        return rb.response
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل إتمام الفاتورة: {exc}"}, status=500)
