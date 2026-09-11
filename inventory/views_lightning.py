"""
Lightning POS + Quick Product Entry — competitor-grade fast workflows.

Two surfaces:
  1. /lightning-pos/  → walk-in retail invoicing (no vehicle, no maintenance fields)
  2. /quick-product/  → 6-field product creation + starting stock in one POST

Both write through the existing SaleInvoice / Product / Inventory / InventoryMovement
models — no schema changes, no new tables.
"""
import re as _re
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum, Value, F
from django.db.models.functions import Replace, Lower
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models import (
    Branch, Customer, EmployeeProfile, ExpenseCategory, FinancialTransaction,
    Inventory, InventoryMovement, Product, PurchaseInvoice,
    SaleInvoice, SaleInvoiceItem, SaleInvoiceServiceItem,
    ServiceCatalog, Treasury, Vehicle, VehicleInspection, Vendor,
)
from .views import (
    _get_branch_for_user, _json_response_safe, tenant_required,
    _user_can_edit_branch,
)
from .views.utils import role_required

WALK_IN_PHONE = "0000000000"
WALK_IN_NAME = "عميل نقدي (Walk-in)"

# =====================================================================
# 🔎 بحث عربي مُطبَّع — يوحّد صيغ الحروف عشان "مساعد" تلاقي "مسـاعد"/"مساعد"
# مهما كانت الهمزات أو التطويل أو التاء المربوطة في الداتا المستوردة.
# =====================================================================
# توحيد الحروف: أ إ آ ٱ → ا | ة → ه | ى → ي | ؤ → و | ئ → ي
_AR_FOLD = [('أ', 'ا'), ('إ', 'ا'), ('آ', 'ا'), ('ٱ', 'ا'),
            ('ة', 'ه'), ('ى', 'ي'), ('ؤ', 'و'), ('ئ', 'ي'), ('ـ', '')]
# إزالة التشكيل والتطويل من نص الاستعلام (بايثون)
_AR_DIACRITICS = _re.compile(r'[ـً-ْٰ]')


def _norm_ar(s):
    """تطبيع نص عربي: توحيد الهمزات/التاء/الألف المقصورة + إزالة التشكيل والتطويل."""
    s = (s or '')
    for a, b in _AR_FOLD:
        s = s.replace(a, b)
    s = _AR_DIACRITICS.sub('', s)
    return s.strip().lower()


def _ar_field_expr(field):
    """تعبير DB يطبّع حقل نصّي بنفس قواعد _norm_ar (بدون التشكيل، نادر في الداتا)."""
    expr = F(field)
    for a, b in _AR_FOLD:
        expr = Replace(expr, Value(a), Value(b))
    return Lower(expr)


def _apply_product_search(qs, q):
    """يفلتر منتجات بالبحث العربي المُطبَّع + بالكلمات مهما كان ترتيبها.

    - الاسم بيتطبّع على مستوى DB ويتقارن بالاستعلام المُطبَّع (كل كلمة لازم تظهر).
    - الكود/الباركود/الماركة/الموديل بتتبحث بالنص الخام كمان (icontains).
    """
    q = (q or '').strip()
    if not q:
        return qs
    qs = qs.annotate(_nname=_ar_field_expr('name'))
    qn = _norm_ar(q)
    cond = Q(part_number__icontains=q) | Q(barcode__icontains=q) | \
        Q(brand__icontains=q) | Q(car_model__icontains=q)
    # كل كلمة في الاستعلام لازم تظهر في الاسم المُطبَّع (يسمح باختلاف الترتيب)
    name_cond = Q()
    for tok in qn.split():
        name_cond &= Q(_nname__contains=tok)
    if name_cond:
        cond |= name_cond
    return qs.filter(cond)


def _walk_in_customer():
    """Single shared walk-in row — phone is unique on Customer.

    🛡️ We look up by the *normalized* form because Customer.save() rewrites
    the phone (e.g. '0000000000' → '+00000000'). Looking up by the raw form
    misses the existing row, then the create attempt races into a UNIQUE
    constraint violation and the whole request 500s.
    """
    normalized = Customer.normalize_phone(WALK_IN_PHONE)
    cust, _ = Customer.objects.get_or_create(
        phone=normalized,
        defaults={"name": WALK_IN_NAME},
    )
    return cust


def _normalize_tenders(treasury_id, paid_amount_raw, payments):
    """يوحّد صيغة الدفع: يقبل إما (خزنة واحدة + مبلغ) أو قائمة دفعات مقسّمة.

    بيرجّع list of (treasury_id, Decimal amount) للمبالغ الموجبة فقط.
    ده اللي بيسمح بالدفع المقسّم (جزء كاش + جزء انستا) على نفس الفاتورة.
    """
    tenders = []
    if payments:
        for p in payments:
            try:
                amt = Decimal(str(p.get("amount")))
            except (InvalidOperation, TypeError):
                continue
            tid = p.get("treasury_id")
            if tid and amt > 0:
                tenders.append((tid, amt))
    else:
        try:
            amt = Decimal(str(paid_amount_raw)) if paid_amount_raw not in (None, "") else Decimal("0")
        except InvalidOperation:
            amt = Decimal("0")
        if treasury_id and amt > 0:
            tenders.append((treasury_id, amt))
    return tenders


def _record_invoice_payments(invoice, tenders, request_user):
    """يسجّل دفعة (أو أكثر) لفاتورة بيع بشكل ذرّي — يدعم الدفع المقسّم:
      - كل دفعة = FinancialTransaction(in) على خزنتها (الـ signal بيزوّد الرصيد)
      - invoice.paid_amount = مجموع كل الدفعات (الباقي بيفضل آجل على العميل)
      - invoice.treasury = أول خزنة (للتوافق مع الطباعة/التقارير القديمة)
    بيرجّع إجمالي المدفوع (Decimal). لازم يتنادى جوه transaction.atomic.
    """
    total_paid = Decimal("0")
    primary = None
    for tid, amt in tenders:
        treasury = (Treasury.objects.select_for_update()
                    .filter(id=tid, branch=invoice.branch, is_active=True).first())
        if treasury is None:
            raise ValueError("الخزنة المختارة غير متاحة في هذا الفرع.")
        # 🐛 [DOUBLE-COUNT FIX] لا نزوّد الرصيد يدوياً: إنشاء الـ
        # FinancialTransaction بيطلق signal (update_balance) اللي بيزوّد الخزنة.
        FinancialTransaction.objects.create(
            treasury=treasury,
            transaction_type="in",
            amount=amt,
            description=f"دفعة فاتورة #{invoice.id} ({treasury.name}) — {invoice.customer.name}",
            sale_invoice=invoice,
            customer=invoice.customer,
        )
        total_paid += amt
        if primary is None:
            primary = treasury
    if total_paid <= 0:
        return Decimal("0")
    invoice.paid_amount = total_paid
    invoice.treasury = primary
    invoice.save(update_fields=["paid_amount", "treasury"])
    return total_paid


def _record_invoice_payment(invoice, treasury_id, paid_amount_raw, request_user):
    """توافق خلفي — دفعة واحدة بخزنة واحدة (يستدعي المسجّل المقسّم)."""
    tenders = _normalize_tenders(treasury_id, paid_amount_raw, None)
    return _record_invoice_payments(invoice, tenders, request_user)


def _post_sale_to_ledger(invoice, request_user):
    """يقيّد الفاتورة في دفتر الأستاذ (إيراد/تكلفة/مديونية) بعد ضبط الأصناف.

    البيع السريع (POS وأمر الشغل) بينشئ الفاتورة قبل الأصناف فمابيتقيّدش
    محاسبياً وقتها؛ فبنستدعي المحرّك هنا بعد update_total. الاستدعاء idempotent
    (قيد مبيعات واحد للفاتورة) وأي خطأ محاسبي بيتسجّل ومابيوقفش البيع.
    """
    try:
        from inventory.services.accounting_service import AccountingService
        AccountingService.post_sale_invoice(invoice, created_by=request_user)
    except Exception as exc:  # noqa: BLE001 — قيد الدفتر لا يوقف الفاتورة أبداً
        import logging as _l
        _l.getLogger('mouss_tec_core').warning(
            "[GL] post_sale_invoice failed for INV #%s: %s", invoice.id, exc)


def _resolve_customer(name, phone):
    """Find-or-create by phone (the unique natural key). Blank phone → walk-in.

    🛡️ Normalize before querying — Customer.save() rewrites phones so a
    raw-form lookup would miss and race the UNIQUE constraint.
    """
    raw = (phone or "").strip()
    name = (name or "").strip()
    if not raw:
        return _walk_in_customer()
    normalized = Customer.normalize_phone(raw)
    cust, created = Customer.objects.get_or_create(
        phone=normalized,
        defaults={"name": name or f"عميل {raw}"},
    )
    if not created and name and cust.name != name and cust.name == WALK_IN_NAME:
        cust.name = name
        cust.save(update_fields=["name"])
    return cust


