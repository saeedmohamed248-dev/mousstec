"""📦 API الروبوت — استلام البضاعة، الجرد، التعليم، الكشك، إشارات الشراء."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.response import Response

from .. import services
from ..models import (
    ProcurementSignal,
)
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from ._common import _kiosk_lookup_endpoint, _robot_endpoint


@_robot_endpoint
def intake(request):
    """Goods intake: photograph a part, register it, add stock.

    "صوّر الكنترول ده واعمله خلفية بيضا وسجّل البارت نمبر وقوله اسمه، ودوّر عليه
    في المخزون — لو موجود زوّد الكمية، لو مش موجود ضيف بند جديد بصورته وسعره."

    Requires a face-authorized employee (it writes stock + prices). Body:
    `image` (file, optional), `name`, `part_number` (optional — read from photo),
    `retail_price`, `quantity`, `car_model`, `part_category`, face auth.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    employee, perr = views._require_permission(request, device, "intake")
    if perr:
        return perr

    image = request.FILES.get("image")
    image_bytes = image.read() if image else None

    # Intake writes stock and prices, so bad numbers are a 400, not a 500 —
    # and a negative quantity would silently *remove* stock.
    try:
        quantity = int(request.data.get("quantity", 1) or 1)
    except (TypeError, ValueError):
        return Response({"detail": "الكمية غير صالحة."}, status=status.HTTP_400_BAD_REQUEST)
    if quantity < 1:
        return Response({"detail": "الكمية لازم تكون ١ أو أكتر."},
                        status=status.HTTP_400_BAD_REQUEST)
    retail_price = request.data.get("retail_price")
    if retail_price not in (None, ""):
        try:
            retail_price = Decimal(str(retail_price))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "السعر غير صالح."}, status=status.HTTP_400_BAD_REQUEST)
        if retail_price < 0:
            return Response({"detail": "السعر لازم يكون موجب."},
                            status=status.HTTP_400_BAD_REQUEST)

    result = services.intake_part(
        device=device, branch=device.branch, image_bytes=image_bytes,
        name=(request.data.get("name") or "").strip(),
        part_number=(request.data.get("part_number") or "").strip(),
        retail_price=retail_price if retail_price not in (None, "") else None,
        quantity=quantity,
        car_model=(request.data.get("car_model") or "").strip(),
        part_category=(request.data.get("part_category") or "").strip(),
        employee=employee,
    )
    return Response(result, status=status.HTTP_201_CREATED)


@_robot_endpoint
def stock_take(request):
    """Physical inventory count (جرد). Requires a face-authorized employee.

    Body: `counts` = [{"query": "<code/name>", "counted_qty": N}, ...],
    optional `instruction`. Returns a reconciled report; variances are approved
    from the dashboard (no silent stock change).
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    employee, perr = views._require_permission(request, device, "stock_take")
    if perr:
        return perr
    result = services.run_stock_take(
        device=device, branch=device.branch,
        counts=request.data.get("counts") or [],
        instruction=(request.data.get("instruction") or "").strip(),
        employee=employee,
    )
    return Response(result, status=status.HTTP_201_CREATED)


@_robot_endpoint
def stock_take_apply(request):
    """Apply a stock-take's variances to inventory. Requires face auth.

    Body: `session_id`. Sets each variance line's branch on-hand to the counted
    quantity (recording adjustment movements). Idempotent.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    employee, perr = views._require_permission(request, device, "stock_take_apply")
    if perr:
        return perr
    from ..models import RobotStockTakeSession
    session = RobotStockTakeSession.objects.filter(
        pk=request.data.get("session_id"), device=device,
    ).first()
    if session is None:
        return Response({"detail": "جلسة الجرد غير موجودة."},
                        status=status.HTTP_404_NOT_FOUND)
    result = services.apply_stock_take(session, employee=employee)
    return Response(result)


@_robot_endpoint
def teach(request):
    """Teach the robot — the human half of its learning loop. Requires face auth.

    Body (one of):
      * `scan_id` + `part_number`: "this scan was really THAT part" — weakens
        the wrong guess, learns the right code/label/look, fixes the scan.
      * `alias` + `part_number`: "when someone says <alias>, they mean THAT
        part" — shop slang the voice assistant then understands in sentences.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    employee, perr = views._require_permission(request, device, "teach")
    if perr:
        return perr

    product = services.find_product((request.data.get("part_number") or "").strip())
    if product is None:
        return Response({"detail": "القطعة غير موجودة."}, status=status.HTTP_404_NOT_FOUND)

    scan_id = request.data.get("scan_id")
    if scan_id:
        ev = views.RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
        if ev is None:
            return Response({"detail": "المسح غير موجود."}, status=status.HTTP_404_NOT_FOUND)
        result = services.teach_scan_correction(ev, product=product, employee=employee)
        result["reply"] = f"تمام، اتعلمت إن القطعة دي {product.name}."
        return Response(result)

    alias = (request.data.get("alias") or "").strip()
    if len(alias) < 3:
        return Response({"detail": "ابعت scan_id أو alias (٣ حروف على الأقل)."},
                        status=status.HTTP_400_BAD_REQUEST)
    services.learn_from_confirmation(
        product=product, label=alias, employee=employee, details={"source": "teach"},
    )
    return Response({
        "ok": True, "alias": alias, "product_id": product.id, "name": product.name,
        "reply": f"تمام، اتعلمت إن «{alias}» يعني {product.name}.",
    })


@_robot_endpoint
def kiosk_part(request):
    """Kiosk: stock + RETAIL price + shelf for a part (`q`)."""
    device, err = views._device_or_401(request)
    if err:
        return err
    from .. import kiosk
    q = request.query_params.get("q") or request.data.get("q") or ""
    return Response(kiosk.part_info(q, device.branch))


@_kiosk_lookup_endpoint
def kiosk_customer(request):
    """Kiosk: first name + loyalty for a phone (no balance, no history)."""
    device, err = views._device_or_401(request)
    if err:
        return err
    from .. import kiosk
    phone = request.query_params.get("phone") or request.data.get("phone") or ""
    return Response(kiosk.customer_brief(phone))


@_kiosk_lookup_endpoint
def kiosk_return_check(request):
    """Kiosk: return/warranty eligibility by `invoice_number` or `phone`."""
    device, err = views._device_or_401(request)
    if err:
        return err
    from .. import kiosk
    get = lambda k: request.query_params.get(k) or request.data.get(k) or ""  # noqa: E731
    return Response(kiosk.return_check(get("invoice_number"), get("phone")))


@_robot_endpoint
def procurement_signals(request):
    """Open low-stock signals for the Procurement Agent to build the manifest."""
    device, err = views._device_or_401(request)
    if err:
        return err
    signals = ProcurementSignal.objects.filter(
        branch=device.branch, status="open",
    ).select_related("product")[:100]
    return Response({
        "signals": [
            {
                "id": s.id,
                "product": s.product.name,
                "part_number": s.product.part_number,
                "on_hand": s.quantity_on_hand,
                "suggested_qty": s.suggested_reorder_qty,
                "manifest": s.manifest_payload,
            }
            for s in signals
        ]
    })
