"""
robot/views.py — REST API the ESP32 firmware calls.

Auth model: the ESP32 authenticates as a *device* via `X-Robot-Token`
(`robot.security.authenticate_device`), not as a Django user — the robot stands
on the shop floor, not behind a login. Privileged actions (creating a sale,
dispensing a part) additionally require a face-authorized employee in the same
request, enforced here. The device may send the face embedding itself, or echo
back an `employee_id` from a face match this same device was granted in the
last few minutes — an id on its own is never accepted, or the device token
alone would be enough to sell as anybody.

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
  POST teach/                staff correct a scan / teach an alias (learning loop)
  POST enroll/capture/       camera capture for the staff face-enrollment round
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from . import audio as audio_svc
from . import customers as customers_svc
from . import enrollment, faces, permissions, services, security, vision, wakename
from .models import (
    MotorCommandLog, ProcurementSignal, RobotAccessLog, RobotCommand,
    RobotDevice, RobotScanEvent, RobotSnapshot, RobotVoiceInteraction,
)
from .pricing import safe_product_payload

# Confidence below which vision must defer to the printed barcode.
_MIN_VISION_CONFIDENCE = 0.75

# How long a successful face authorization stays valid for follow-up privileged
# calls from the same device. The firmware scans a face once and then echoes the
# employee id back on the sale/motor calls of that visit; beyond this window it
# has to show the face again.
_FACE_SESSION_MINUTES = 10


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

    # An employee id echoed back by the firmware is NOT proof of anything on its
    # own — anyone holding the device token could post any id. Honour it only
    # when THIS device actually granted that employee a face match in the last
    # few minutes, which `face/` recorded in RobotAccessLog.
    emp_id = request.data.get("employee_id")
    if emp_id:
        since = timezone.now() - timedelta(minutes=_FACE_SESSION_MINUTES)
        granted = (
            RobotAccessLog.objects
            .filter(
                device=device,
                employee_id=emp_id,
                result="granted",
                created_at__gte=since,
            )
            .select_related("employee")
            .order_by("-created_at")
            .first()
        )
        if granted and granted.employee and _employee_is_active(granted.employee):
            return granted.employee
    return None


# The mic is on the bridge board and the camera on the other one, so a spoken
# command carries no face. The person talking is almost always the one the
# camera just recognized in front of the robot — but only very recently, and
# only for voice (sales/motion still need their own face match).
_VOICE_FACE_SECONDS = 60


def _voice_employee(request, device):
    """Employee for a voice turn: explicit face auth, else a face this device
    matched in the last `_VOICE_FACE_SECONDS`."""
    employee = _authorized_employee(request, device)
    if employee is not None:
        return employee
    since = timezone.now() - timedelta(seconds=_VOICE_FACE_SECONDS)
    recent = (RobotAccessLog.objects
              .filter(device=device, result="granted", created_at__gte=since)
              .select_related("employee").order_by("-created_at").first())
    if recent and recent.employee and _employee_is_active(recent.employee):
        return recent.employee
    return None


def _employee_is_active(employee) -> bool:
    """True unless the employee's linked user account has been deactivated."""
    user = getattr(employee, "user", None)
    return True if user is None else bool(user.is_active)


