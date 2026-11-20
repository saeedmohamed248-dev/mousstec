"""🧾 API الروبوت — البيع من الروبوت (بوش موظف مصرّح)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.response import Response

from .. import customers as customers_svc
from .. import entries, services
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from ._common import _robot_endpoint


@_robot_endpoint
def sale(request):
    """Create a retail sale from a scan. Requires a face-authorized employee.

    Body: `scan_id` OR `part_number`, `customer_name`, `customer_phone`,
    `payment` (cash|credit), `branch_id`, `face_embedding` (or `employee_id`),
    optional `quantity`, `unit_price` (retail only). Security gate per
    requirements. Without `branch_id` nothing is created: the 428 reply holds
    the question «أسجلها على أنهي فرع؟» and the branch list.
    """
    device, err = views._device_or_401(request)
    if err:
        return err

    employee, perr = views._require_permission(request, device, "sale")
    if perr:
        return perr

    # Resolve product from scan or part number.
    product = None
    scan_id = request.data.get("scan_id")
    if scan_id:
        ev = views.RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
        product = ev.product if ev else None
    if product is None:
        product = services.find_product(request.data.get("part_number", ""))
    if product is None:
        return Response({"detail": "القطعة غير موجودة."}, status=status.HTTP_404_NOT_FOUND)

    # Resolve / create the customer by phone (phone is unique in CRM).
    from inventory.models import Customer
    phone = (request.data.get("customer_phone") or "").strip()
    name = (request.data.get("customer_name") or "عميل نقدي").strip()
    customer = None
    if phone:
        customer, _c = Customer.get_or_create_by_phone(phone, defaults={"name": name})
    else:
        customer, _c = Customer.get_or_create_by_phone(
            "0000000000", defaults={"name": "عميل نقدي"},
        )

    # Quantity and price come off the wire, so neither is trusted. A bad value
    # here is a wrong invoice, not a 500: reject it with a reason.
    try:
        quantity = int(request.data.get("quantity", 1) or 1)
    except (TypeError, ValueError):
        return Response(
            {"detail": "الكمية غير صالحة."}, status=status.HTTP_400_BAD_REQUEST
        )
    if quantity < 1:
        return Response(
            {"detail": "الكمية لازم تكون ١ أو أكتر."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Which branch is this invoice for? Never assumed: without `branch_id` the
    # reply is the question to ask and the branches to pick from (428), and
    # the device posts again with the answer.
    branch_id = request.data.get("branch_id")
    if branch_id in (None, ""):
        return Response(entries.branch_choices(device),
                        status=status.HTTP_428_PRECONDITION_REQUIRED)
    branch = entries.branch_by_id(branch_id)
    if branch is None:
        return Response({"detail": "الفرع غير موجود."}, status=status.HTTP_400_BAD_REQUEST)

    # A cash sale needs somewhere to put the cash. Without a cash treasury the
    # invoice would post as if unpaid while the customer has paid.
    payment = request.data.get("payment", "cash")
    if services.is_cash_payment(payment) and entries.cash_treasury(branch) is None:
        return Response(
            {"detail": f"فرع {branch.name} مالوش خزنة كاش — ضيف خزنة كاش للفرع "
                       "أو سجّل البيع آجل."},
            status=status.HTTP_409_CONFLICT,
        )
    # Same rule as the POS: credit with no phone lands the debt on the shared
    # walk-in customer, and nobody knows who owes it.
    if not phone and not services.is_cash_payment(payment):
        return Response(
            {"detail": "البيع الآجل محتاج رقم موبايل العميل — من غيره المديونية "
                       "بتتسجّل على «عميل نقدي» ومحدش يعرف مين عليه الفلوس."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Don't sell what the branch doesn't have. Staff can still take an order
    # for a part on its way by saying so explicitly (`allow_backorder`).
    backorder = str(request.data.get("allow_backorder", "")).lower() in ("1", "true", "yes")
    on_hand = services.branch_stock(product, branch)
    if quantity > on_hand and not backorder:
        return Response(
            {"detail": f"المتاح في فرع {branch.name} {on_hand} بس من {product.name}.",
             "on_hand": on_hand},
            status=status.HTTP_409_CONFLICT,
        )

    unit_price = request.data.get("unit_price")
    if unit_price in (None, ""):
        unit_price = None  # services.create_robot_sale falls back to retail
    else:
        try:
            unit_price = Decimal(str(unit_price))
        except (InvalidOperation, TypeError, ValueError):
            return Response(
                {"detail": "السعر غير صالح."}, status=status.HTTP_400_BAD_REQUEST
            )
        if unit_price <= 0:
            return Response(
                {"detail": "السعر لازم يكون أكبر من صفر."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Price floor: a device-supplied price may not go below the part's scrap
        # price (the retail-side floor for a used part). This closes the "sell at
        # 1 EGP" hole the PR flagged, while still allowing a used-part discount
        # down to scrap. Parts with no scrap price set impose no floor here —
        # that stays a per-shop business decision, not a silent hardcode.
        floor = Decimal(str(getattr(product, "scrap_price", 0) or 0))
        if floor > 0 and unit_price < floor:
            return Response(
                {"detail": f"السعر لا يقل عن سعر الخردة ({floor:.0f} ج.م)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    from django.core.exceptions import ValidationError as _DjangoValidationError
    is_backorder = quantity > on_hand
    try:
        invoice = services.create_robot_sale(
            product=product, branch=branch, customer=customer,
            employee=employee, quantity=quantity, unit_price=unit_price,
            payment=payment, post=not is_backorder,
        )
    except _DjangoValidationError as exc:   # e.g. stock moved between check and post
        return Response({"detail": "؛ ".join(exc.messages)}, status=status.HTTP_409_CONFLICT)

    # A name given without a phone used to be dropped: the sale showed as
    # «عميل نقدي» in the invoices list. Keep it on the invoice, like the POS.
    typed_name = (request.data.get("customer_name") or "").strip()
    if not phone and typed_name and typed_name != "عميل نقدي":
        from inventory.models import SaleInvoice
        invoice.set_walk_in_name(typed_name)
        SaleInvoice.objects.filter(pk=invoice.pk).update(notes=invoice.notes)
    entries.log_sale(device=device, branch=branch, invoice=invoice, employee=employee,
                     summary=(f"أوردر {quantity} {product.name} (مش متوفر)" if is_backorder
                              else f"بيع {quantity} {product.name}"))

    # Link the scan to the invoice for the audit trail, and LEARN from the
    # confirmed match (scanned code/label → this product) so recognition of the
    # same part gets faster and more confident next time.
    if scan_id:
        ev = views.RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
        if ev:
            ev.sale_invoice = invoice
            ev.created_by = employee
            ev.save(update_fields=["sale_invoice", "created_by"])
            services.learn_from_confirmation(
                product=product,
                code=ev.recognized_part_number or "",
                label=ev.recognized_label or "",
                fingerprint_hash=services.scan_fingerprint(ev),
                employee=employee,
            )

    # Remember what this customer buys (car model / category) for next visit.
    try:
        customers_svc.learn_from_purchase(customer, product, quantity=quantity)
    except Exception:
        pass  # a memory hiccup must never undo a posted sale

    # Low-stock check after the sale deducted stock.
    services.maybe_raise_procurement_signal(
        device=device, product=product, branch=branch,
    )

    return Response({
        "ok": True,
        "invoice_id": invoice.id,
        "branch": branch.name,
        "total_amount": float(invoice.total_amount),
        "status": invoice.status,
        "backorder": is_backorder,
        "authorized_by": employee.name,
    }, status=status.HTTP_201_CREATED)
