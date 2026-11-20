"""🙂 API الروبوت — الوش: حضور وانصراف الموظفين وترحيب العملاء."""

from __future__ import annotations


from rest_framework import status
from rest_framework.response import Response

from .. import customers as customers_svc
from .. import assistant, faces, memory, permissions, services, security
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from ._common import _robot_endpoint


def _attendance_greeting(employee, action: str) -> str:
    """What the robot says when a face clocks someone in or out ('' otherwise:
    a passive sighting while already clocked in stays silent)."""
    first = (getattr(employee, "name", "") or "").split()
    name = f" يا {first[0]}" if first else ""
    if action == "clock_in":
        return f"أهلاً{name}، سجّلت حضورك."
    if action == "clock_out":
        return f"مع السلامة{name}، سجّلت انصرافك."
    return ""


@_robot_endpoint
def face(request):
    """Facial recognition → attendance clock-in/out + session authorization.

    Body: `face_embedding` (list of floats) OR `image` (a JPEG the ESP32-CAM
    sends — the embedding is then extracted server-side). Logs the event, clocks
    the employee in/out via hr.AttendanceRecord, and returns whether the session
    is authorized to create sales.
    """
    device, err = views._device_or_401(request)
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

    # A frame with nobody's face in it (the camera checks on any motion) is
    # not an unknown person: nothing to log.
    if not embedding:
        return Response({"authorized": False, "result": "no_face"})

    employee, score = security.identify_employee(embedding, branch=device.branch)

    if not employee:
        views.RobotAccessLog.objects.create(
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
    views.RobotAccessLog.objects.create(
        device=device, employee=employee, result="granted", action=action,
        match_score=score, image=request.FILES.get("image"),
    )
    # Say it out loud, so whoever walked in knows the robot logged them.
    greeting = _attendance_greeting(employee, action)
    if action == "clock_in":
        # The first face of the day: a manager hears yesterday and what's
        # waiting. An extra — never worth losing the clock-in over.
        try:
            briefing = assistant.morning_briefing(employee, device)
        except Exception:
            services.logger.warning("robot face: no morning briefing", exc_info=True)
            briefing = ""
        if briefing:
            greeting += " " + briefing
    if greeting:
        views.RobotCommand.objects.create(device=device, kind="say", payload={"text": greeting})
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
    device, err = views._device_or_401(request)
    if err:
        return err
    # Someone new in front of the robot: the last customer's part and car aren't theirs.
    memory.fresh_start(device)

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
            "greeting": f"{services.welcome()}! أنا تحت أمرك — محتاج قطعة أو استفسار؟",
        })

    # Enrolling the customer's face writes biometric data, so it needs a staff
    # member present who is allowed to do it. Greeting and recognising an
    # already-enrolled customer stay open — this only gates the write.
    may_enroll = permissions.employee_can(
        views._authorized_employee(request, device), "customer_enroll"
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
