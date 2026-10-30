"""🔧 كارت الصيانة (أمر شغل): عميل + عربية + قطع + خدمات + فحص (DVI).

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from inventory.models import (
    Branch,
    Customer,
    Inventory,
    InventoryMovement,
    SaleInvoice,
    SaleInvoiceItem,
    SaleInvoiceServiceItem,
    ServiceCatalog,
    Treasury,
    Vehicle,
    VehicleInspection,
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
    _ar_field_expr,
    _finite_dec,
    _is_walk_in,
    _named_walk_in,
    _norm_ar,
    _normalize_tenders,
    _post_sale_to_ledger,
    _record_invoice_payments,
    _resolve_customer,
)


# =====================================================================
# 3. JOB CARD (Repair Order) — Customer + Vehicle + Parts + Services + DVI
# =====================================================================
DVI_FIELDS = ("brakes_status", "engine_oil_status", "tires_status", "battery_status")


@login_required(login_url='/login/')
@tenant_required
@module_required('jobcard')
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
    """Suggest customers for the POS/Job Card panel — match by name or phone.

    🔎 بحث عربي مُطبَّع: بنوحّد الهمزات/التاء المربوطة/الألف المقصورة في الاسم
    المخزّن وفي نص البحث سوا (نفس منطق بحث المنتجات)، عشان "محمد" تلاقي
    "محمّد"/"احمد" تلاقي "أحمد" مهما اختلفت صيغة الحروف في الداتا المستوردة —
    ده كان سبب إن اسم العميل "مش بيظهر" رغم إنه موجود.
    """
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return _json_response_safe({"results": []})
    qn = _norm_ar(q)
    qs = (
        Customer.objects
        .annotate(_nname=_ar_field_expr('name'))
        .filter(Q(_nname__contains=qn) | Q(name__icontains=q) | Q(phone__icontains=q))
        .order_by('name')[:10]
    )
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

    def _dec(key, default="0"):
        try:
            return Decimal(str(payload.get(key) or default))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal(default)

    try:
        with transaction.atomic():
            # 🐛 [FIX]: الأخطاء هنا بقت بترفع _RollbackWith بدل return — الـ return
            #    من جوه atomic كان بيعمل commit للعميل/العربية الجديدة حتى لو
            #    الأمر اترفض بعدها (مثلاً «المخزون غير كافٍ»).
            # --- Customer ----------------------------------------------------
            cust_id = payload.get("customer_id")
            if cust_id:
                customer = Customer.objects.filter(id=cust_id).first()
                if customer is None:
                    raise _RollbackWith(_json_response_safe({"error": "العميل المحدد غير موجود."}, status=400))
            else:
                customer = _resolve_customer(payload.get("customer_name"), payload.get("customer_phone"))
                customer, walk_in_note = _named_walk_in(customer, payload.get("customer_name"))
                if walk_in_note:
                    payload["notes"] = (walk_in_note + " — " + (payload.get("notes") or "")).strip(" —")

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
            stock_rows, wanted = {}, {}   # صف واحد للصنف حتى لو على أكتر من سطر (زي نقطة البيع)
            for raw in items:
                try:
                    pid = int(raw.get("product_id"))
                    qty = int(raw.get("qty") or 0)
                except (TypeError, ValueError):
                    raise _RollbackWith(_json_response_safe({"error": "قطعة أو كمية غير صالحة."}, status=400))
                if qty <= 0:
                    raise _RollbackWith(_json_response_safe({"error": "كمية القطعة غير صالحة."}, status=400))
                price = _finite_dec(raw.get("price"))
                if price is None:
                    raise _RollbackWith(_json_response_safe({"error": "سعر غير صالح."}, status=400))
                if price < 0:
                    raise _RollbackWith(_json_response_safe({"error": "سعر القطعة مينفعش يبقى بالسالب."}, status=400))
                inv = stock_rows.get(pid)
                if inv is None:
                    inv = (Inventory.objects.select_for_update()
                           .filter(product_id=pid, branch=branch).first())
                    if inv is not None:
                        stock_rows[pid] = inv
                wanted[pid] = wanted.get(pid, 0) + qty
                if inv is None or inv.quantity < wanted[pid]:
                    available = inv.quantity if inv else 0
                    raise _RollbackWith(_json_response_safe({
                        "error": f"المخزون غير كافٍ للقطعة #{pid} (متاح: {available}, مطلوب: {wanted[pid]})."
                    }, status=409))
                line_specs.append((inv, qty, price))

            # --- Services: validate before writing ---------------------------
            svc_specs = []
            for svc in services:
                service = ServiceCatalog.objects.filter(id=int(svc.get("service_id"))).first()
                if service is None:
                    continue
                price = svc.get("price")
                if price in (None, ""):
                    svc_price = None
                else:
                    try:
                        svc_price = Decimal(str(price))
                    except (InvalidOperation, TypeError):
                        raise _RollbackWith(_json_response_safe({"error": "سعر خدمة غير صالح."}, status=400))
                    if svc_price < 0:
                        raise _RollbackWith(_json_response_safe({"error": "سعر الخدمة مينفعش يبقى بالسالب."}, status=400))
                svc_specs.append((service, svc_price))

            # --- Discount / labor / tax: حصر القيم -------------------------
            # 🐛 [FIX]: الخصم بالسالب كان بيزوّد الإجمالي، والخصم الأكبر من
            #    الفاتورة كان بيطلّعها بالسالب، والضريبة كانت بتقبل أي رقم.
            labor = max(_dec("labor_cost_manual"), Decimal("0"))
            tax_pct = min(max(_dec("tax_percentage"), Decimal("0")), Decimal("100"))
            parts_total = sum((Decimal(str(q)) * p for _, q, p in line_specs), Decimal("0"))
            # سعر فاضي/صفر بيتملى من الكتالوج وقت الحفظ (SaleInvoiceServiceItem.save)
            svc_total = sum((sp if sp else Decimal(str(s.labor_price or 0))
                             for s, sp in svc_specs), Decimal("0"))
            gross = parts_total + svc_total + labor
            discount = min(max(_dec("discount"), Decimal("0")), gross)

            # 🔒 حد خصم الموظف — نفس فحص نقطة البيع (كان أمر الشغل طريق
            #    لتخطّيه). نزول سعر القطعة عن سعر البيع بيتحسب خصم برضه.
            price_cut = Decimal("0")
            base_total = svc_total + labor
            for inv_row, q, p in line_specs:
                retail = Decimal(str(inv_row.product.retail_price or 0))
                price_cut += Decimal(str(q)) * max(retail - p, Decimal("0"))
                base_total += Decimal(str(q)) * max(retail, p)
            effective_discount = discount + price_cut
            if effective_discount > 0 and base_total > 0 and not request.user.is_superuser:
                profile = getattr(request.user, "employee_profile", None)
                if profile:
                    disc_pct = (effective_discount / base_total) * Decimal("100")
                    if not profile.can_apply_discount(disc_pct):
                        raise _RollbackWith(_json_response_safe({
                            "error": (f"الخصم ({disc_pct:.1f}%) يتجاوز الحد المسموح لك "
                                      f"({profile.effective_max_discount:.0f}%). راجع المدير.")
                        }, status=403))

            # --- Create the invoice header ----------------------------------
            try:
                mileage = int(payload.get("mileage")) if payload.get("mileage") else None
            except (TypeError, ValueError):
                mileage = None
            requested_type = payload.get("invoice_type")
            invoice_type = requested_type if requested_type in ("sale", "maintenance") else ("maintenance" if services else "sale")
            if invoice_type == "maintenance":
                # 📏 حد كروت الصيانة الشهري في الباقة (Silver = 150) — كان مكتوب ومش متطبّق
                from tenancy.services.plan_limits import repair_cards_block
                blocked = repair_cards_block()
                if blocked:
                    raise _RollbackWith(_json_response_safe(
                        {"error": blocked, "code": "plan_limit", "upgrade_url": "/subscription/manage/"},
                        status=402))
            invoice = SaleInvoice.objects.create(
                invoice_type=invoice_type,
                status="in_progress",
                customer=customer,
                vehicle=vehicle,
                branch=branch,
                mileage=mileage,
                notes=(payload.get("notes") or "").strip() or None,
                labor_cost_manual=labor,
                discount=discount,
                tax_percentage=tax_pct,
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
                    note=f"أمر شغل #{invoice.id}", created_by=request.user,
                )

            # --- Services ----------------------------------------------------
            for service, svc_price in svc_specs:
                SaleInvoiceServiceItem.objects.create(
                    invoice=invoice, service=service, price=svc_price)

            # --- DVI (صيانة + عربية بس) -------------------------------------
            # 🐛 [FIX]: الشاشة بتبعت الفحص دايماً (افتراضي «ممتاز»)، فكان بيتسجّل
            #    فحص وهمي حتى في «بيع قطع» — دلوقتي للصيانة بس، وبقيم صالحة.
            dvi = payload.get("dvi") or {}
            dvi_ok = {"green", "yellow", "red"}
            if vehicle and invoice_type == "maintenance" and any(dvi.get(k) for k in DVI_FIELDS):
                VehicleInspection.objects.create(
                    invoice=invoice, vehicle=vehicle,
                    **{k: (dvi.get(k) if dvi.get(k) in dvi_ok else "green") for k in DVI_FIELDS},
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
            if due > Decimal("0.00") and _is_walk_in(customer):
                raise _RollbackWith(_json_response_safe({"error": _WALK_IN_CREDIT_ERROR}, status=400))
            if due > Decimal("0.00"):
                from django.db.models import F as _F
                Customer.objects.filter(pk=customer.pk).update(balance=_F("balance") + due)

            # 🛡️ علّمنا الفاتورة كـ «مُطبّقة» عشان لو اتحوّلت لـ posted لاحقاً
            # (كشك/أدمن) ما يشتغلش execute_sale تاني فيخصم المخزون ويقيّد الآجل
            # مرة تانية (double-post). كل الأثر المالي والمخزوني اتسجّل يدوياً هنا.
            SaleInvoice.objects.filter(pk=invoice.pk).update(is_applied=True)
            # 🐛 [FIX]: «فاتورة قطع غيار فقط» بيع خلصان — مش شغل في الورشة. كانت
            #    بتفضل «قيد التنفيذ» للأبد (مفيش زرار يقفلها غير لوحة الأدمن)،
            #    فمينفعش يتعمل لها مرتجع وماكانتش بتظهر في كشف الحساب المطبوع.
            #    save بعد is_applied: execute_sale مابيشتغلش تاني، بس إشارة
            #    «اتسلّمت» بتشتغل (عمولات/تقييم العميل) زي التسليم العادي.
            if invoice_type == "sale":
                invoice.refresh_from_db()
                invoice.status = "posted"
                invoice.save(update_fields=["status"])

        return _json_response_safe({
            "ok": True,
            "invoice_id": invoice.id,
            "total": float(invoice.total_amount),
            "paid": float(invoice.paid_amount),
            "due": float(invoice.due_amount),
            "print_url": reverse("inventory:print_invoice_a4", args=[invoice.id]),
        })
    except _RollbackWith as rb:
        return rb.response
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل حفظ أمر الشغل: {exc}"}, status=500)