# =====================================================================
# 1. LIGHTNING POS
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
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
    # بحث عربي مُطبَّع (يلاقي الاسم مهما اختلفت صيغة الحروف) + الكود/الباركود
    qs = _apply_product_search(Product.objects.filter(is_active=True), q).distinct()[:12]

    results = []
    for p in qs:
        stock_qs = p.inventory_set.all()
        if branch is not None:
            stock_qs = stock_qs.filter(branch=branch)
        stock = stock_qs.aggregate(s=Sum("quantity"))["s"] or 0
        results.append({
            "id": p.id,
            "sku": p.part_number,
            "name": p.name,
            "brand": p.brand,
            "price": float(p.retail_price or 0),
            "stock": stock,
        })
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
            customer = _resolve_customer(payload.get("customer_name"), payload.get("customer_phone"))

            # Pre-lock + validate
            line_specs = []
            for raw in items:
                pid = int(raw.get("product_id"))
                qty = int(raw.get("qty") or 0)
                if qty <= 0:
                    return _json_response_safe({"error": "كمية غير صالحة."}, status=400)
                try:
                    price = Decimal(str(raw.get("price")))
                except (InvalidOperation, TypeError):
                    return _json_response_safe({"error": "سعر غير صالح."}, status=400)
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

                inv = (Inventory.objects
                       .select_for_update()
                       .filter(product_id=pid, branch=branch)
                       .first())
                if inv is None or inv.quantity < qty:
                    available = inv.quantity if inv else 0
                    return _json_response_safe({
                        "error": f"المخزون غير كافٍ للقطعة #{pid} (متاح: {available}, مطلوب: {qty})."
                    }, status=409)
                line_specs.append((inv, pid, qty, price, line_disc))

            try:
                discount = Decimal(str(payload.get("discount") or "0"))
            except InvalidOperation:
                discount = Decimal("0")

            # 🔒 حد الخصم حسب صلاحية الموظف (المدير/الأدمن بلا حد) — الإجمالي +
            #    خصومات الأصناف تُحتسب معاً مقابل حد الموظف.
            subtotal = sum((Decimal(str(q)) * Decimal(str(p)) for _, _, q, p, _ in line_specs), Decimal("0"))
            line_disc_total = sum((d for _, _, _, _, d in line_specs), Decimal("0"))
            # إجمالي الخصم = خصم الفاتورة + خصومات الأصناف (لفحص حد صلاحية الموظف)
            effective_discount = discount + line_disc_total
            if effective_discount > 0 and subtotal > 0 and not request.user.is_superuser:
                profile = getattr(request.user, "employee_profile", None)
                if profile:
                    disc_pct = (effective_discount / subtotal) * Decimal("100")
                    if not profile.can_apply_discount(disc_pct):
                        return _json_response_safe({
                            "error": (f"الخصم ({disc_pct:.1f}%) يتجاوز الحد المسموح لك "
                                      f"({profile.effective_max_discount:.0f}%). راجع المدير.")
                        }, status=403)

            invoice = SaleInvoice.objects.create(
                invoice_type="sale",
                status="posted",
                customer=customer,
                branch=branch,
                discount=discount,
                paid_amount=Decimal("0.00"),
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
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل إتمام الفاتورة: {exc}"}, status=500)


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

    if Product.objects.filter(part_number=sku).exists():
        return _json_response_safe({"error": f"رقم القطعة '{sku}' موجود مسبقاً."}, status=409)

    try:
        with transaction.atomic():
            product = Product.objects.create(
                part_number=sku,
                name=name,
                brand=(request.POST.get("brand") or "BMW").strip(),
                car_model=(request.POST.get("car_model") or "").strip() or "—",
                car_year=(request.POST.get("car_year") or "").strip() or "—",
                purchase_price=cost,
                retail_price=retail,
                average_cost=cost,
                min_stock_level=int(request.POST.get("min_stock_level") or 2),
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

        return _json_response_safe({
            "ok": True,
            "product_id": product.id,
            "sku": product.part_number,
            "stock": starting_qty,
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل إنشاء القطعة: {exc}"}, status=500)


# =====================================================================
# 3. JOB CARD (Repair Order) — Customer + Vehicle + Parts + Services + DVI
# =====================================================================

DVI_FIELDS = ("brakes_status", "engine_oil_status", "tires_status", "battery_status")


@login_required(login_url='/login/')
@tenant_required
def job_card_create(request):
    branch = _get_branch_for_user(request.user)
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    return render(request, "inventory/job_card_create.html", {
        "branch": branch,
        "branches": Branch.objects.all() if branch is None else None,
        "services": ServiceCatalog.objects.all().order_by("name"),
        "treasuries": treasury_qs,
    })


@login_required(login_url='/login/')
@tenant_required
@require_GET
def customer_search(request):
    """Suggest customers for the Job Card customer panel — match by name or phone."""
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return _json_response_safe({"results": []})
    qs = Customer.objects.filter(Q(name__icontains=q) | Q(phone__icontains=q))[:10]
    results = [{
        "id": c.id, "name": c.name, "phone": c.phone,
        "vip": c.vip_tier,
        "vehicles": [{"id": v.id, "plate": v.car_plate or "—",
                       "chassis": v.chassis_number, "model": v.model_name or ""}
                      for v in c.vehicles.all()[:6]],
    } for c in qs]
    return _json_response_safe({"results": results})


@login_required(login_url='/login/')
@tenant_required
@require_POST
def job_card_save(request):
    """
    Atomic Job Card save: creates SaleInvoice + parts + services + DVI.
    Deducts inventory for any parts on the card (locked rows).
    """
    import json as _json
    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات JSON غير صالحة."}, status=400)

    branch = _get_branch_for_user(request.user)
    if branch is None:
        bid = payload.get("branch_id")
        branch = Branch.objects.filter(id=bid).first() if bid else None
    if branch is None:
        return _json_response_safe({"error": "لم يتم تحديد الفرع."}, status=400)
    if not _user_can_edit_branch(request.user, branch):
        return _json_response_safe({"error": "👁 صلاحيتك في الفرع ده عرض فقط — مش مسموح بالحفظ."}, status=403)

    items = payload.get("items") or []
    services = payload.get("services") or []
    if not items and not services:
        return _json_response_safe({"error": "أضف قطعاً أو خدمات قبل الحفظ."}, status=400)

    try:
        with transaction.atomic():
            # --- Customer ----------------------------------------------------
            cust_id = payload.get("customer_id")
            if cust_id:
                customer = Customer.objects.filter(id=cust_id).first()
                if customer is None:
                    return _json_response_safe({"error": "العميل المحدد غير موجود."}, status=400)
            else:
                customer = _resolve_customer(payload.get("customer_name"), payload.get("customer_phone"))

            # --- Vehicle (optional but recommended for maintenance) ----------
            vehicle = None
            veh_id = payload.get("vehicle_id")
            if veh_id:
                vehicle = Vehicle.objects.filter(id=veh_id, customer=customer).first()
            elif payload.get("vehicle_chassis"):
                chassis = payload["vehicle_chassis"].strip().upper()
                vehicle = Vehicle.objects.filter(chassis_number=chassis).first()
                if vehicle is None:
                    vehicle = Vehicle.objects.create(
                        customer=customer,
                        chassis_number=chassis,
                        car_plate=(payload.get("vehicle_plate") or "").strip() or None,
                        brand=(payload.get("vehicle_brand") or "BMW").strip(),
                        model_name=(payload.get("vehicle_model") or "").strip() or None,
                    )

            # --- Parts: lock + validate stock --------------------------------
            line_specs = []
            for raw in items:
                pid = int(raw.get("product_id"))
                qty = int(raw.get("qty") or 0)
                if qty <= 0:
                    return _json_response_safe({"error": "كمية القطعة غير صالحة."}, status=400)
                try:
                    price = Decimal(str(raw.get("price")))
                except (InvalidOperation, TypeError):
                    return _json_response_safe({"error": "سعر غير صالح."}, status=400)
                inv = (Inventory.objects.select_for_update()
                       .filter(product_id=pid, branch=branch).first())
                if inv is None or inv.quantity < qty:
                    available = inv.quantity if inv else 0
                    return _json_response_safe({
                        "error": f"المخزون غير كافٍ للقطعة #{pid} (متاح: {available}, مطلوب: {qty})."
                    }, status=409)
                line_specs.append((inv, qty, price))

            # --- Create the invoice header ----------------------------------
            try:
                mileage = int(payload.get("mileage")) if payload.get("mileage") else None
            except (TypeError, ValueError):
                mileage = None
            requested_type = payload.get("invoice_type")
            invoice_type = requested_type if requested_type in ("sale", "maintenance") else ("maintenance" if services else "sale")
            invoice = SaleInvoice.objects.create(
                invoice_type=invoice_type,
                status="in_progress",
                customer=customer,
                vehicle=vehicle,
                branch=branch,
                mileage=mileage,
                notes=(payload.get("notes") or "").strip() or None,
                labor_cost_manual=Decimal(str(payload.get("labor_cost_manual") or "0")),
                discount=Decimal(str(payload.get("discount") or "0")),
                tax_percentage=Decimal(str(payload.get("tax_percentage") or "0")),
            )

            # --- Parts -------------------------------------------------------
            for inv, qty, price in line_specs:
                product = inv.product
                SaleInvoiceItem.objects.create(
                    invoice=invoice, product=product, quantity=qty,
                    unit_price=price,
                    cost_at_sale=product.average_cost or Decimal("0.00"),
                )
                before = inv.quantity
                inv.quantity = before - qty
                inv.save(update_fields=["quantity"])
                InventoryMovement.objects.create(
                    product=product, branch=branch, reason="sale",
                    quantity_change=-qty, quantity_before=before, quantity_after=inv.quantity,
                    reference_type="SaleInvoice", reference_id=invoice.id,
                    created_by=request.user,
                )

            # --- Services ----------------------------------------------------
            for svc in services:
                svc_id = int(svc.get("service_id"))
                service = ServiceCatalog.objects.filter(id=svc_id).first()
                if service is None:
                    continue
                price = svc.get("price")
                SaleInvoiceServiceItem.objects.create(
                    invoice=invoice, service=service,
                    price=Decimal(str(price)) if price not in (None, "") else None,
                )

            # --- DVI (only if vehicle attached) ------------------------------
            dvi = payload.get("dvi") or {}
            if vehicle and any(dvi.get(k) for k in DVI_FIELDS):
                VehicleInspection.objects.create(
                    invoice=invoice, vehicle=vehicle,
                    brakes_status=dvi.get("brakes_status") or "green",
                    engine_oil_status=dvi.get("engine_oil_status") or "green",
                    tires_status=dvi.get("tires_status") or "green",
                    battery_status=dvi.get("battery_status") or "green",
                    technician_notes=(dvi.get("technician_notes") or "").strip(),
                )

            invoice.update_total()
            # 📒 قيّد الإيراد/التكلفة/المديونية في دفتر الأستاذ (بعد ضبط الأصناف)
            _post_sale_to_ledger(invoice, request.user)
            # الدفع — واحدة أو مقسّمة (كاش/انستا/…)، والباقي آجل على العميل.
            tenders = _normalize_tenders(payload.get("treasury_id"),
                                         payload.get("paid_amount"),
                                         payload.get("payments"))
            if tenders:
                _record_invoice_payments(invoice, tenders, request.user)
                invoice.refresh_from_db()

            # 🛡️ الجزء الآجل يتسجّل على رصيد العميل. أمر الشغل بيبقى in_progress
            # فمابيشتغلش execute_sale (اللي بيقيّد الآجل)، فبنسجّله يدوياً هنا زي POS.
            due = invoice.due_amount
            if due > Decimal("0.00"):
                from django.db.models import F as _F
                Customer.objects.filter(pk=customer.pk).update(balance=_F("balance") + due)

            # 🛡️ علّمنا الفاتورة كـ «مُطبّقة» عشان لو اتحوّلت لـ posted لاحقاً
            # (كشك/أدمن) ما يشتغلش execute_sale تاني فيخصم المخزون ويقيّد الآجل
            # مرة تانية (double-post). كل الأثر المالي والمخزوني اتسجّل يدوياً هنا.
            SaleInvoice.objects.filter(pk=invoice.pk).update(is_applied=True)

        return _json_response_safe({
            "ok": True,
            "invoice_id": invoice.id,
            "total": float(invoice.total_amount),
            "paid": float(invoice.paid_amount),
            "due": float(invoice.due_amount),
            "print_url": reverse("inventory:print_invoice_a4", args=[invoice.id]),
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ أمر الشغل: {exc}"}, status=500)


# =====================================================================
# 4. QUICK EXPENSE — daily out-of-pocket expense entry
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def quick_expense(request):
    branch = _get_branch_for_user(request.user)
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)

    # Salary employees — surfaced when category.system_key == 'salaries'
    emp_qs = (EmployeeProfile.objects
              .select_related('user', 'branch')
              .order_by('user__first_name', 'user__username'))
    if branch is not None:
        emp_qs = emp_qs.filter(Q(branch=branch) | Q(branch__isnull=True))

    return render(request, "inventory/quick_expense.html", {
        "branch": branch,
        "branches": Branch.objects.all() if branch is None else None,
        "treasuries": treasury_qs,
        "categories": ExpenseCategory.objects.all().order_by("name"),
        "salary_employees": emp_qs,
    })


@login_required(login_url='/login/')
@tenant_required
@require_POST
def quick_expense_create(request):
    treasury_id = request.POST.get("treasury_id")
    try:
        amount = Decimal(str(request.POST.get("amount") or "0"))
    except InvalidOperation:
        amount = Decimal("0")
    if amount <= 0:
        return _json_response_safe({"error": "أدخل مبلغاً صحيحاً أكبر من صفر."}, status=400)
    if not treasury_id:
        return _json_response_safe({"error": "اختر الخزنة."}, status=400)
    description = (request.POST.get("description") or "").strip() or "مصروف يومي"
    category_id = request.POST.get("category_id") or None
    employee_id = request.POST.get("employee_id") or None

    try:
        with transaction.atomic():
            treasury = (Treasury.objects.select_for_update()
                        .filter(id=treasury_id, is_active=True).first())
            if treasury is None:
                return _json_response_safe({"error": "الخزنة غير موجودة."}, status=404)
            if not _user_can_edit_branch(request.user, treasury.branch):
                return _json_response_safe({"error": "👁 صلاحيتك في فرع الخزنة دي عرض فقط — مش مسموح بالصرف."}, status=403)
            if (treasury.balance or Decimal("0")) < amount:
                return _json_response_safe({
                    "error": f"رصيد الخزنة غير كافٍ (متاح: {treasury.balance})."
                }, status=409)
            category = ExpenseCategory.objects.filter(id=category_id).first() if category_id else None

            # 👥 If category is 'salaries', require an employee link
            employee = None
            if category and category.system_key == 'salaries':
                if not employee_id:
                    return _json_response_safe({
                        "error": "اختر الموظف المستلم للراتب."
                    }, status=400)
                employee = EmployeeProfile.objects.filter(id=employee_id).first()
                if employee is None:
                    return _json_response_safe({"error": "الموظف غير موجود."}, status=404)
                # Stamp the description for ledger clarity
                description = f"{description} — {employee.user.get_full_name() or employee.user.username}"

            tx = FinancialTransaction.objects.create(
                treasury=treasury,
                transaction_type="out",
                amount=amount,
                description=description,
                category=category,
                employee=employee,
            )
        treasury.refresh_from_db(fields=["balance"])
        return _json_response_safe({
            "ok": True,
            "transaction_id": tx.id,
            "new_balance": float(treasury.balance),
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل تسجيل المصروف: {exc}"}, status=500)


# =====================================================================
# 💸 إدارة المصاريف — عرض / تعديل الخزنة / حذف
# =====================================================================
def _is_operating_expense(ft):
    """المصروف التشغيلي = سحب (out) مش مرتبط بفاتورة/مورد/عميل ولا تحويل بين خزائن."""
    return (ft.transaction_type == 'out'
            and ft.sale_invoice_id is None
            and ft.purchase_invoice_id is None
            and ft.vendor_id is None
            and ft.customer_id is None
            and not (ft.description or "").startswith(_TRANSFER_TAG))


def _delete_expense_ft(ft):
    """يحذف حركة مصروف ويرجّع أثرها على رصيد الخزنة + يشيل قيوده المحاسبية.

    مفيش post_delete signal بيصلّح الرصيد، فبنرجّعه يدوياً هنا. القيود
    المحاسبية بتتشال عشان الدفاتر تفضل متوازنة.
    """
    from django.db.models import F as _F
    from inventory.models import AccountingEntry, JournalEntry
    if ft.transaction_type == 'out':
        Treasury.objects.filter(pk=ft.treasury_id).update(balance=_F('balance') + ft.amount)
    else:
        Treasury.objects.filter(pk=ft.treasury_id).update(balance=_F('balance') - ft.amount)
    JournalEntry.objects.filter(financial_transaction=ft).delete()
    AccountingEntry.objects.filter(financial_transaction=ft).delete()
    ft.delete()


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def expense_list(request):
    """قائمة المصاريف التشغيلية للفرع النشط مع تعديل/حذف."""
    branch = _get_branch_for_user(request.user)
    qs = (FinancialTransaction.objects
          .filter(transaction_type='out', sale_invoice__isnull=True,
                  purchase_invoice__isnull=True, vendor__isnull=True,
                  customer__isnull=True)
          .exclude(description__startswith=_TRANSFER_TAG)  # التحويلات وسداد الموردين مش مصاريف
          .select_related('treasury', 'treasury__branch', 'category', 'employee__user')
          .order_by('-date', '-id'))
    if branch is not None:
        qs = qs.filter(treasury__branch=branch)

    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(category__name__icontains=q))

    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    total = qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    return render(request, 'inventory/expense_list.html', {
        'page': page, 'q': q, 'branch': branch, 'total': total,
        'can_edit': _can_edit_invoices(request.user),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
def expense_edit(request, pk):
    """تعديل مصروف — تقدر تغيّر الخزنة والمبلغ والبيان والبند.

    التغيير بيتم بحذف الحركة القديمة (مع إرجاع رصيد خزنتها) وإنشاء حركة
    جديدة على الخزنة المختارة — فالرصيد بيتظبط صح على الخزنتين.
    """
    ft = (FinancialTransaction.objects
          .select_related('treasury', 'treasury__branch', 'category').filter(pk=pk).first())
    if not ft or not _is_operating_expense(ft):
        return redirect(f"{reverse('inventory:expense_list')}?err=notfound")
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, ft.treasury.branch)):
        return redirect(f"{reverse('inventory:expense_list')}?err=perm")

    branch = ft.treasury.branch
    treasuries = Treasury.objects.filter(is_active=True, branch=branch).order_by('name')

    if request.method == 'POST':
        new_tid = request.POST.get('treasury_id')
        try:
            new_amount = Decimal(str(request.POST.get('amount') or '0'))
        except InvalidOperation:
            new_amount = Decimal('0')
        new_desc = (request.POST.get('description') or '').strip() or ft.description
        new_cat_id = request.POST.get('category_id') or None
        if new_amount <= 0:
            return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err=amount")
        new_treasury = Treasury.objects.filter(id=new_tid, is_active=True, branch=branch).first()
        if new_treasury is None:
            return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err=treasury")
        if not _user_can_edit_branch(request.user, new_treasury.branch):
            return redirect(f"{reverse('inventory:expense_list')}?err=perm")
        try:
            with transaction.atomic():
                # الرصيد المتاح على الخزنة الجديدة بعد إرجاع القديمة (لو نفس الخزنة)
                available = new_treasury.balance or Decimal('0')
                if new_treasury.pk == ft.treasury_id:
                    available += ft.amount  # هيترجّع أول ما نحذف القديمة
                if available < new_amount:
                    return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err=balance")
                category = ExpenseCategory.objects.filter(id=new_cat_id).first() if new_cat_id else ft.category
                employee = ft.employee
                _delete_expense_ft(ft)
                FinancialTransaction.objects.create(
                    treasury=new_treasury, transaction_type='out', amount=new_amount,
                    description=new_desc, category=category, employee=employee,
                )
        except Exception:
            return redirect(f"{reverse('inventory:expense_edit', args=[pk])}?err=fail")
        return redirect(f"{reverse('inventory:expense_list')}?ok=edited")

    return render(request, 'inventory/expense_edit.html', {
        'ft': ft, 'treasuries': treasuries, 'branch': branch,
        'categories': ExpenseCategory.objects.all().order_by('name'),
        'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@require_POST
def expense_delete(request, pk):
    """🗑️ حذف مصروف مع إرجاع قيمته لرصيد الخزنة."""
    ft = FinancialTransaction.objects.select_related('treasury__branch').filter(pk=pk).first()
    if not ft or not _is_operating_expense(ft):
        return redirect(f"{reverse('inventory:expense_list')}?err=notfound")
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, ft.treasury.branch)):
        return redirect(f"{reverse('inventory:expense_list')}?err=perm")
    try:
        with transaction.atomic():
            _delete_expense_ft(ft)
    except Exception:
        return redirect(f"{reverse('inventory:expense_list')}?err=fail")
    return redirect(f"{reverse('inventory:expense_list')}?ok=deleted")


# =====================================================================
# 5. MODERN LIST VIEWS — replace the Django admin changelist for daily ops
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def sale_invoice_list(request):
    branch = _get_branch_for_user(request.user)
    qs = (SaleInvoice.objects
          .select_related("customer", "vehicle", "branch")
          .order_by("-date_created"))
    if branch is not None:
        qs = qs.filter(branch=branch)

    q = (request.GET.get("q") or "").strip()
    if q:
        cond = Q(customer__name__icontains=q) | Q(customer__phone__icontains=q)
        if q.isdigit():
            cond |= Q(id=int(q))
        qs = qs.filter(cond)

    status = (request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)

    inv_type = (request.GET.get("type") or "").strip()
    if inv_type in ("sale", "maintenance"):
        qs = qs.filter(invoice_type=inv_type)

    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "inventory/sale_invoice_list.html", {
        "page": page,
        "q": q,
        "status": status,
        "inv_type": inv_type,
        "status_choices": SaleInvoice.STATUS_CHOICES,
        "type_choices": SaleInvoice.INVOICE_TYPES,
        "branch": branch,
        "can_return": _can_process_returns(request.user) and _user_can_edit_branch(request.user, branch),
        "can_edit": _can_edit_invoices(request.user) and _user_can_edit_branch(request.user, branch),
        "flash_returned": request.GET.get("returned"),
        "flash_deleted": request.GET.get("deleted"),
        "flash_err": request.GET.get("err"),
    })


def _can_process_returns(user):
    if user.is_superuser:
        return True
    prof = getattr(user, 'employee_profile', None)
    if not prof:
        return False
    return prof.role in ('admin', 'manager', 'accountant', 'cashier') or prof.can_edit_posted_invoices


@login_required(login_url='/login/')
@tenant_required
@require_POST
def sale_invoice_return(request, pk):
    """♻️ عمل مرتجع كامل لفاتورة معتمدة — يرجّع المخزون ويرد المبلغ من الخزنة.

    يستخدم InvoiceService.create_return_invoice ثم يعتمد المرتجع (status=posted)
    فتشتغل خطوة execute_sale اللي بتزوّد المخزون وتسجّل سحب رد الفلوس.
    """
    from inventory.services.invoice_service import InvoiceService
    from django.core.exceptions import ValidationError

    if not _can_process_returns(request.user):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")

    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=notfound")
    if not _user_can_edit_branch(request.user, invoice.branch):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")

    # منع المرتجع المكرر لنفس الفاتورة
    if invoice.is_return or invoice.status != 'posted' or invoice.return_invoices.exists():
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=cannot")

    try:
        with transaction.atomic():
            ret = InvoiceService.create_return_invoice(invoice)
            ret.status = 'posted'
            ret.save()  # يطلق execute_sale → إرجاع المخزون + سحب رد المبلغ
    except ValidationError as e:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=cannot")
    except Exception:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=fail")

    return redirect(f"{reverse('inventory:sale_invoice_list')}?returned={invoice.id}&ret_id={ret.id}")


# =====================================================================
# ✏️🗑️ تعديل / حذف الفواتير + تسوية دفعاتها (خزائن)
# =====================================================================
def _can_edit_invoices(user):
    """صلاحية تعديل/حذف الفواتير والمصاريف — أدمن/مدير/محاسب أو من مُنح الصلاحية."""
    if user.is_superuser:
        return True
    prof = getattr(user, 'employee_profile', None)
    if not prof:
        return False
    return prof.role in ('admin', 'manager', 'accountant') or prof.can_edit_posted_invoices


def _resync_invoice_paid(invoice):
    """يعيد حساب المدفوع = Σ(دفعات in) − Σ(تسويات out) على الفاتورة، ويحفظه.

    ده بيخلّي التسويات (عكس دفعة) والدفعات الجديدة تتجمّع بشكل صحيح من غير
    ما نمسح أي حركة مالية (كل حاجة بتفضل في السجل للمراجعة).
    """
    agg = invoice.payments.aggregate(
        ins=Sum('amount', filter=Q(transaction_type='in')),
        outs=Sum('amount', filter=Q(transaction_type='out')),
    )
    net = (agg['ins'] or Decimal('0')) - (agg['outs'] or Decimal('0'))
    invoice.paid_amount = net if net > 0 else Decimal('0.00')
    last_in = (invoice.payments.filter(transaction_type='in')
               .order_by('-id').first())
    invoice.treasury = last_in.treasury if last_in else None
    invoice.save(update_fields=['paid_amount', 'treasury'])
    return invoice.paid_amount


def _purge_invoice_payments(invoice):
    """يمسح كل دفعات الفاتورة نهائياً ويرجّع أرصدة خزائنها + يشيل قيودها.

    حذف حقيقي (مش حركة تسوية تفضل في السجل) — عشان تعديل/حذف الدفعات يسيب
    السجل نضيف من غير تسويات أو تكرار.
    """
    from django.db.models import F as _F
    from inventory.models import AccountingEntry, JournalEntry
    for ft in list(invoice.payments.all()):
        if ft.transaction_type == 'in':
            Treasury.objects.filter(pk=ft.treasury_id).update(balance=_F('balance') - ft.amount)
        else:
            Treasury.objects.filter(pk=ft.treasury_id).update(balance=_F('balance') + ft.amount)
        JournalEntry.objects.filter(financial_transaction=ft).delete()
        AccountingEntry.objects.filter(financial_transaction=ft).delete()
        ft.delete()


@login_required(login_url='/login/')
@tenant_required
@require_POST
def sale_invoice_delete(request, pk):
    """🗑️ حذف فاتورة بيع مع عكس كل أثرها المالي والمخزوني:
      - يرجّع الكميات للمخزون
      - يسوّي كل الدفعات (يرجّع فلوس الخزائن)
      - يشيل الجزء الآجل من رصيد العميل
      - يعكس عمولة البائع لو اتحسبت
    الحذف للفواتير العادية فقط (مش المرتجعات، ومش فاتورة عليها مرتجع)."""
    if not _can_edit_invoices(request.user):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")

    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=notfound")
    if not _user_can_edit_branch(request.user, invoice.branch):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")
    if invoice.is_return:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=cannot_del_return")
    if invoice.return_invoices.exists():
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=has_return")

    try:
        with transaction.atomic():
            from django.db.models import F as _F
            from inventory.models import AccountingEntry, JournalEntry
            inv_id = invoice.id
            due_before = invoice.due_amount

            # 1) رجّع المخزون واحذف حركات المخزون بتاعة الفاتورة (من غير أي أثر باقي)
            for item in invoice.items.select_related('product').all():
                inv = (Inventory.objects.select_for_update()
                       .filter(product=item.product, branch=invoice.branch).first())
                if inv is not None:
                    inv.quantity = (inv.quantity or 0) + item.quantity
                    inv.save(update_fields=['quantity'])
                # اعكس عمولة البائع المحسوبة على السطر
                if getattr(item, 'commission_accrued', None) and item.salesperson_id:
                    EmployeeProfile.objects.filter(pk=item.salesperson_id).update(
                        commission_balance=_F('commission_balance') - item.commission_accrued)
            InventoryMovement.objects.filter(
                reference_type='SaleInvoice', reference_id=inv_id).delete()

            # 2) احذف دفعات الفاتورة نهائياً + رجّع أرصدة الخزائن + امسح قيودها
            #    (حذف حقيقي — مش حركة تسوية تظهر في المصاريف)
            for ft in list(invoice.payments.all()):
                if ft.transaction_type == 'in':
                    Treasury.objects.filter(pk=ft.treasury_id).update(
                        balance=_F('balance') - ft.amount)
                else:
                    Treasury.objects.filter(pk=ft.treasury_id).update(
                        balance=_F('balance') + ft.amount)
                JournalEntry.objects.filter(financial_transaction=ft).delete()
                AccountingEntry.objects.filter(financial_transaction=ft).delete()
                ft.delete()

            # 3) شيل الجزء الآجل من رصيد العميل
            if due_before > Decimal('0.00') and invoice.customer_id:
                Customer.objects.filter(pk=invoice.customer_id).update(
                    balance=_F('balance') - due_before)

            # 4) امسح قيد المبيعات من دفتر الأستاذ (إيراد/تكلفة/مديونية) — كأنه ما اتعملش
            JournalEntry.objects.filter(sale_invoice=invoice, journal_type='sales').delete()

            # 5) احذف الفاتورة (السطور بتتشال cascade)
            invoice.delete()
    except Exception:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=del_fail")

    return redirect(f"{reverse('inventory:sale_invoice_list')}?deleted={pk}")


@login_required(login_url='/login/')
@tenant_required
def sale_invoice_edit(request, pk):
    """✏️ تعديل خزائن/دفعات فاتورة بعد إنشائها.

    GET: يعرض الفاتورة ودفعاتها الحالية + خزائن الفرع.
    POST: يسوّي كل الدفعات الحالية ثم يسجّل الدفعات الجديدة (تقسيم كاش/انستا…)،
          ويظبط الجزء الآجل على رصيد العميل. كده تقدر تغيّر الخزنة أو المبلغ
          المدفوع من غير ما تعيد عمل الفاتورة.
    """
    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch', 'treasury')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=notfound")
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, invoice.branch)):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")
    if invoice.is_return:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=cannot")

    treasuries = Treasury.objects.filter(is_active=True, branch=invoice.branch).order_by('name')

    if request.method == 'POST':
        import json as _json
        try:
            payload = _json.loads(request.body or b"{}")
        except ValueError:
            return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)
        raw_payments = payload.get("payments") or []
        tenders = []
        for p in raw_payments:
            try:
                amt = Decimal(str(p.get("amount")))
            except (InvalidOperation, TypeError):
                continue
            tid = p.get("treasury_id")
            if tid and amt > 0:
                tenders.append((tid, amt))
        try:
            with transaction.atomic():
                due_before = invoice.due_amount
                # 1) امسح الدفعات القديمة نهائياً (رجّع الخزائن) — من غير تسويات
                _purge_invoice_payments(invoice)
                # 2) سجّل الدفعات الجديدة
                if tenders:
                    _record_invoice_payments(invoice, tenders, request.user)
                # 3) أعِد حساب المدفوع من كل الحركات
                _resync_invoice_paid(invoice)
                invoice.refresh_from_db()
                # 4) ظبط الآجل على رصيد العميل (الفرق بين الآجل القديم والجديد)
                due_after = invoice.due_amount
                delta = due_after - due_before
                if delta != 0 and invoice.customer_id:
                    from django.db.models import F as _F
                    Customer.objects.filter(pk=invoice.customer_id).update(
                        balance=_F('balance') + delta)
            return _json_response_safe({
                "ok": True, "invoice_id": invoice.id,
                "total": float(invoice.total_amount),
                "paid": float(invoice.paid_amount),
                "due": float(invoice.due_amount),
            })
        except Exception as exc:  # noqa: BLE001
            return _json_response_safe({"error": f"فشل التعديل: {exc}"}, status=500)

    current = list(invoice.payments.filter(transaction_type='in')
                   .select_related('treasury').order_by('id'))
    return render(request, "inventory/sale_invoice_edit.html", {
        "invoice": invoice,
        "treasuries": treasuries,
        "current_payments": current,
    })


@login_required(login_url='/login/')
@tenant_required
def product_list(request):
    branch = _get_branch_for_user(request.user)
    qs = Product.objects.filter(is_active=True).order_by("name")

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = _apply_product_search(qs, q)

    stock_filter = (request.GET.get("stock") or "").strip()
    page = Paginator(qs, 30).get_page(request.GET.get("page"))

    # annotate live stock + low-stock flag for the page slice only (avoid full-table aggregate)
    # + توزيع القطعة على الفروع (اسم الفرع : الكمية) — يظهر خصوصاً في وضع «كل الفروع»
    products_view = []
    for p in page.object_list:
        inv_rows = list(p.inventory_set.select_related("branch").all())
        if branch is not None:
            inv_rows = [r for r in inv_rows if r.branch_id == branch.id]
        stock = sum(r.quantity for r in inv_rows)
        # توزيع الكميات على الفروع (بس الفروع اللي فيها كمية)
        by_branch = [(r.branch.name, r.quantity) for r in inv_rows if r.quantity]
        by_branch.sort(key=lambda x: (-x[1], x[0]))
        is_low = stock <= (p.min_stock_level or 0)
        line_value = Decimal(str(stock)) * Decimal(str(p.purchase_price or 0))
        products_view.append({"product": p, "stock": stock, "is_low": is_low,
                              "value": line_value, "by_branch": by_branch})

    if stock_filter == "low":
        products_view = [r for r in products_view if r["is_low"]]
    elif stock_filter == "out":
        products_view = [r for r in products_view if r["stock"] == 0]

    # 📊 KPI summary across the WHOLE catalogue (not just this page) — a
    # professional stock overview: units on hand, capital tied up (cost),
    # retail value, and low/out counts.
    from django.db.models import ExpressionWrapper, DecimalField, IntegerField, F
    from django.db.models.functions import Coalesce
    inv_qs = Inventory.objects.filter(product__is_active=True)
    if branch is not None:
        inv_qs = inv_qs.filter(branch=branch)
    money = DecimalField(max_digits=16, decimal_places=2)
    agg = inv_qs.aggregate(
        units=Coalesce(Sum("quantity"), 0),
        capital=Coalesce(Sum(ExpressionWrapper(F("quantity") * F("product__purchase_price"), output_field=money)), Decimal("0")),
        retail=Coalesce(Sum(ExpressionWrapper(F("quantity") * F("product__retail_price"), output_field=money)), Decimal("0")),
    )
    prod_stock = Product.objects.filter(is_active=True)
    stock_sum = Sum("inventory__quantity", filter=Q(inventory__branch=branch) if branch is not None else None)
    prod_stock = prod_stock.annotate(_stock=Coalesce(stock_sum, 0, output_field=IntegerField()))
    summary = {
        "products": qs.count(),
        "units": agg["units"] or 0,
        "capital": agg["capital"] or Decimal("0"),
        "retail": agg["retail"] or Decimal("0"),
        "out_count": prod_stock.filter(_stock__lte=0).count(),
        "low_count": prod_stock.filter(_stock__gt=0, _stock__lte=F("min_stock_level")).count(),
    }

    return render(request, "inventory/product_list.html", {
        "page": page,
        "rows": products_view,
        "q": q,
        "stock_filter": stock_filter,
        "branch": branch,
        "summary": summary,
    })


# =====================================================================
# 💰 الخزائن — عرض وإضافة خزنة للفرع النشط
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def treasury_list(request):
    """قائمة خزائن الفرع النشط + إضافة خزنة جديدة.

    - الأدمن في وضع «كل الفروع» بيشوف كل الخزائن ويختار الفرع وقت الإضافة.
    - لو مركّز على فرع (أو موظف فرع)، الخزائن والإضافة بتبقى للفرع ده تلقائياً.
    """
    branch = _get_branch_for_user(request.user)
    qs = Treasury.objects.select_related('branch').order_by('branch__name', 'name')
    if branch is not None:
        qs = qs.filter(branch=branch)
    rows = list(qs)
    total = sum((t.balance or Decimal('0')) for t in rows)
    return render(request, "inventory/treasury_list.html", {
        "rows": rows,
        "branch": branch,
        "total": total,
        "type_choices": Treasury.TYPE_CHOICES,
        # لما الأدمن في وضع «كل الفروع» نعرض قائمة الفروع لاختيار مكان الخزنة
        "branches": Branch.objects.all().order_by('name') if branch is None else None,
        "can_edit": _user_can_edit_branch(request.user, branch) if branch is not None else True,
        "flash": request.GET.get("ok"),
        "flash_err": request.GET.get("err"),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_create(request):
    """إنشاء خزنة جديدة للفرع النشط (أو فرع مختار في وضع كل الفروع)."""
    name = (request.POST.get("name") or "").strip()
    ttype = (request.POST.get("type") or "cash").strip()
    branch = _get_branch_for_user(request.user)
    if branch is None:
        bid = request.POST.get("branch")
        branch = Branch.objects.filter(id=bid).first() if bid else None

    if not name:
        return redirect(f"{reverse('inventory:treasury_list')}?err=name")
    if branch is None:
        return redirect(f"{reverse('inventory:treasury_list')}?err=branch")
    if not _user_can_edit_branch(request.user, branch):
        return redirect(f"{reverse('inventory:treasury_list')}?err=perm")
    valid_types = {t[0] for t in Treasury.TYPE_CHOICES}
    if ttype not in valid_types:
        ttype = "cash"
    if Treasury.objects.filter(name__iexact=name, branch=branch).exists():
        return redirect(f"{reverse('inventory:treasury_list')}?err=dup")

    Treasury.objects.create(name=name, type=ttype, branch=branch, balance=Decimal('0'))
    return redirect(f"{reverse('inventory:treasury_list')}?ok=1")


# =====================================================================
# 💳 الحركات المالية — سجل موحّد + كشف حساب الخزنة + إيداع/سحب/تحويل
# =====================================================================
_TRANSFER_TAG = "[تحويل:"


def _txn_meta(ft):
    """يصنّف الحركة المالية ويحدد مصدرها ورابط تعديلها (من الفاتورة/المصروف).

    - مرتبطة بفاتورة بيع → رابط تعديل دفع الفاتورة.
    - مرتبطة بفاتورة شراء → مصدر شراء (تعديل من الأدمن).
    - سحب تشغيلي (out) غير مرتبط بفاتورة → تعديل كمصروف.
    - إيداع/سحب يدوي أو تحويل → قابل للحذف من مكانه.
    """
    if ft.sale_invoice_id:
        inv = ft.sale_invoice
        if inv is not None and not inv.is_return:
            return {"label": f"فاتورة بيع #{inv.id}", "kind": "sale",
                    "url": reverse('inventory:sale_invoice_edit', args=[inv.id]), "deletable": False}
        if inv is not None:
            return {"label": f"مرتجع #{inv.id}", "kind": "return", "url": None, "deletable": False}
    if ft.purchase_invoice_id or ft.vendor_id:
        label = (f"فاتورة شراء #{ft.purchase_invoice_id}" if ft.purchase_invoice_id
                 else "سداد مورد")
        url = reverse('inventory:vendor_detail', args=[ft.vendor_id]) if ft.vendor_id else None
        return {"label": label, "kind": "purchase", "url": url, "deletable": False}
    # تحصيل من عميل — يُدار من كشف حساب العميل (حذفه هنا مش هيرجّع رصيد العميل)
    if ft.customer_id:
        return {"label": "تحصيل عميل", "kind": "customer",
                "url": reverse('inventory:customer_detail', args=[ft.customer_id]), "deletable": False}
    is_transfer = (ft.description or "").startswith(_TRANSFER_TAG)
    if ft.transaction_type == 'out' and not is_transfer:
        return {"label": "مصروف / سحب", "kind": "expense",
                "url": reverse('inventory:expense_edit', args=[ft.id]), "deletable": True}
    return {"label": ("تحويل" if is_transfer else "إيداع/حركة يدوية"),
            "kind": "transfer" if is_transfer else "manual", "url": None, "deletable": True}


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def transactions_list(request):
    """💳 سجل كل الحركات المالية (إيداع/سحب) على مستوى الفرع النشط، بفلاتر
    وإجماليات، وكل حركة برابط لمصدرها للتعديل."""
    branch = _get_branch_for_user(request.user)
    qs = (FinancialTransaction.objects
          .select_related('treasury', 'treasury__branch', 'sale_invoice', 'category', 'customer')
          .order_by('-date', '-id'))
    if branch is not None:
        qs = qs.filter(treasury__branch=branch)

    ttype = (request.GET.get('type') or '').strip()
    if ttype in ('in', 'out'):
        qs = qs.filter(transaction_type=ttype)
    tre_id = (request.GET.get('treasury') or '').strip()
    if tre_id.isdigit():
        qs = qs.filter(treasury_id=int(tre_id))
    q = (request.GET.get('q') or '').strip()
    if q:
        cond = Q(description__icontains=q) | Q(customer__name__icontains=q)
        if q.isdigit():
            cond |= Q(sale_invoice_id=int(q))
        qs = qs.filter(cond)

    agg = qs.aggregate(
        tin=Sum('amount', filter=Q(transaction_type='in')),
        tout=Sum('amount', filter=Q(transaction_type='out')),
    )
    total_in = agg['tin'] or Decimal('0')
    total_out = agg['tout'] or Decimal('0')

    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    rows = [{"ft": ft, "meta": _txn_meta(ft)} for ft in page.object_list]

    treasuries = Treasury.objects.select_related('branch')
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)

    return render(request, 'inventory/transactions_list.html', {
        'page': page, 'rows': rows, 'branch': branch,
        'total_in': total_in, 'total_out': total_out, 'net': total_in - total_out,
        'ttype': ttype, 'q': q, 'tre_id': tre_id,
        'treasuries': treasuries.order_by('name'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def treasury_detail(request, pk):
    """🧾 كشف حساب خزنة واحدة — كل حركاتها + رصيد جاري + روابط تعديل المصدر."""
    branch = _get_branch_for_user(request.user)
    t = Treasury.objects.select_related('branch').filter(pk=pk).first()
    if not t or (branch is not None and t.branch_id != branch.id):
        return redirect(f"{reverse('inventory:treasury_list')}?err=notfound")

    base = (FinancialTransaction.objects
            .select_related('sale_invoice', 'category', 'customer')
            .filter(treasury=t))

    # رصيد جاري لكل حركة (نحسبه من كل السجل تصاعدياً مرة واحدة)
    running = {}
    ledger = list(base.order_by('date', 'id').values('id', 'transaction_type', 'amount'))
    if len(ledger) <= 8000:
        bal = Decimal('0')
        for r in ledger:
            bal += r['amount'] if r['transaction_type'] == 'in' else -r['amount']
            running[r['id']] = bal

    qs = base.order_by('-date', '-id')
    ttype = (request.GET.get('type') or '').strip()
    if ttype in ('in', 'out'):
        qs = qs.filter(transaction_type=ttype)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(customer__name__icontains=q))

    agg = qs.aggregate(
        tin=Sum('amount', filter=Q(transaction_type='in')),
        tout=Sum('amount', filter=Q(transaction_type='out')),
    )
    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    rows = [{"ft": ft, "meta": _txn_meta(ft), "running": running.get(ft.id)}
            for ft in page.object_list]

    can_edit = _user_can_edit_branch(request.user, t.branch)
    other_treasuries = (Treasury.objects.filter(is_active=True, branch=t.branch)
                        .exclude(pk=t.pk).order_by('name'))
    return render(request, 'inventory/treasury_statement.html', {
        'treasury': t, 'rows': rows, 'page': page, 'branch': branch,
        'total_in': agg['tin'] or Decimal('0'), 'total_out': agg['tout'] or Decimal('0'),
        'ttype': ttype, 'q': q, 'can_edit': can_edit,
        'other_treasuries': other_treasuries,
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_movement(request, pk):
    """➕ إيداع أو سحب يدوي على خزنة (رأس مال، تسوية، سحب شخصي… إلخ)."""
    t = Treasury.objects.filter(pk=pk, is_active=True).first()
    if not t:
        return redirect(f"{reverse('inventory:treasury_list')}?err=notfound")
    if not _user_can_edit_branch(request.user, t.branch):
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=perm")
    direction = (request.POST.get('direction') or '').strip()
    if direction not in ('in', 'out'):
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=dir")
    try:
        amount = Decimal(str(request.POST.get('amount') or '0'))
    except InvalidOperation:
        amount = Decimal('0')
    if amount <= 0:
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=amount")
    desc = (request.POST.get('description') or '').strip() or ("إيداع يدوي" if direction == 'in' else "سحب يدوي")
    with transaction.atomic():
        locked = Treasury.objects.select_for_update().get(pk=t.pk)
        if direction == 'out' and (locked.balance or Decimal('0')) < amount:
            return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=balance")
        FinancialTransaction.objects.create(
            treasury=locked, transaction_type=direction, amount=amount, description=desc)
    return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?ok=moved")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_transfer(request, pk):
    """🔁 تحويل مبلغ من خزنة لخزنة تانية (نفس الفرع) — حركتين مربوطتين."""
    src = Treasury.objects.filter(pk=pk, is_active=True).first()
    if not src:
        return redirect(f"{reverse('inventory:treasury_list')}?err=notfound")
    if not _user_can_edit_branch(request.user, src.branch):
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=perm")
    dst = Treasury.objects.filter(id=request.POST.get('to_id'), is_active=True,
                                  branch=src.branch).first()
    if not dst or dst.pk == src.pk:
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=dest")
    try:
        amount = Decimal(str(request.POST.get('amount') or '0'))
    except InvalidOperation:
        amount = Decimal('0')
    if amount <= 0:
        return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=amount")
    import uuid as _uuid
    ref = f"{_TRANSFER_TAG}{_uuid.uuid4().hex[:8]}]"
    with transaction.atomic():
        locked = Treasury.objects.select_for_update().get(pk=src.pk)
        if (locked.balance or Decimal('0')) < amount:
            return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?err=balance")
        FinancialTransaction.objects.create(
            treasury=locked, transaction_type='out', amount=amount,
            description=f"{ref} تحويل إلى {dst.name}")
        FinancialTransaction.objects.create(
            treasury=dst, transaction_type='in', amount=amount,
            description=f"{ref} تحويل من {src.name}")
    return redirect(f"{reverse('inventory:treasury_detail', args=[pk])}?ok=transferred")


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def treasury_txn_delete(request, pk):
    """🗑️ حذف حركة يدوية/تحويل (مش مرتبطة بفاتورة) مع إرجاع الرصيد.

    التحويل بيتحذف بطرفيه معاً. الحركات المرتبطة بفاتورة بتتعدّل من الفاتورة.
    """
    ft = FinancialTransaction.objects.select_related('treasury__branch').filter(pk=pk).first()
    if not ft:
        return redirect(f"{reverse('inventory:transactions_list')}?err=notfound")
    # الحركات المرتبطة بفاتورة/عميل/مورد بتتدار من مصدرها (عشان الرصيد يتظبط صح)
    if ft.sale_invoice_id or ft.purchase_invoice_id or ft.customer_id or ft.vendor_id:
        return redirect(f"{reverse('inventory:transactions_list')}?err=linked")
    if not _user_can_edit_branch(request.user, ft.treasury.branch):
        return redirect(f"{reverse('inventory:transactions_list')}?err=perm")
    back = request.POST.get('next') or reverse('inventory:transactions_list')
    desc = ft.description or ""
    with transaction.atomic():
        # لو تحويل: احذف الطرف التاني كمان (نفس مرجع التحويل)
        if desc.startswith(_TRANSFER_TAG):
            ref = desc[:desc.find(']') + 1]
            for leg in FinancialTransaction.objects.filter(description__startswith=ref):
                _delete_expense_ft(leg)
        else:
            _delete_expense_ft(ft)
    sep = '&' if '?' in back else '?'
    return redirect(f"{back}{sep}ok=deleted")


# =====================================================================
# 👥 كشف حساب العملاء (الآجل) + تحصيل الدفعات
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant', 'cashier')
def customers_receivables(request):
    """قائمة العملاء وأرصدتهم (الآجل) — مين عليه فلوس وكام، مع بحث وإجمالي."""
    qs = Customer.objects.all()
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    flt = (request.GET.get('filter') or 'debt').strip()
    if flt == 'debt':
        qs = qs.filter(balance__gt=0)
    elif flt == 'credit':
        qs = qs.filter(balance__lt=0)
    qs = qs.order_by('-balance', 'name')

    # إجمالي المديونية (كل العملاء اللي عليهم) — مش متأثر بالفلتر/البحث
    total_debt = Customer.objects.filter(balance__gt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')
    total_credit = Customer.objects.filter(balance__lt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')

    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    return render(request, 'inventory/customers_list.html', {
        'page': page, 'q': q, 'filter': flt,
        'total_debt': total_debt, 'total_credit': abs(total_credit),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant', 'cashier')
def customer_detail(request, pk):
    """🧾 كشف حساب عميل — فواتيره اللي عليها متبقي + دفعات التحصيل + رصيده."""
    customer = Customer.objects.filter(pk=pk).first()
    if not customer:
        return redirect(f"{reverse('inventory:customers_receivables')}?err=notfound")

    invoices = (SaleInvoice.objects.select_related('branch')
                .filter(customer=customer).exclude(status='quotation')
                .order_by('-date_created'))
    open_invoices = [inv for inv in invoices if inv.due_amount > Decimal('0.00')]

    # دفعات التحصيل = حركات إيداع مرتبطة بالعميل (سواء وقت الفاتورة أو تحصيل آجل)
    payments = (FinancialTransaction.objects.select_related('treasury')
                .filter(customer=customer, transaction_type='in')
                .order_by('-date', '-id')[:100])

    branch = _get_branch_for_user(request.user)
    treasuries = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)

    return render(request, 'inventory/customer_statement.html', {
        'customer': customer,
        'open_invoices': open_invoices,
        'invoices': invoices[:50],
        'payments': payments,
        'treasuries': treasuries.select_related('branch').order_by('branch__name', 'name'),
        'can_collect': _can_edit_invoices(request.user) or (
            getattr(getattr(request.user, 'employee_profile', None), 'role', '') == 'cashier'),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant', 'cashier')
@require_POST
def customer_collect(request, pk):
    """💵 تحصيل دفعة من العميل على حسابه (الآجل) — يقلّل رصيده ويدخل الفلوس الخزنة."""
    customer = Customer.objects.filter(pk=pk).first()
    if not customer:
        return redirect(f"{reverse('inventory:customers_receivables')}?err=notfound")
    treasury = Treasury.objects.filter(id=request.POST.get('treasury_id'), is_active=True).first()
    if treasury is None:
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=treasury")
    if not _user_can_edit_branch(request.user, treasury.branch):
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=perm")
    try:
        amount = Decimal(str(request.POST.get('amount') or '0'))
    except InvalidOperation:
        amount = Decimal('0')
    if amount <= 0:
        return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?err=amount")
    note = (request.POST.get('note') or '').strip()
    desc = f"تحصيل من العميل {customer.name}" + (f" — {note}" if note else "")
    with transaction.atomic():
        from django.db.models import F as _F
        # الحركة (in) بتزوّد الخزنة (signal) وبتسوّي الـ AR في الدفتر (post_payment)
        FinancialTransaction.objects.create(
            treasury=treasury, transaction_type='in', amount=amount,
            description=desc, customer=customer)
        # نقلّل مديونية العميل يدوياً (مفيش signal بيعملها)
        Customer.objects.filter(pk=customer.pk).update(balance=_F('balance') - amount)
    return redirect(f"{reverse('inventory:customer_detail', args=[pk])}?ok=collected")


# =====================================================================
# 🚚 الموردون (SRM) — كشف حساب المستحقات + سداد
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def vendors_payables(request):
    """قائمة الموردين وأرصدتهم (اللي علينا) — مع بحث وإجمالي المستحقات."""
    qs = Vendor.objects.all()
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    flt = (request.GET.get('filter') or 'debt').strip()
    if flt == 'debt':
        qs = qs.filter(balance__gt=0)
    elif flt == 'credit':
        qs = qs.filter(balance__lt=0)
    qs = qs.order_by('-balance', 'name')

    total_debt = Vendor.objects.filter(balance__gt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')
    total_credit = Vendor.objects.filter(balance__lt=0).aggregate(s=Sum('balance'))['s'] or Decimal('0')

    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    return render(request, 'inventory/vendors_list.html', {
        'page': page, 'q': q, 'filter': flt,
        'total_debt': total_debt, 'total_credit': abs(total_credit),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def vendor_detail(request, pk):
    """🧾 كشف حساب مورد — فواتير الشراء اللي عليها متبقّي + دفعاته + رصيده."""
    vendor = Vendor.objects.filter(pk=pk).first()
    if not vendor:
        return redirect(f"{reverse('inventory:vendors_payables')}?err=notfound")

    invoices = (PurchaseInvoice.objects.select_related('branch')
                .filter(vendor=vendor).order_by('-date_created'))
    open_invoices = [inv for inv in invoices
                     if (inv.total_amount - inv.paid_amount) > Decimal('0.00')]

    payments = (FinancialTransaction.objects.select_related('treasury')
                .filter(vendor=vendor, transaction_type='out')
                .order_by('-date', '-id')[:100])

    branch = _get_branch_for_user(request.user)
    treasuries = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)

    # نجهّز المتبقّي لكل فاتورة للعرض
    open_rows = [{"inv": inv, "due": inv.total_amount - inv.paid_amount} for inv in open_invoices]

    return render(request, 'inventory/vendor_statement.html', {
        'vendor': vendor,
        'open_rows': open_rows,
        'payments': payments,
        'treasuries': treasuries.select_related('branch').order_by('branch__name', 'name'),
        'can_pay': _can_edit_invoices(request.user),
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@require_POST
def vendor_pay(request, pk):
    """💸 سداد دفعة لمورد على حسابه — يقلّل مستحقاته ويطلع الفلوس من الخزنة."""
    vendor = Vendor.objects.filter(pk=pk).first()
    if not vendor:
        return redirect(f"{reverse('inventory:vendors_payables')}?err=notfound")
    treasury = Treasury.objects.filter(id=request.POST.get('treasury_id'), is_active=True).first()
    if treasury is None:
        return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?err=treasury")
    if not _user_can_edit_branch(request.user, treasury.branch):
        return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?err=perm")
    try:
        amount = Decimal(str(request.POST.get('amount') or '0'))
    except InvalidOperation:
        amount = Decimal('0')
    if amount <= 0:
        return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?err=amount")
    note = (request.POST.get('note') or '').strip()
    desc = f"سداد للمورد {vendor.name}" + (f" — {note}" if note else "")
    with transaction.atomic():
        from django.db.models import F as _F
        locked = Treasury.objects.select_for_update().get(pk=treasury.pk)
        if (locked.balance or Decimal('0')) < amount:
            return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?err=balance")
        # الحركة (out) بتقلّل الخزنة (signal) وبتسوّي الـ AP في الدفتر (post_payment)
        FinancialTransaction.objects.create(
            treasury=locked, transaction_type='out', amount=amount,
            description=desc, vendor=vendor)
        # نقلّل مستحقات المورد يدوياً (مفيش signal بيعملها)
        Vendor.objects.filter(pk=vendor.pk).update(balance=_F('balance') - amount)
    return redirect(f"{reverse('inventory:vendor_detail', args=[pk])}?ok=paid")


# =====================================================================
# 📈 تقرير الأرباح والخسائر (P&L)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def pnl_report(request):
    """قائمة الدخل: المبيعات − تكلفة البضاعة = مجمّل الربح، ناقص المصروفات = صافي الربح."""
    from django.utils import timezone as _tz
    now = _tz.now()
    period = request.GET.get('period', 'month')
    if period == 'today':
        start, label = now.replace(hour=0, minute=0, second=0, microsecond=0), "اليوم"
    elif period == 'all':
        start, label = None, "كل الفترات"
    elif period == 'year':
        start, label = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), "هذه السنة"
    else:
        period = 'month'
        start, label = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "هذا الشهر"

    branch = _get_branch_for_user(request.user)

    inv = SaleInvoice.objects.exclude(status='quotation')
    exp = (FinancialTransaction.objects
           .filter(transaction_type='out', sale_invoice__isnull=True,
                   purchase_invoice__isnull=True, vendor__isnull=True, customer__isnull=True)
           .exclude(description__startswith=_TRANSFER_TAG))
    if branch is not None:
        inv = inv.filter(branch=branch)
        exp = exp.filter(treasury__branch=branch)
    if start is not None:
        inv = inv.filter(date_created__gte=start)
        exp = exp.filter(date__gte=start)

    agg = inv.aggregate(
        sales_g=Sum('total_amount', filter=Q(is_return=False)),
        sales_r=Sum('total_amount', filter=Q(is_return=True)),
        cogs_g=Sum('total_cost', filter=Q(is_return=False)),
        cogs_r=Sum('total_cost', filter=Q(is_return=True)),
    )
    net_sales = (agg['sales_g'] or Decimal('0')) - (agg['sales_r'] or Decimal('0'))
    cogs = (agg['cogs_g'] or Decimal('0')) - (agg['cogs_r'] or Decimal('0'))
    gross = net_sales - cogs

    exp_rows = list(exp.values('category__name').annotate(t=Sum('amount')).order_by('-t'))
    total_exp = exp.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    net_profit = gross - total_exp
    margin = (net_profit / net_sales * Decimal('100')) if net_sales else Decimal('0')

    return render(request, 'inventory/pnl_report.html', {
        'branch': branch, 'period': period, 'label': label,
        'net_sales': net_sales, 'cogs': cogs, 'gross': gross,
        'exp_rows': exp_rows, 'total_exp': total_exp,
        'net_profit': net_profit, 'margin': margin,
        'invoices_count': inv.filter(is_return=False).count(),
        'returns_amount': (agg['sales_r'] or Decimal('0')),
    })


# =====================================================================
# ⚖️ ميزان المراجعة (Trial Balance) من دفتر الأستاذ
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def trial_balance(request):
    """ميزان المراجعة: مجاميع المدين/الدائن لكل حساب من القيود، وإجمالي متوازن."""
    from inventory.models import AccountingEntry, ChartOfAccount
    from django.utils import timezone as _tz
    now = _tz.now()
    period = request.GET.get('period', 'all')
    if period == 'month':
        start, label = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "هذا الشهر"
    elif period == 'year':
        start, label = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), "هذه السنة"
    else:
        period, start, label = 'all', None, "كل الفترات (تراكمي)"

    qs = AccountingEntry.objects.all()
    if start is not None:
        qs = qs.filter(entry_date__gte=start)

    agg = (qs.values('account_id', 'account__code', 'account__name', 'account__account_type')
           .annotate(d=Sum('debit'), c=Sum('credit'))
           .order_by('account__code'))

    type_label = dict(ChartOfAccount.ACCOUNT_TYPES)
    rows, tot_d, tot_c = [], Decimal('0'), Decimal('0')
    for r in agg:
        d = r['d'] or Decimal('0')
        c = r['c'] or Decimal('0')
        if d == 0 and c == 0:
            continue
        atype = r['account__account_type']
        net = (d - c) if atype in ('asset', 'expense') else (c - d)
        rows.append({
            'code': r['account__code'], 'name': r['account__name'],
            'type': atype, 'type_label': type_label.get(atype, atype),
            'debit': d, 'credit': c, 'net': net,
            'net_side': 'debit' if atype in ('asset', 'expense') else 'credit',
        })
        tot_d += d
        tot_c += c

    return render(request, 'inventory/trial_balance.html', {
        'rows': rows, 'total_debit': tot_d, 'total_credit': tot_c,
        'balanced': (tot_d == tot_c), 'diff': (tot_d - tot_c),
        'period': period, 'label': label,
    })


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
    import re as _re
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
        try:
            return Decimal(str(v).strip() or '0')
        except Exception:
            return Decimal('0')

    def to_int(v):
        try:
            return int(float(str(v).strip() or '0'))
        except Exception:
            return 0

    created = updated = stock_set = 0
    errors = []

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
            defaults = {
                'name': name,
                'brand': str(cell(row, 'brand')).strip() or 'BMW',
                'car_model': str(cell(row, 'car_model')).strip() or '—',
                'purchase_price': to_dec(cell(row, 'purchase_price')),
                'retail_price': to_dec(cell(row, 'retail_price')),
            }
            msl = cell(row, 'min_stock_level')
            if str(msl).strip():
                defaults['min_stock_level'] = to_int(msl)
            try:
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
                    # صنف جديد — نضمن part_number مميّز (نزوّد لاحقة لو الكود متكرر)
                    base = (sku or (("BC-" + barcode) if barcode else "AUTO"))[:90]
                    pn = base
                    _k = 2
                    while Product.objects.filter(part_number=pn).exists():
                        pn = f"{base}-{_k}"
                        _k += 1
                    prod = Product.objects.create(
                        part_number=pn, barcode=(barcode or None), **defaults)
                    created += 1

                # كمية الفرع النشط
                qraw = cell(row, 'quantity')
                if str(qraw).strip() != '':
                    qty = to_int(qraw)
                    inv, _created = Inventory.objects.get_or_create(
                        product=prod, branch=branch, defaults={'quantity': qty})
                    Inventory.objects.filter(pk=inv.pk).update(quantity=qty)
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