def _require_permission(request, device, action):
    """Resolve the face-authorized employee AND check their role may do `action`.

    Returns (employee, None) when allowed, or (None, Response) with 403 —
    unrecognized face → generic deny; recognized but wrong role → spoken reason.
    """
    employee = _authorized_employee(request, device)
    if not employee:
        return None, Response(
            {"authorized": False, "detail": "الوجه غير مصرّح."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not permissions.employee_can(employee, action):
        return None, Response(
            {
                "authorized": True,
                "permitted": False,
                "role": permissions.employee_role(employee),
                "detail": permissions.denial_message(action),
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    return employee, None


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

    event = RobotScanEvent.objects.create(
        device=device, purpose=purpose, image=image,
        recognized_label=label, recognized_part_number=part_number,
        confidence=confidence, product=product,
    )

    if not product:
        miss = {
            "found": False,
            "scan_id": event.id,
            "message": "لم أتعرّف على القطعة بثقة كافية — من فضلك اعرض الباركود المطبوع.",
        }
        _announce_scan(request, device, miss)
        return Response(miss)

    payload = safe_product_payload(
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
    RobotCommand.objects.filter(pk=cmd_id, device=device).update(
        status="done", done_at=timezone.now(), result={"scan_id": payload.get("scan_id")},
    )
    if not payload.get("found"):
        text = "مش قادر أتعرف على القطعة دي — قرّب الباركود للكاميرا."
    else:
        stock = payload.get("stock", 0)
        price = payload.get("suggested_price") or payload.get("retail_price") or 0
        text = f"دي {payload.get('name')}. "
        text += (f"متوفر منها {stock} بسعر {price:.0f} جنيه." if stock
                 else "مش متوفرة حالياً في الفرع.")
        if payload.get("needs_confirmation"):
            text += " عرفتها من شكلها، أكّدها من فضلك."
    RobotCommand.objects.create(device=device, kind="say", payload={"text": text})


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

    # Accept either a ready transcript OR raw audio (transcribed server-side).
    transcript = (request.data.get("transcript") or "").strip()
    audio = request.FILES.get("audio")
    if not transcript and audio is not None:
        transcript = audio_svc.transcribe(audio.read()) or ""

    # Only speech addressed to the robot by name gets an answer. People
    # talking to each other nearby are ignored — not answered, not stored.
    ptt = str(request.data.get("ptt", "")).lower() in ("1", "true", "yes")
    # Only the enrollment round's own control words ("مش موجود", "التالي")
    # skip the name — any other chatter while names are being called is still
    # ignored. A stock count needs no exception: every accepted count extends
    # the 20 s follow-up window, so a steady counter never repeats the name.
    ongoing = (_is_enrollment_control(transcript)
               and enrollment.active_session(device) is not None)
    for_robot, text, name_only = wakename.gate(
        device, transcript, push_to_talk=ptt, ongoing_flow=ongoing)
    if not for_robot:
        return Response({"intent": "ignored", "reply": "", "addressed": False})
    wakename.keep_listening(device)
    if name_only:
        return Response({"intent": "wake", "reply": "أيوه، تحت أمرك.",
                         "addressed": True, "transcript": transcript})

    employee = _voice_employee(request, device)
    intent, reply, payload = _handle_voice(text, device, employee)

    RobotVoiceInteraction.objects.create(
        device=device, transcript=transcript, intent=intent,
        reply_text=reply, employee=employee, payload=payload,
    )
    return Response({"intent": intent, "reply": reply, "transcript": transcript,
                     "addressed": True, **payload})


_ENROLL_SKIP_WORDS = ("مش موجود", "مش هنا", "التالي", "اللي بعده", "skip", "next")
_ENROLL_CANCEL_WORDS = ("وقف التسجيل", "الغي التسجيل", "stop enrollment")


def _is_enrollment_control(transcript: str) -> bool:
    low = (transcript or "").lower()
    return any(w in low for w in _ENROLL_SKIP_WORDS + _ENROLL_CANCEL_WORDS)


def _handle_voice(transcript: str, device, employee=None):
    """Tiny bilingual intent router for the voice assistant."""
    low = transcript.lower()

    # --- Teaching: "اتعلم الطرمبة يعني 11517586925" --------------------
    # Checked first: during a stock-take the trailing part number would
    # otherwise be read as a count.
    taught = _TEACH_RE.match(transcript.strip())
    if taught:
        alias, target = taught.group(1).strip(), taught.group(2).strip()
        if not permissions.employee_can(employee, "teach"):
            return ("command", permissions.denial_message("teach"), {"action": "denied"})
        product = services.find_product(target)
        if product is None:
            return ("command", f"مش لاقي «{target}» في الأصناف — قول رقم القطعة.",
                    {"action": "teach_unresolved"})
        services.learn_from_confirmation(
            product=product, label=alias, employee=employee,
            details={"source": "voice"},
        )
        return ("command", f"تمام، اتعلمت إن «{alias}» يعني {product.name}.",
                {"action": "learned", "alias": alias, "product_id": product.id})

    # --- Answering a page: "جاي" / "حاضر" from the employee who was called --
    if employee is not None and _PAGE_ACK_RE.search(low):
        from .models import RobotPageCall
        page = (RobotPageCall.objects
                .filter(device=device, target_employee=employee, status="announced",
                        announced_at__gte=timezone.now() - timedelta(minutes=15))
                .order_by("-announced_at").first())
        if page is not None:
            page.status = "acknowledged"
            page.save(update_fields=["status"])
            # Let whoever paged see the answer in their alerts feed.
            from .models import RobotAlert
            RobotAlert.objects.create(
                device=device, kind="other",
                message=f"📢 {employee.name} ردّ على النداء: جاي.",
            )
            return ("command", f"تمام يا {employee.name}، هبلّغهم إنك جاي.",
                    {"action": "page_acknowledged", "page_id": page.id})

    # --- Staff face enrollment round -------------------------------------
    round_ = enrollment.active_session(device)
    if round_ is not None:
        if any(w in low for w in _ENROLL_SKIP_WORDS):
            skipped = enrollment.skip_current(round_, note="اتقال مش موجود")
            return ("command", f"ماشي، هنتخطى {skipped['name'] if skipped else 'الموظف ده'}.",
                    {"action": "enroll_skip"})
        if any(w in low for w in _ENROLL_CANCEL_WORDS):
            enrollment.cancel(round_)
            return ("command", "تمام، وقفت تسجيل البصمات.", {"action": "enroll_cancel"})
    if "بصمات" in low and any(w in low for w in ("سجل", "سجّل", "تسجيل")):
        if not permissions.employee_can(employee, "enroll_staff"):
            return ("command", permissions.denial_message("enroll_staff") +
                    " أو ابدأه من لوحة التحكم.", {"action": "denied"})
        try:
            enrollment.start(device, user=getattr(employee, "user", None))
        except enrollment.EnrollmentUnavailable as exc:
            return ("command", str(exc), {"action": "enroll_unavailable"})
        return ("command", "تمام، هبدأ أنادي الموظفين واحد واحد.", {"action": "enroll_start"})

    # --- "امسح القطعة دي": ask the camera to look -------------------------
    if any(w in low for w in ("امسح القطعة", "شوف القطعة", "اعرف القطعة", "scan this")):
        RobotCommand.objects.create(device=device, kind="scan",
                                    payload={"purpose": "lookup"})
        return ("command", "ثانية واحدة، قرّب القطعة من الكاميرا.", {"action": "scan_requested"})

    # --- Conversational stock-take (جرد) ---------------------------------
    open_session = services.get_open_stock_take(device)

    # Finish an in-progress count.
    if open_session and (low.startswith("خلص") or low.startswith("انهاء") or
                         low.startswith("إنهاء") or re.search(r"\b(finish|done)\b", low)):
        report = services.complete_stock_take(open_session)
        n, v = report["counted_items"], len(report["variances"])
        return ("command",
                f"خلّصت الجرد: عدّينا {n} صنف، فيه {v} فرق. تقدر تعتمد التسويات من اللوحة.",
                {"action": "stock_take_done", "report": report})

    # Start a new count — only a stock/manager role may (RBAC on voice too).
    if (low.startswith("اجرد") or low.startswith("جرد") or
            "stock take" in low or low.startswith("count")):
        if not permissions.employee_can(employee, "stock_take"):
            return ("command", permissions.denial_message("stock_take"),
                    {"action": "denied"})
        services.start_stock_take(device, device.branch,
                                  instruction=transcript, employee=employee)
        return ("command",
                "تمام، ابدأ عدّ القطع. قول اسم كل قطعة والعدد, ولما تخلص قول: خلص الجرد.",
                {"action": "start_stock_take"})

    # While counting, each turn like "كنترول ٣" adds a line.
    if open_session:
        query, qty = services.parse_count_utterance(transcript)
        if query is not None and qty is not None:
            line, product = services.add_stock_take_count(
                open_session, query=query, counted_qty=qty)
            if product is None:
                return ("command",
                        f"مش لاقي «{query}» في المخزون — قول الاسم أو الرقم تاني.",
                        {"action": "count_unresolved"})
            return ("command",
                    f"سجّلت {product.name}: {qty}. القطعة اللي بعدها؟",
                    {"action": "count_added",
                     "expected": line.expected_qty, "counted": line.counted_qty})

    # --- Fault code question, e.g. "P0301" or "كود p0420" ----------------
    m = re.search(r"\b([pbcu][0-9]{4})\b", low)
    if m:
        res = services.lookup_fault_code(m.group(1).upper())
        if res.get("found"):
            reply = f"{res['code']}: {res['description']}."
            if res.get("likely_causes"):
                reply += " الأسباب المحتملة: " + "، ".join(res["likely_causes"][:3]) + "."
            return "diagnostic", reply, {"fault": res}
        return "diagnostic", f"لم أجد تعريفاً للكود {m.group(1).upper()}.", {}

    # --- Otherwise: inventory/stock question -----------------------------
    if not transcript.strip():
        return "unknown", "مسمعتكش كويس، ممكن تعيد؟", {}
    ans = services.inventory_answer(transcript, branch=device.branch)
    if not ans.get("found"):
        # Flagged so the dashboard can list what the robot couldn't answer and
        # staff can teach it ("اتعلم … يعني …") — its gaps become lessons.
        # A general question still gets a helpful spoken answer from the LLM.
        general = services.ai_reply(transcript)
        if general:
            return "unknown", general, {"unresolved": True, "ai": True}
        return ("inventory_query", "لم أجد القطعة دي في المخزون. ممكن تقولي رقمها؟",
                {"unresolved": True})
    price = ans.get("retail_price") or 0
    stock = ans.get("stock", 0)
    if stock > 0:
        reply = f"أيوه، {ans['name']} متوفر ({stock} قطعة) بسعر {price:.0f} جنيه."
        # Say where it is, and turn the head toward that shelf when mapped.
        from inventory.models import Product
        product = Product.objects.filter(pk=ans.get("id")).first()
        shelf = services.shelf_location(product, device.branch) if product else ""
        if shelf:
            reply += f" موجود في الرف {shelf}."
            ans["shelf_location"] = shelf
            services.point_to_shelf(device, shelf)
    else:
        reply = f"{ans['name']} مش متوفر حالياً في الفرع."
        # Someone asked for it and we have none: that is demand, so let the
        # Procurement Agent know (idempotent per part/branch).
        from inventory.models import Product
        product = Product.objects.filter(pk=ans.get("id")).first()
        if product is not None:
            services.maybe_raise_procurement_signal(
                device=device, product=product, branch=device.branch,
            )
    return "inventory_query", reply, {"product": ans}


# A paged employee answering the robot's call.
_PAGE_ACK_RE = re.compile(r"(^|\s)(جاي|جايلك|جايين|حاضر|coming|on my way)(\s|$)")

# "اتعلم <كلمة> يعني <رقم/اسم القطعة>" / "learn <word> means <part>".
_TEACH_RE = re.compile(
    r"^(?:اتعلم|اتعلّم|تعلم|learn)\s+(.+?)\s+(?:يعني|=|means|is)\s+(.+)$",
    re.IGNORECASE,
)


@_robot_endpoint
def face(request):
    """Facial recognition → attendance clock-in/out + session authorization.

    Body: `face_embedding` (list of floats) OR `image` (a JPEG the ESP32-CAM
    sends — the embedding is then extracted server-side). Logs the event, clocks
    the employee in/out via hr.AttendanceRecord, and returns whether the session
    is authorized to create sales.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    # Accept a ready embedding, or extract one from a posted image.
    embedding = request.data.get("face_embedding")
    if not embedding and request.FILES.get("image") is not None:
        img = request.FILES["image"]
        img_bytes = img.read()
        try:
            img.seek(0)
        except Exception:
            pass
        embedding = faces.extract_embedding(img_bytes)

    # Without a real face model nothing is authorized, and saying so beats
    # logging a silent "unknown" that looks like a badly-lit photo.
    if not security.matching_available():
        return Response(
            {
                "authorized": False,
                "result": "unavailable",
                "detail": (
                    "التعرّف على الوجه مش مفعّل: مفيش موديل وجه حقيقي مثبّت، "
                    "والبديل مايقدرش يفرّق بين الأشخاص."
                ),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    employee, score = security.identify_employee(embedding, branch=device.branch)

    if not employee:
        RobotAccessLog.objects.create(
            device=device, result="unknown", action="scan",
            match_score=score, image=request.FILES.get("image"),
        )
        return Response({"authorized": False, "result": "unknown", "match_score": score})

    # "authorize" = seen while authorizing / on motion; "attendance" = a
    # deliberate check-in/out. Only the latter can end a shift.
    purpose = str(request.data.get("purpose") or "attendance").lower()
    if purpose not in ("attendance", "authorize"):
        purpose = "attendance"
    action, record = security.register_attendance(
        employee, match_score=score, purpose=purpose,
    )
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
def customer_greet(request):
    """Greet a walk-in customer, recognizing them by face / name / phone / invoice.

    Body (any of): `image` (face), `name`, `phone`, `invoice_number`. On a match
    the robot remembers the visit (and enrolls the face for next time when an
    image is given) and returns a warm, personal greeting plus a private
    `staff_note` (e.g. outstanding balance) that is NOT part of the spoken text.

    No employee auth needed — this is customer-facing and never exposes pricing
    beyond the customer's own record.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    # Face embedding from a posted image, if any.
    embedding = request.data.get("face_embedding")
    if not embedding and request.FILES.get("image") is not None:
        img = request.FILES["image"]
        b = img.read()
        embedding = faces.extract_embedding(b)

    customer, method, score = customers_svc.recognize_customer(
        embedding=embedding,
        name=(request.data.get("name") or "").strip(),
        phone=(request.data.get("phone") or "").strip(),
        invoice_number=(request.data.get("invoice_number") or "").strip(),
    )

    if not customer:
        return Response({
            "recognized": False,
            "greeting": "أهلاً بيك في Mouss Tec! أنا تحت أمرك — محتاج قطعة أو استفسار؟",
        })

    # Enrolling the customer's face writes biometric data, so it needs a staff
    # member present who is allowed to do it. Greeting and recognising an
    # already-enrolled customer stay open — this only gates the write.
    may_enroll = permissions.employee_can(
        _authorized_employee(request, device), "customer_enroll"
    )
    face = customers_svc.remember_visit(
        customer, embedding=embedding, may_enroll_face=may_enroll
    )
    info = customers_svc.customer_greeting(
        customer, method=method, visit_count=face.visit_count,
        notes=face.notes,
    )

    # Turn the head to face the customer, if the cam told us where they are.
    if request.data.get("face_offset") is not None:
        services.look_at(device, request.data.get("face_offset"))

    return Response({
        "recognized": True,
        "customer_id": customer.id,
        "customer_name": customer.name,
        "recognized_by": method,
        "match_score": score,
        "visit_count": face.visit_count,
        **info,
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

    employee, perr = _require_permission(request, device, "sale")
    if perr:
        return perr

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

    # Don't sell what the branch doesn't have. Staff can still take an order
    # for a part on its way by saying so explicitly (`allow_backorder`).
    backorder = str(request.data.get("allow_backorder", "")).lower() in ("1", "true", "yes")
    on_hand = services.branch_stock(product, device.branch)
    if quantity > on_hand and not backorder:
        return Response(
            {"detail": f"المتاح في الفرع {on_hand} بس من {product.name}.",
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

    invoice = services.create_robot_sale(
        product=product, branch=device.branch, customer=customer,
        employee=employee, quantity=quantity, unit_price=unit_price,
        payment=request.data.get("payment", "cash"),
    )

    # Link the scan to the invoice for the audit trail, and LEARN from the
    # confirmed match (scanned code/label → this product) so recognition of the
    # same part gets faster and more confident next time.
    if scan_id:
        ev = RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
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
    employee, perr = _require_permission(request, device, "motor")
    if perr:
        return perr
    try:
        actuator, direction, duration_ms = services.validate_motor_command(
            request.data.get("actuator"), request.data.get("direction"),
            request.data.get("duration_ms"),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    cmd = MotorCommandLog.objects.create(
        device=device, actuator=actuator, direction=direction,
        duration_ms=duration_ms, issued_by=getattr(employee, "user", None),
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
    employee, perr = _require_permission(request, device, "intake")
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
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "stock_take")
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
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "stock_take_apply")
    if perr:
        return perr
    from .models import RobotStockTakeSession
    session = RobotStockTakeSession.objects.filter(
        pk=request.data.get("session_id"), device=device,
    ).first()
    if session is None:
        return Response({"detail": "جلسة الجرد غير موجودة."},
                        status=status.HTTP_404_NOT_FOUND)
    result = services.apply_stock_take(session, employee=employee)
    return Response(result)


@_robot_endpoint
def speak(request):
    """Text-to-speech: return synthesized audio bytes for the amp to play.

    Body: `text`. Returns audio/mpeg when a TTS provider is available, else 204
    so the firmware can fall back to on-device synthesis.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    text = (request.data.get("text") or "").strip()[:600]
    from django.http import HttpResponse
    # `format=wav` → 16 kHz mono PCM the ESP32 streams straight to the amp.
    if str(request.data.get("format", "")).lower() == "wav":
        wav = audio_svc.synthesize_wav(text) if text else None
        if not wav:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return HttpResponse(wav, content_type="audio/wav")
    audio_bytes = audio_svc.synthesize(text) if text else None
    if not audio_bytes:
        return Response(status=status.HTTP_204_NO_CONTENT)
    return HttpResponse(audio_bytes, content_type="audio/mpeg")


@_robot_endpoint
def camera_frame(request):
    """The ESP32-CAM pushes its latest live frame here (camera runs 24/7).

    Body: `image` (JPEG), optional `motion` (1 when the frame differs / PIR
    fired). Stores the frame for the dashboard live view; if motion is flagged
    AND it's after-hours, raises an alert with a saved snapshot.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    if image is not None and device.camera_always_on:
        # The live view needs the LATEST frame, not every frame ever pushed.
        # Django writes a new file on each save and never removes the old one,
        # so with a camera running 24/7 this directory would grow until the
        # disk filled. Drop the previous frame as we replace it; deliberate
        # captures live in RobotSnapshot and are untouched.
        previous = device.last_frame
        device.last_frame = image
        device.last_frame_at = timezone.now()
        device.save(update_fields=["last_frame", "last_frame_at"])
        if previous:
            try:
                previous.delete(save=False)
            except Exception:
                pass  # a missing/locked old frame must never fail the upload

    motion = str(request.data.get("motion", "")).lower() in ("1", "true", "yes")
    alerted = False
    if motion:
        device.last_motion_at = timezone.now()
        device.save(update_fields=["last_motion_at"])
        if services.is_after_hours(device):
            snap = None
            if image is not None:
                try:
                    image.seek(0)
                except Exception:
                    pass
                snap = RobotSnapshot.objects.create(
                    device=device, image=image, reason="after_hours",
                )
            services.raise_after_hours_alert(device, snapshot=snap)
            alerted = True
    # Tell the camera how fast to push: fast while a supervisor is watching
    # (stream_until in the future), slow when idle — smooth video on demand
    # without hammering the network 24/7.
    # Camera-side work rides on this reply (the cam doesn't poll anything
    # else): pending snapshot/scan commands, and — during a staff enrollment
    # round — who to capture now.
    cam_cmds = services.take_camera_commands(device)
    try:
        enroll = enrollment.camera_prompt(device)
    except Exception:
        enroll = None
    return Response({
        "ok": True, "motion": motion, "after_hours_alert": alerted,
        "push_interval_ms": 700 if enroll else device.desired_push_interval_ms(),
        "commands": cam_cmds,
        "enroll": enroll,
    })


@_robot_endpoint
def telemetry(request):
    """Device reports its health (battery/temp/disk/wifi) for the live view.

    Body: any of `battery_percent`, `cpu_temp`, `free_disk_mb`, `wifi_rssi`.
    Raises a low-battery alert (deduped) when the battery drops below 15%.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    def _num(key, cast):
        v = request.data.get(key)
        if v in (None, ""):
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    # The body carries *any of* these, so only write what was actually sent —
    # a device reporting just its battery must not blank out the temperature,
    # disk and signal readings the dashboard is showing.
    changed = []
    for key, cast in (("battery_percent", int), ("cpu_temp", float),
                      ("free_disk_mb", int), ("wifi_rssi", int)):
        if key not in request.data:
            continue
        setattr(device, key, _num(key, cast))
        changed.append(key)
    device.telemetry_at = timezone.now()
    device.save(update_fields=changed + ["telemetry_at"])

    if device.battery_percent is not None and device.battery_percent < 15:
        services.raise_low_battery_alert(device, device.battery_percent)

    # Per-motor current (amps) from the Mega's current sensors → predictive
    # maintenance: stall and wear alerts before a motor dies.
    currents = request.data.get("motor_current")
    faults = []
    if isinstance(currents, dict) and currents:
        faults = services.record_motor_currents(device, currents)
    return Response({"ok": True, "motor_alerts": len(faults)})


@_robot_endpoint
def snapshot_upload(request):
    """Device uploads a still (usually answering a `snapshot` command)."""
    device, err = _device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    if image is None:
        return Response({"detail": "no image"}, status=status.HTTP_400_BAD_REQUEST)
    reason = str(request.data.get("reason") or "manual")
    if reason not in dict(RobotSnapshot.REASON):
        reason = "manual"
    snap = RobotSnapshot.objects.create(device=device, image=image, reason=reason)
    # Ack the originating command if one was referenced.
    cmd_id = request.data.get("command_id")
    if cmd_id:
        RobotCommand.objects.filter(pk=cmd_id, device=device).update(
            status="done", done_at=timezone.now(), result={"snapshot_id": snap.id},
        )
    return Response({"ok": True, "snapshot_id": snap.id}, status=status.HTTP_201_CREATED)


@_robot_endpoint
def commands_pending(request):
    """Device polls queued dashboard/owner commands, which are marked sent."""
    device, err = _device_or_401(request)
    if err:
        return err
    # Snapshot/scan belong to the camera board (delivered via /camera/frame/);
    # handing them to the bridge would ack them without a photo ever taken.
    pending = list(device.commands.filter(status="pending")
                   .exclude(kind__in=RobotCommand.CAMERA_KINDS)
                   .order_by("created_at")[:20])
    out = [{"command_id": c.id, "kind": c.kind, "payload": c.payload} for c in pending]
    RobotCommand.objects.filter(id__in=[c.id for c in pending]).update(
        status="sent", sent_at=timezone.now(),
    )
    return Response({"commands": out})


@_robot_endpoint
def commands_ack(request):
    """Device confirms a command finished (with optional result)."""
    device, err = _device_or_401(request)
    if err:
        return err
    cmd = RobotCommand.objects.filter(
        pk=request.data.get("command_id"), device=device,
    ).first()
    if cmd is None:
        return Response({"detail": "unknown command"}, status=status.HTTP_404_NOT_FOUND)
    ok = str(request.data.get("ok", "1")).lower() in ("1", "true", "yes")
    cmd.status = "done" if ok else "failed"
    cmd.done_at = timezone.now()
    cmd.result = request.data.get("result", {}) or {}
    cmd.save(update_fields=["status", "done_at", "result"])
    # A 'page' command that completed marks its RobotPageCall announced.
    if cmd.kind == "page" and ok:
        from .models import RobotPageCall
        page_id = (cmd.payload or {}).get("page_id")
        if page_id:
            RobotPageCall.objects.filter(pk=page_id, device=device).update(
                status="announced", announced_at=timezone.now(),
            )
    return Response({"ok": True})


@_robot_endpoint
def look(request):
    """Face the speaker: turn the head toward a horizontal `offset` ∈ [-1,1].

    The ESP32-CAM computes the offset from the detected face's position in frame
    and calls this; the head pans to center them. No auth beyond the device
    token — turning to look at someone is harmless.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    cmd = services.look_at(device, request.data.get("offset", 0),
                           issued_by=None)
    return Response({"ok": True, "turned": bool(cmd),
                     "frame": cmd.serial_frame() if cmd else None})


@_robot_endpoint
def sync_pull(request):
    """Download a RETAIL-ONLY offline catalog so the robot works with no net."""
    device, err = _device_or_401(request)
    if err:
        return err
    return Response(services.offline_catalog(device.branch))


@_robot_endpoint
def sync_push(request):
    """Replay queued offline events when the net returns (idempotent)."""
    device, err = _device_or_401(request)
    if err:
        return err
    result = services.apply_offline_events(device, request.data.get("events") or [])
    return Response(result)


@_robot_endpoint
def enroll_capture(request):
    """ESP32-CAM capture during a staff face-enrollment round.

    No face auth: at first install nobody can be recognized yet. It only acts
    while an owner-started round is active on THIS device, and only for the
    employee whose name the robot just called.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    if image is None:
        return Response({"detail": "no image"}, status=status.HTTP_400_BAD_REQUEST)
    return Response(enrollment.capture(device, image.read()))


@_robot_endpoint
def teach(request):
    """Teach the robot — the human half of its learning loop. Requires face auth.

    Body (one of):
      * `scan_id` + `part_number`: "this scan was really THAT part" — weakens
        the wrong guess, learns the right code/label/look, fixes the scan.
      * `alias` + `part_number`: "when someone says <alias>, they mean THAT
        part" — shop slang the voice assistant then understands in sentences.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "teach")
    if perr:
        return perr

    product = services.find_product((request.data.get("part_number") or "").strip())
    if product is None:
        return Response({"detail": "القطعة غير موجودة."}, status=status.HTTP_404_NOT_FOUND)

    scan_id = request.data.get("scan_id")
    if scan_id:
        ev = RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
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
    device, err = _device_or_401(request)
    if err:
        return err
    from . import kiosk
    q = request.query_params.get("q") or request.data.get("q") or ""
    return Response(kiosk.part_info(q, device.branch))


@_robot_endpoint
def kiosk_customer(request):
    """Kiosk: first name + loyalty for a phone (no balance, no history)."""
    device, err = _device_or_401(request)
    if err:
        return err
    from . import kiosk
    phone = request.query_params.get("phone") or request.data.get("phone") or ""
    return Response(kiosk.customer_brief(phone))


@_robot_endpoint
def kiosk_return_check(request):
    """Kiosk: return/warranty eligibility by `invoice_number` or `phone`."""
    device, err = _device_or_401(request)
    if err:
        return err
    from . import kiosk
    get = lambda k: request.query_params.get(k) or request.data.get(k) or ""  # noqa: E731
    return Response(kiosk.return_check(get("invoice_number"), get("phone")))


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
