"""
robot/views.py — REST API the ESP32 firmware calls.

Auth model: the ESP32 authenticates as a *device* via `X-Robot-Token`
(`robot.security.authenticate_device`), not as a Django user — the robot stands
on the shop floor, not behind a login. Privileged actions (creating a sale,
dispensing a part) additionally require a face-authorized employee in the same
request, enforced here.

Every product/price response is built by `robot.pricing.safe_product_payload`,
so wholesale/cost can never reach the device.

Endpoints (prefix /api/robot/v1/ — see urls.py):
  POST heartbeat/            device liveness + firmware version
  POST scan/                 ESP32-CAM part image → identify → stock/price (or scrap price)
  POST voice/                transcript → intent → spoken reply (workshop assistant + POS)
  POST face/                 face embedding → attendance clock-in/out + authorize session
  POST sale/                 create a retail sale from a scan (requires face auth)
  POST motor/                queue a physical-articulation command (requires face auth)
  GET  motor/pending/        commands the ESP32 should execute + ack
  GET  procurement-signals/  open low-stock signals for the Procurement Agent
"""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from . import services, security, vision
from .models import (
    MotorCommandLog, ProcurementSignal, RobotAccessLog, RobotDevice,
    RobotScanEvent, RobotVoiceInteraction,
)
from .pricing import safe_product_payload

# Confidence below which vision must defer to the printed barcode.
_MIN_VISION_CONFIDENCE = 0.75


