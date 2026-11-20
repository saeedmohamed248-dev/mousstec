"""📷 API الروبوت — مسح القطعة بالكاميرا (تعرّف ← مخزون/سعر أو سعر مستعمل)."""

from __future__ import annotations


from django.utils import timezone
from rest_framework.response import Response

from .. import language, memory, services, vision
from ..brain.knowledge import pieces
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from ._common import _MIN_VISION_CONFIDENCE, _robot_endpoint


@_robot_endpoint
def scan(request):
    """ESP32-CAM part capture → identify → stock+retail price (or scrap price).

    Body: image (file) or `code` (barcode text the cam decoded), `purpose`
    (pos|scrap|lookup). For confident matches returns a retail-only payload; for
    the scrap flow adds a condition-based suggested retail price. Raises a
    procurement signal if stock is critically low.
    """
    device, err = views._device_or_401(request)
    if err:
        return err

    purpose = request.data.get("purpose", "pos")
    code = (request.data.get("code") or "").strip()
    image = request.FILES.get("image")

    label, part_number, confidence = "", "", 0.0
    product = None

    # Read the image bytes once, up front, so both vision and the scrap step can
    # use them regardless of whether a barcode `code` was also supplied. Then
    # rewind the file so it saves in full to the ImageField below.
    image_bytes = None
    if image is not None:
        image_bytes = image.read()
        try:
            image.seek(0)
        except Exception:
            pass

    # Prefer a decoded barcode (exact); else run vision on the image. Either
    # way what staff taught the robot is consulted before the raw catalogue,
    # and a photo it has seen before is recognized by its look alone.
    recognized_by = ""
    if code:
        product = (services.resolve_from_knowledge(code=code)
                   or services.find_product(code))
        confidence = 1.0 if product else 0.0
        part_number = code
        recognized_by = "code" if product else ""
    elif image_bytes:
        label, part_number, confidence = vision.identify_part(image_bytes)
        if confidence >= _MIN_VISION_CONFIDENCE and part_number:
            product = (services.resolve_from_knowledge(code=part_number)
                       or services.find_product(part_number))
            recognized_by = "vision" if product else ""
        if product is None:
            product, distance = services.match_fingerprint(
                services.image_hash(image_bytes))
            if product is not None:
                # Similar-looking parts exist, so a look-alone match is shown
                # for a human to confirm, never taken as certain.
                recognized_by = "learned_look"
                confidence = round(1.0 - distance / 64.0, 3)

    event = views.RobotScanEvent.objects.create(
        device=device, purpose=purpose, image=image,
        recognized_label=label, recognized_part_number=part_number,
        confidence=confidence, product=product,
    )

    if not product:
        miss = {
            "found": False,
            "scan_id": event.id,
            "message": "مش متأكد من القطعة دي، ورّيني الباركود اللي عليها لو سمحت.",
        }
        _announce_scan(request, device, miss)
        return Response(miss)

    payload = views.safe_product_payload(
        product, branch=device.branch, include_scrap=(purpose == "scrap"),
    )
    payload["found"] = True
    payload["scan_id"] = event.id
    payload["recognized_by"] = recognized_by
    payload["needs_confirmation"] = recognized_by == "learned_look"

    # Scrap flow: assess wear and suggest a RETAIL price.
    if purpose == "scrap" and image_bytes:
        cond, notes = vision.assess_condition(image_bytes)
        try:
            calibration = services.used_price_calibration()
        except Exception:
            calibration = 1.0
        suggested = services.suggest_used_price(product, cond, calibration)
        notes = dict(notes or {})
        notes["raw_suggested_price"] = float(services.suggest_used_price(product, cond))
        notes["calibration"] = calibration
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
    _announce_scan(request, device, payload)
    return Response(payload)


def _announce_scan(request, device, payload):
    """A scan someone asked for by voice gets its answer spoken by the robot.

    The camera board has no speaker, so when the scan answers a queued `scan`
    command we mark it done and queue a `say` for the bridge ESP32.
    """
    cmd_id = request.data.get("command_id")
    if not cmd_id:
        return
    cmd = views.RobotCommand.objects.filter(pk=cmd_id, device=device).first()
    if cmd is None:
        return
    views.RobotCommand.objects.filter(pk=cmd.pk).update(
        status="done", done_at=timezone.now(), result={"scan_id": payload.get("scan_id")},
    )
    if not payload.get("found"):
        text = "مش قادر أتعرف على القطعة دي — قرّب الباركود للكاميرا."
    else:
        if not payload.get("needs_confirmation"):
            # «عليه ضمان؟», «فيه أرخص؟» next are about the part just scanned.
            memory.remember_part(device, payload.get("id"))
        stock = payload.get("stock", 0)
        price = payload.get("suggested_price") or payload.get("retail_price") or 0
        text = f"دي {payload.get('name')}. "
        said = f" بسعر {services.spoken_price(price)}" if price > 0 else ""
        text += (f"عندي منها {pieces(stock)}{said}." if stock
                 else "مش موجودة هنا دلوقتي.")
        if payload.get("needs_confirmation"):
            text += " عرفتها من شكلها، أكّدها من فضلك."
    # Answered in the language the scan was asked for in.
    text = language.reply_in((cmd.payload or {}).get("lang", "ar"), text)
    views.RobotCommand.objects.create(device=device, kind="say", payload={"text": text})
