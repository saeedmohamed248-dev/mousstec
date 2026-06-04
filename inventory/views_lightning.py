"""
Lightning POS + Quick Product Entry — competitor-grade fast workflows.

Two surfaces:
  1. /lightning-pos/  → walk-in retail invoicing (no vehicle, no maintenance fields)
  2. /quick-product/  → 6-field product creation + starting stock in one POST

Both write through the existing SaleInvoice / Product / Inventory / InventoryMovement
models — no schema changes, no new tables.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models import (
    Branch, Customer, Inventory, InventoryMovement, Product,
    SaleInvoice, SaleInvoiceItem, SaleInvoiceServiceItem,
    ServiceCatalog, Vehicle, VehicleInspection,
)
from .views import (
    _get_branch_for_user, _json_response_safe, tenant_required,
)

WALK_IN_PHONE = "0000000000"
WALK_IN_NAME = "عميل نقدي (Walk-in)"


def _walk_in_customer():
    """Single shared walk-in row — phone is unique on Customer."""
    cust, _ = Customer.objects.get_or_create(
        phone=WALK_IN_PHONE,
        defaults={"name": WALK_IN_NAME},
    )
    return cust


def _resolve_customer(name, phone):
    """Find-or-create by phone (the unique natural key). Blank phone → walk-in."""
    phone = (phone or "").strip()
    name = (name or "").strip()
    if not phone:
        return _walk_in_customer()
    cust, created = Customer.objects.get_or_create(
        phone=phone,
        defaults={"name": name or f"عميل {phone}"},
    )
    if not created and name and cust.name != name and cust.name == WALK_IN_NAME:
        cust.name = name
        cust.save(update_fields=["name"])
    return cust


# =====================================================================
# 1. LIGHTNING POS
# =====================================================================

@login_required(login_url='/secure-portal/')
@tenant_required
def lightning_pos(request):
    branch = _get_branch_for_user(request.user)
    return render(request, "inventory/lightning_pos.html", {
        "branch": branch,
    })


@login_required(login_url='/secure-portal/')
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
    qs = Product.objects.filter(is_active=True).filter(
        Q(part_number__iexact=q)
        | Q(barcode=q)
        | Q(part_number__icontains=q)
        | Q(name__icontains=q)
    ).distinct()[:12]

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


@login_required(login_url='/secure-portal/')
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

                inv = (Inventory.objects
                       .select_for_update()
                       .filter(product_id=pid, branch=branch)
                       .first())
                if inv is None or inv.quantity < qty:
                    available = inv.quantity if inv else 0
                    return _json_response_safe({
                        "error": f"المخزون غير كافٍ للقطعة #{pid} (متاح: {available}, مطلوب: {qty})."
                    }, status=409)
                line_specs.append((inv, pid, qty, price))

            invoice = SaleInvoice.objects.create(
                invoice_type="sale",
                status="posted",
                customer=customer,
                branch=branch,
                paid_amount=Decimal("0.00"),
            )

            for inv, pid, qty, price in line_specs:
                product = inv.product
                SaleInvoiceItem.objects.create(
                    invoice=invoice,
                    product=product,
                    quantity=qty,
                    unit_price=price,
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

        return _json_response_safe({
            "ok": True,
            "invoice_id": invoice.id,
            "total": float(invoice.total_amount),
            "print_url": reverse("inventory:print_invoice_thermal", args=[invoice.id]),
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل إتمام الفاتورة: {exc}"}, status=500)


# =====================================================================
# 2. QUICK PRODUCT ENTRY
# =====================================================================

@login_required(login_url='/secure-portal/')
@tenant_required
def quick_product_entry(request):
    branch = _get_branch_for_user(request.user)
    branches = Branch.objects.all() if branch is None else None
    return render(request, "inventory/quick_product.html", {
        "branch": branch,
        "branches": branches,
    })


@login_required(login_url='/secure-portal/')
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


@login_required(login_url='/secure-portal/')
@tenant_required
def job_card_create(request):
    branch = _get_branch_for_user(request.user)
    return render(request, "inventory/job_card_create.html", {
        "branch": branch,
        "services": ServiceCatalog.objects.all().order_by("name"),
    })


@login_required(login_url='/secure-portal/')
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


@login_required(login_url='/secure-portal/')
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
            invoice = SaleInvoice.objects.create(
                invoice_type="maintenance" if services else "sale",
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

        return _json_response_safe({
            "ok": True,
            "invoice_id": invoice.id,
            "total": float(invoice.total_amount),
            "print_url": reverse("inventory:print_invoice_a4", args=[invoice.id]),
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ أمر الشغل: {exc}"}, status=500)