def _device_or_401(request):
    """Resolve the device from its token, or return a 401 Response."""
    device = security.authenticate_device(request)
    if device is None:
        return None, Response(
            {"detail": "invalid or missing X-Robot-Token"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return device, None


def _authorized_employee(request, device):
    """Return the face-authorized employee for this request, or None.

    The device sends the current face embedding (or an employee id it already
    resolved this session) with privileged calls. Security gate for sales /
    dispensing per the requirements.
    """
    embedding = request.data.get("face_embedding")
    if embedding:
        emp, score = security.identify_employee(embedding, branch=device.branch)
        return emp
    # Allow an already-authorized employee id echoed back within the session.
    emp_id = request.data.get("employee_id")
    if emp_id:
        from hr.models import Employee
        return Employee.objects.filter(pk=emp_id, user__is_active=True).first()
    return None


# All robot endpoints authenticate by device token, not Django session/JWT.
def _robot_endpoint(view):
    return api_view(["GET", "POST"])(
        authentication_classes([])(permission_classes([])(view))
    )


@_robot_endpoint
def heartbeat(request):
    device, err = _device_or_401(request)
    if err:
        return err
    fw = request.data.get("firmware_version")
    if fw:
        device.firmware_version = str(fw)[:20]
        device.save(update_fields=["firmware_version"])
    return Response({
        "ok": True,
        "device": device.name,
        "branch": device.branch.name,
        "server_time": timezone.now().isoformat(),
    })


@_robot_endpoint
def scan(request):
    """ESP32-CAM part capture → identify → stock+retail price (or scrap price).

    Body: image (file) or `code` (barcode text the cam decoded), `purpose`
    (pos|scrap|lookup). For confident matches returns a retail-only payload; for
    the scrap flow adds a condition-based suggested retail price. Raises a
    procurement signal if stock is critically low.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    purpose = request.data.get("purpose", "pos")
    code = (request.data.get("code") or "").strip()
    image = request.FILES.get("image")

    label, part_number, confidence = "", "", 0.0
    product = None

    # Prefer a decoded barcode (exact); else run vision on the image.
    if code:
        product = services.find_product(code)
        confidence = 1.0 if product else 0.0
        part_number = code
    elif image:
        image_bytes = image.read()
        label, part_number, confidence = vision.identify_part(image_bytes)
        if confidence >= _MIN_VISION_CONFIDENCE and part_number:
            product = services.find_product(part_number)

    event = RobotScanEvent.objects.create(
        device=device, purpose=purpose, image=image,
        recognized_label=label, recognized_part_number=part_number,
        confidence=confidence, product=product,
    )

    if not product:
        return Response({
            "found": False,
            "scan_id": event.id,
            "message": "لم أتعرّف على القطعة بثقة كافية — من فضلك اعرض الباركود المطبوع.",
        })

    payload = safe_product_payload(
        product, branch=device.branch, include_scrap=(purpose == "scrap"),
    )
    payload["found"] = True
    payload["scan_id"] = event.id

    # Scrap flow: assess wear and suggest a RETAIL price.
    if purpose == "scrap" and image is not None:
        cond, notes = vision.assess_condition(image_bytes)
        suggested = services.suggest_used_price(product, cond)
        event.condition_score = cond
        event.condition_notes = notes
        event.suggested_price = suggested
        event.save(update_fields=["condition_score", "condition_notes", "suggested_price"])
        payload["condition_score"] = cond
        payload["suggested_price"] = float(suggested)

    # Multi-agent: low stock → procurement signal (idempotent).
    signal = services.maybe_raise_procurement_signal(
        device=device, product=product, branch=device.branch,
    )
    payload["low_stock_signal"] = bool(signal)

    return Response(payload)


@_robot_endpoint
def voice(request):
    """Hands-free assistant + POS voice answers.

    Body: `transcript` (STT text), optional `face_embedding`. Classifies intent,
    queries the ERP (stock/price retail-only, or a fault code), returns
    `reply_text` for the amp to speak.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    transcript = (request.data.get("transcript") or "").strip()
    employee = _authorized_employee(request, device)
    intent, reply, payload = _handle_voice(transcript, device)

    RobotVoiceInteraction.objects.create(
        device=device, transcript=transcript, intent=intent,
        reply_text=reply, employee=employee, payload=payload,
    )
    return Response({"intent": intent, "reply": reply, **payload})


def _handle_voice(transcript: str, device):
    """Tiny bilingual intent router for the voice assistant."""
    low = transcript.lower()

    # Fault code question, e.g. "P0301" or "كود p0420".
    import re
    m = re.search(r"\b([pbcu][0-9]{4})\b", low)
    if m:
        res = services.lookup_fault_code(m.group(1).upper())
        if res.get("found"):
            reply = f"{res['code']}: {res['description']}."
            if res.get("likely_causes"):
                reply += " الأسباب المحتملة: " + "، ".join(res["likely_causes"][:3]) + "."
            return "diagnostic", reply, {"fault": res}
        return "diagnostic", f"لم أجد تعريفاً للكود {m.group(1).upper()}.", {}

    # Stock-take command, e.g. "اجرد الكنترول والفلاتر" / "count the ...".
    if low.startswith("اجرد") or low.startswith("جرد") or "stock take" in low or "count " in low:
        return ("command",
                "تمام، ابدأ عدّ القطع وأنا أسجّلها. قول اسم كل قطعة والعدد.",
                {"action": "start_stock_take"})

    # Otherwise treat as an inventory/stock question.
    ans = services.inventory_answer(transcript, branch=device.branch)
    if not ans.get("found"):
        return "inventory_query", "لم أجد القطعة دي في المخزون. ممكن تقولي رقمها؟", {}
    price = ans.get("retail_price")
    stock = ans.get("stock", 0)
    if stock > 0:
        reply = f"أيوه، {ans['name']} متوفر ({stock} قطعة) بسعر {price:.0f} جنيه."
    else:
        reply = f"{ans['name']} مش متوفر حالياً في الفرع."
    return "inventory_query", reply, {"product": ans}


@_robot_endpoint
def face(request):
    """Facial recognition → attendance clock-in/out + session authorization.

    Body: `face_embedding` (list of floats), optional `image`. Logs the event,
    clocks the employee in/out via hr.AttendanceRecord, and returns whether the
    session is authorized to create sales.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    embedding = request.data.get("face_embedding")
    employee, score = security.identify_employee(embedding, branch=device.branch)

    if not employee:
        RobotAccessLog.objects.create(
            device=device, result="unknown", action="scan",
            match_score=score, image=request.FILES.get("image"),
        )
        return Response({"authorized": False, "result": "unknown", "match_score": score})

    action, record = security.register_attendance(employee, match_score=score)
    RobotAccessLog.objects.create(
        device=device, employee=employee, result="granted", action=action,
        match_score=score, image=request.FILES.get("image"),
    )
    return Response({
        "authorized": True,
        "employee_id": employee.id,
        "employee_name": employee.name,
        "action": action,
        "match_score": score,
    })


@_robot_endpoint
def sale(request):
    """Create a retail sale from a scan. Requires a face-authorized employee.

    Body: `scan_id` OR `part_number`, `customer_name`, `customer_phone`,
    `payment` (cash|credit), `face_embedding` (or `employee_id`), optional
    `quantity`, `unit_price` (retail only). Security gate per requirements.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    employee = _authorized_employee(request, device)
    if not employee:
        return Response(
            {"authorized": False, "detail": "الوجه غير مصرّح — لا يمكن إنشاء فاتورة."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Resolve product from scan or part number.
    product = None
    scan_id = request.data.get("scan_id")
    if scan_id:
        ev = RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
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
        customer, _c = Customer.objects.get_or_create(phone=phone, defaults={"name": name})
    else:
        customer, _c = Customer.objects.get_or_create(
            phone="0000000000", defaults={"name": "عميل نقدي"},
        )

    quantity = int(request.data.get("quantity", 1) or 1)
    unit_price = request.data.get("unit_price")
    unit_price = Decimal(str(unit_price)) if unit_price not in (None, "") else None

    invoice = services.create_robot_sale(
        product=product, branch=device.branch, customer=customer,
        employee=employee, quantity=quantity, unit_price=unit_price,
        payment=request.data.get("payment", "cash"),
    )

    # Link the scan to the invoice for the audit trail.
    if scan_id:
        RobotScanEvent.objects.filter(pk=scan_id, device=device).update(
            sale_invoice=invoice, created_by=employee,
        )

    # Low-stock check after the sale deducted stock.
    services.maybe_raise_procurement_signal(
        device=device, product=product, branch=device.branch,
    )

    return Response({
        "ok": True,
        "invoice_id": invoice.id,
        "total_amount": float(invoice.total_amount),
        "authorized_by": employee.name,
    }, status=status.HTTP_201_CREATED)


@_robot_endpoint
def motor(request):
    """Queue a physical-articulation command (head/arms/tracks).

    Requires a face-authorized employee (safety: no arm lift by anonymous
    request). Body: `actuator`, `direction`, `duration_ms`, face auth.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    if not _authorized_employee(request, device):
        return Response(
            {"detail": "غير مصرّح بأوامر الحركة."}, status=status.HTTP_403_FORBIDDEN,
        )
    cmd = MotorCommandLog.objects.create(
        device=device,
        actuator=request.data.get("actuator", "head"),
        direction=request.data.get("direction", "stop"),
        duration_ms=int(request.data.get("duration_ms", 0) or 0),
    )
    return Response({"ok": True, "command_id": cmd.id, "frame": cmd.serial_frame()},
                    status=status.HTTP_201_CREATED)


@_robot_endpoint
def motor_pending(request):
    """ESP32 polls for unacknowledged motor commands, then acks them.

    GET returns pending frames; the ESP32 forwards each to the Mega over serial
    and the same call marks them acknowledged (fire-and-forget model).
    """
    device, err = _device_or_401(request)
    if err:
        return err
    pending = list(device.motor_commands.filter(acknowledged=False).order_by("created_at")[:20])
    frames = [{"command_id": c.id, "frame": c.serial_frame()} for c in pending]
    MotorCommandLog.objects.filter(id__in=[c.id for c in pending]).update(
        acknowledged=True, acknowledged_at=timezone.now(),
    )
    return Response({"commands": frames})


@_robot_endpoint
def intake(request):
    """Goods intake: photograph a part, register it, add stock.

    "صوّر الكنترول ده واعمله خلفية بيضا وسجّل البارت نمبر وقوله اسمه، ودوّر عليه
    في المخزون — لو موجود زوّد الكمية، لو مش موجود ضيف بند جديد بصورته وسعره."

    Requires a face-authorized employee (it writes stock + prices). Body:
    `image` (file, optional), `name`, `part_number` (optional — read from photo),
    `retail_price`, `quantity`, `car_model`, `part_category`, face auth.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    employee = _authorized_employee(request, device)
    if not employee:
        return Response(
            {"authorized": False, "detail": "الوجه غير مصرّح — لا يمكن إدخال بضاعة."},
            status=status.HTTP_403_FORBIDDEN,
        )

    image = request.FILES.get("image")
    image_bytes = image.read() if image else None
    retail_price = request.data.get("retail_price")

    result = services.intake_part(
        device=device, branch=device.branch, image_bytes=image_bytes,
        name=(request.data.get("name") or "").strip(),
        part_number=(request.data.get("part_number") or "").strip(),
        retail_price=retail_price if retail_price not in (None, "") else None,
        quantity=int(request.data.get("quantity", 1) or 1),
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
    device, err = _device_or_401(request)
    if err:
        return err
    employee = _authorized_employee(request, device)
    if not employee:
        return Response(
            {"authorized": False, "detail": "الوجه غير مصرّح — لا يمكن بدء جرد."},
            status=status.HTTP_403_FORBIDDEN,
        )
    result = services.run_stock_take(
        device=device, branch=device.branch,
        counts=request.data.get("counts") or [],
        instruction=(request.data.get("instruction") or "").strip(),
        employee=employee,
    )
    return Response(result, status=status.HTTP_201_CREATED)


@_robot_endpoint
def procurement_signals(request):
    """Open low-stock signals for the Procurement Agent to build the manifest."""
    device, err = _device_or_401(request)
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
