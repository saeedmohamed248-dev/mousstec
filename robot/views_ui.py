"""
robot/views_ui.py — staff-facing (login-required) dashboard & device profile.

This is the "Quick Access → Robot" page: one screen that shows everything the
robot has done — scans, sales it created, inventory movements/intake, expenses,
voice turns, face access logs, procurement signals, and what it has learned —
plus an editable profile per device.

Prices shown here are staff figures from the ERP; the retail-only rule applies
to what the *robot* emits, not to this internal supervisor view.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from datetime import timedelta

from inventory.views.utils import role_required

from . import replies as shop
from . import wakename
from .brain import replies as matcher
from .models import (
    MotorCommandLog, ProcurementSignal, RobotAccessLog, RobotAlert, RobotFaceEnrollment,
    RobotCommand, RobotCustomerFace, RobotDevice, RobotEntry, RobotKnowledge, RobotPageCall,
    RobotHeard, RobotHeardFix, RobotReply, RobotRestockRequest, RobotScanEvent, RobotSnapshot, RobotStaffPin,
    RobotStockTakeSession, RobotVoiceInteraction,
)


@login_required(login_url="/login/")
@role_required("admin", "manager")
def dashboard(request):
    """Everything the robot did, in one place."""
    since = timezone.now() - timedelta(days=30)

    devices = RobotDevice.objects.select_related("branch").all()

    scans = (RobotScanEvent.objects.select_related("product", "device", "sale_invoice")
             .order_by("-created_at")[:50])
    intakes = (RobotScanEvent.objects.filter(purpose="intake")
               .select_related("product", "created_by")
               .order_by("-created_at")[:50])

    # Sales the robot created (scan → invoice).
    robot_invoices = (RobotScanEvent.objects.filter(sale_invoice__isnull=False)
                      .select_related("sale_invoice", "sale_invoice__customer", "product")
                      .order_by("-created_at")[:50])

    # Inventory movements attributable to robot intake (stock-in) in last 30d.
    from inventory.models import InventoryMovement
    movements = (InventoryMovement.objects
                 .select_related("product", "branch")
                 .filter(created_at__gte=since)
                 .order_by("-created_at")[:50])

    # Invoices and expenses the robot itself recorded, with the branch each
    # one was put on (and the ones still waiting for a branch, or refused).
    expenses = (RobotEntry.objects.select_related("branch", "employee", "device")
                .order_by("-created_at")[:40])

    voice = RobotVoiceInteraction.objects.select_related("employee").order_by("-created_at")[:40]
    access = RobotAccessLog.objects.select_related("employee", "device").order_by("-created_at")[:40]
    signals = (ProcurementSignal.objects.filter(status="open")
               .select_related("product", "branch").order_by("-created_at")[:40])
    knowledge = RobotKnowledge.objects.select_related("product").order_by("-hit_count")[:50]
    stock_takes = (RobotStockTakeSession.objects.select_related("branch")
                   .prefetch_related("lines").order_by("-created_at")[:20])
    motors = MotorCommandLog.objects.select_related("device").order_by("-created_at")[:30]

    # Questions the robot couldn't answer — each is something staff can teach
    # it from the form next to the list.
    unanswered_qs = (RobotVoiceInteraction.objects
                     .filter(payload__unresolved=True, created_at__gte=since)
                     .exclude(transcript="")
                     .order_by("-created_at"))
    unanswered = list(unanswered_qs[:30])
    # Each as the question to teach (without «يا موس»), and the reply it
    # gets now if one was written since — shown so it can be edited.
    taught = _replies_to_match()
    for row in unanswered:
        row.question = _without_name(row.transcript, devices)
        found = matcher.match(row.question, taught)
        row.taught = found[0] if found else None

    stats = {
        "الأجهزة": devices.count(),
        "متصل الآن": sum(1 for d in devices if d.is_online),
        "مسح (30 يوم)": RobotScanEvent.objects.filter(created_at__gte=since).count(),
        "إدخال (30 يوم)": RobotScanEvent.objects.filter(purpose="intake", created_at__gte=since).count(),
        "مبيعات (30 يوم)": RobotScanEvent.objects.filter(
            sale_invoice__isnull=False, created_at__gte=since).count(),
        "إشارات توريد": ProcurementSignal.objects.filter(status="open").count(),
        "معلومات اتعلمها": RobotKnowledge.objects.count(),
        "أسئلة ماعرفهاش": unanswered_qs.count(),
    }

    from inventory.models import Branch
    with_robot = {d.branch_id for d in devices}
    # The branches with no robot yet first: the form picks the first.
    branches = sorted(({"branch": b, "has_robot": b.pk in with_robot} for b in Branch.objects.order_by("id")),
                      key=lambda row: row["has_robot"])
    from . import health
    from .brain import VERSION
    for d in devices:                  # «🆕»: a brain older than the one this server ships
        fw = d.firmware_version or ""
        d.brain_behind = fw.startswith("brain-") and _version(fw) < _version(VERSION)
    return render(request, "robot/dashboard.html", {
        "branches": branches,
        "devices": devices, "scans": scans, "intakes": intakes,
        "robot_invoices": robot_invoices, "movements": movements,
        "expenses": expenses, "voice": voice, "access": access,
        "signals": signals, "knowledge": knowledge, "stock_takes": stock_takes,
        "motors": motors, "stats": stats, "unanswered": unanswered,
        "taught": request.session.pop("robot_taught", None),
        "services": health.report(), "latest_brain": VERSION,
    })


def _version(text: str) -> tuple:
    """«brain-0.9.3» → (0, 9, 3) (robot/brain/loop._version)."""
    import re

    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


@login_required(login_url="/login/")
@role_required("admin", "manager")
def teach(request):
    """Supervisor teaches the robot from its "questions I couldn't answer"
    list: what to answer to it — any reply, said as written (a «🗨️ الردود
    الجاهزة» reply: by voice and offline) — or which part a word means
    ("<phrase> means <part number>"). A reply already taught is edited here.

    Every gap a customer hits becomes something the robot answers next time.
    """
    from django.utils.translation import gettext as _

    from . import services
    if request.method != "POST":
        return redirect("robot_ui:dashboard")
    phrase = _without_name(request.POST.get("phrase"), RobotDevice.objects.all())
    answer = " ".join((request.POST.get("answer") or "").split())
    part_number = (request.POST.get("part_number") or "").strip()
    if answer:
        reply = (RobotReply.objects.filter(pk=request.POST.get("reply_id") or None).first()
                 or _written_for(phrase))       # the same question again: its reply changes
        if not phrase and reply is None:
            message = _("⚠️ اكتب الناس بتقول إيه.")
        elif len(answer) > shop.MOST_CHARS:
            message = _("⚠️ الرد طويل أوي: خليه أقل من %(chars)s حرف.") % {"chars": shop.MOST_CHARS}
        else:
            if reply is None:
                reply = RobotReply(topic="other", questions=phrase)
            elif phrase and matcher.match(phrase, [_as_matched(reply)]) is None:
                reply.questions = f"{reply.questions}\n{phrase}"     # asked another way: that too
            reply.answer = answer
            reply.is_active = True
            reply.updated_by = request.user
            reply.save()
            message = _("✅ اتحفظ: لما حد يقول «%(q)s» هيرد «%(a)s».") % {
                "q": phrase or matcher.questions(reply.questions)[0], "a": answer}
    elif part_number:
        product = services.find_product(part_number)
        if len(phrase) >= 3 and product is not None:
            services.learn_from_confirmation(
                product=product, label=phrase, details={"source": "dashboard"},
            )
            message = f"✅ اتعلم: «{phrase}» = {product.name}"
        else:
            message = _("⚠️ لازم عبارة (٣ حروف+) ورقم قطعة موجود.")
    else:
        message = _("⚠️ اكتب الرد اللي الروبوت هيقوله، أو رقم القطعة لو الكلمة اسم قطعة.")
    request.session["robot_taught"] = message
    return redirect("robot_ui:dashboard")


def _without_name(text, devices) -> str:
    """What was asked without «يا موس» (any robot's name): the question a
    reply is taught for, as the robot matches it once it's been called."""
    for device in devices:
        addressed, rest = wakename.strip_name(device, text or "")
        if addressed:
            return rest
    return " ".join((text or "").split()).strip(" ،,.!؟?:-")


def _written_for(phrase):
    """The reply that has `phrase` as one of its questions, or None: a second
    reply to the same question would never be said (the first wins a tie)."""
    asked = matcher.normalize(phrase)
    if not asked:
        return None
    for reply in RobotReply.objects.order_by("id"):
        if asked in {matcher.normalize(q) for q in matcher.questions(reply.questions)}:
            return reply
    return None


def _as_matched(reply) -> dict:
    return {"id": reply.pk, "questions": matcher.questions(reply.questions), "answer": reply.answer}


def _replies_to_match() -> list:
    """Every reply written, whatever its branch, as the matcher takes them."""
    return [_as_matched(r) for r in RobotReply.objects.filter(is_active=True).exclude(answer="")
            .order_by("branch_id", "id")]


@login_required(login_url="/login/")
@role_required("admin", "manager")
def apply_stock_take(request, pk):
    """Supervisor approves a stock-take: correct inventory to the counted numbers."""
    from . import services
    session = get_object_or_404(RobotStockTakeSession, pk=pk)
    if request.method == "POST":
        services.apply_stock_take(session)
    return redirect("robot_ui:dashboard")


@login_required(login_url="/login/")
@role_required("admin", "manager")
def device_profile(request, pk):
    """View + edit one robot's profile (name, firmware, active, rotate token)."""
    device = get_object_or_404(RobotDevice, pk=pk)

    if request.method == "POST":
        device.name = request.POST.get("name", device.name).strip() or device.name
        device.is_active = request.POST.get("is_active") == "on"
        fw = request.POST.get("firmware_version", "").strip()
        if fw:
            device.firmware_version = fw[:20]
        # The name it answers to, other spellings STT may produce, and
        # whether it answers only when called by name.
        wake = (request.POST.get("wake_name") or "").strip()
        if wake:
            device.wake_name = wake[:40]
        aliases = request.POST.get("wake_aliases")
        if aliases is not None:
            device.wake_aliases = [a.strip() for a in aliases.replace("،", ",").split(",")
                                   if a.strip()][:20]
        device.wake_required = request.POST.get("wake_required") == "on"
        # Optional branch reassignment.
        branch_id = request.POST.get("branch")
        if branch_id:
            from inventory.models import Branch
            b = Branch.objects.filter(pk=branch_id).first()
            if b:
                device.branch = b
        # Optional token rotation. The new token is shown once, on the next
        # render, so it can be copied into the firmware — it is never rendered
        # again afterwards.
        if request.POST.get("rotate_token") == "on":
            _hand_over_token(request, device, device.issue_token())
        device.save()
        return redirect("robot_ui:device_profile", pk=device.pk)

    from inventory.models import Branch
    # The device token is the robot's whole credential, so the page shows only
    # its last 4 characters. A freshly rotated one is handed over once here and
    # then dropped from the session.
    new_token = request.session.pop(f"robot_new_token_{device.pk}", None)
    return render(request, "robot/device_profile.html", {
        "device": device,
        # The brain's zip with its key: only until it's downloaded, right after the key was made.
        "brain_with_key": bool(request.session.get(_BRAIN_TOKEN.format(device.pk))),
        "branches": Branch.objects.all(),
        # Only a hash is stored now, so there's no plaintext tail to hint at.
        "token_hint": "(متخزّن مشفّر — لو ضاع اعمل تدوير)",
        "wake_aliases_text": ", ".join(device.wake_aliases or []),
        "new_token": new_token,
        "recent_scans": device.scans.order_by("-created_at")[:15],
        "recent_access": device.access_logs.order_by("-created_at")[:15],
    })


# ---------------------------------------------------------------------------
# A robot in every branch: added here, its brain downloaded from its page
# ---------------------------------------------------------------------------

_BRAIN_TOKEN = "robot_brain_token_{}"


def _hand_over_token(request, device, token: str) -> None:
    """`device`'s new key: shown once on its page, and put in its brain's
    zip until that's downloaded (only the hash is stored)."""
    request.session[f"robot_new_token_{device.pk}"] = token
    request.session[_BRAIN_TOKEN.format(device.pk)] = token


@login_required(login_url="/login/")
@role_required("admin", "manager")
def new_device(request):
    """➕ a robot for a branch — then its page, with its brain to download.
    Before this a robot was added with a command over SSH, so it stayed in
    the main branch only."""
    if request.method != "POST":
        return redirect("robot_ui:dashboard")
    from django.utils.crypto import get_random_string
    from django.utils.translation import gettext as _

    from inventory.models import Branch
    branch = Branch.objects.filter(pk=request.POST.get("branch") or 0).first()
    if branch is None:                       # the form's list is required: only a stale page gets here
        return redirect("robot_ui:dashboard")
    name = " ".join((request.POST.get("name") or "").split())[:80] or _("روبوت %(branch)s") % {
        "branch": branch.name}
    device = RobotDevice(name=name, branch=branch, device_uid=f"ROBOT-{get_random_string(10).upper()}")
    token = device.issue_token()
    device.save()
    _hand_over_token(request, device, token)
    return redirect("robot_ui:device_profile", pk=device.pk)


@login_required(login_url="/login/")
@role_required("admin", "manager")
def delete_device(request, pk):
    """🗑️ a robot registered by mistake (a double click on «سجّل الروبوت») —
    only one that never connected: one that did has its history (voice,
    scans, sales) and is turned off from its page instead."""
    if request.method == "POST":
        RobotDevice.objects.filter(pk=pk, last_seen_at__isnull=True).delete()
    return redirect("robot_ui:dashboard")


@login_required(login_url="/login/")
@role_required("admin", "manager")
def brain_zip(request, pk):
    """The robot's brain for its branch's computer: with its brain.ini and key
    right after the key was made (once), else the brain alone — an update
    that keeps the computer's own brain.ini."""
    from . import brain_package

    device = get_object_or_404(RobotDevice, pk=pk)
    token = request.session.pop(_BRAIN_TOKEN.format(device.pk), "") if request.GET.get("key") else ""
    api_base = f"https://{request.get_host()}/api/robot/v1"
    data = brain_package.build(api_base, token, device.wake_name or "موس")
    name = "mouss-brain.zip" if token else "mouss-brain-update.zip"
    resp = HttpResponse(data, content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    resp["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Live camera + full remote control (owner/manager)
# ---------------------------------------------------------------------------

@login_required(login_url="/login/")
@role_required("owner", "admin", "manager")
def device_control(request, pk):
    """Live camera view + full remote control of one robot.

    The camera runs 24/7; this page shows its latest pushed frame (auto-
    refreshing) and lets a supervisor snapshot, pan/move the head/arms/tracks,
    make it speak, and (owner/admin) page an employee — all by queuing commands
    the device polls.
    """
    device = get_object_or_404(RobotDevice, pk=pk)

    if request.method == "POST":
        action = request.POST.get("action", "")
        user = request.user
        if action == "snapshot":
            RobotCommand.objects.create(device=device, kind="snapshot",
                                        issued_by=user, payload={"reason": "manual"})
        elif action == "say":
            text = (request.POST.get("text") or "").strip()
            if text:
                RobotCommand.objects.create(device=device, kind="say",
                                            issued_by=user, payload={"text": text})
        elif action == "move":
            # Low-level articulation → MotorCommandLog (ESP32 polls /motor/pending/).
            # Same validation/clamp as the device API: a typo'd duration must
            # not become a 500, nor an unbounded motor run.
            from . import services
            try:
                actuator, direction, duration_ms = services.validate_motor_command(
                    request.POST.get("actuator"), request.POST.get("direction"),
                    request.POST.get("duration_ms", 500),
                )
            except ValueError:
                return redirect("robot_ui:device_control", pk=device.pk)
            MotorCommandLog.objects.create(
                device=device, actuator=actuator, direction=direction,
                duration_ms=duration_ms, issued_by=user,
            )
        elif action in ("stream_start", "stream_stop"):
            # Raise/lower the camera's push rate while a viewer is watching, so
            # the MJPEG is smooth on demand without pushing hard all day. The
            # camera reads the resulting cadence from its /camera/frame/ reply.
            if action == "stream_start":
                device.stream_until = timezone.now() + timedelta(minutes=3)
            else:
                device.stream_until = None
            device.save(update_fields=["stream_until"])
            RobotCommand.objects.create(device=device, kind=action, issued_by=user)
        return redirect("robot_ui:device_control", pk=device.pk)

    from hr.models import Employee
    return render(request, "robot/device_control.html", {
        "device": device,
        "employees": Employee.objects.select_related("user")[:200],
        "recent_snapshots": device.snapshots.order_by("-created_at")[:12],
        "recent_commands": device.commands.order_by("-created_at")[:15],
        "recent_motion": device.alerts.order_by("-created_at")[:10],
        "motor_health": device.motor_health or {},
        "motor_runtime": _safe_runtime(device),
        # Owner/admin may page staff.
        "can_page": _user_role(request) in ("owner", "admin"),
    })


def _safe_runtime(device) -> dict:
    from . import services
    try:
        return services.motor_runtime_ms(device)
    except Exception:
        return {}


def _user_role(request) -> str:
    prof = getattr(getattr(request, "user", None), "employee_profile", None)
    if getattr(request.user, "is_superuser", False):
        return "owner"
    return getattr(prof, "role", "") or ""


@login_required(login_url="/login/")
@role_required("owner", "admin", "manager")
def face_enrollment(request, pk):
    """Staff face enrollment: the robot calls employees by name, one by one.

    GET shows who is enrolled and the live progress of the current round
    (auto-refreshing while it runs). POST actions: start (selected employees,
    default = everyone without a face), skip (the one being called now),
    cancel.
    """
    from hr.models import Employee
    from . import enrollment

    device = get_object_or_404(RobotDevice, pk=pk)
    session = enrollment.active_session(device)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "start":
            ids = request.POST.getlist("employees")
            try:
                enrollment.start(device, employee_ids=ids or None,
                                 only_missing=not ids, user=request.user)
            except enrollment.EnrollmentUnavailable as exc:
                request.session["robot_enroll_error"] = str(exc)
        elif action == "skip" and session is not None:
            enrollment.skip_current(session, note="اتخطّى من اللوحة", user=request.user)
        elif action == "cancel" and session is not None:
            enrollment.cancel(session, user=request.user)
        return redirect("robot_ui:face_enrollment", pk=device.pk)

    last = session or (RobotFaceEnrollment.objects.filter(device=device)
                       .order_by("-created_at").first())
    snapshot_ids = [e.get("snapshot_id") for e in (last.entries if last else [])
                    if e.get("snapshot_id")]
    snaps = {s.pk: s for s in RobotSnapshot.objects.filter(pk__in=snapshot_ids)}
    entries = []
    for e in (last.entries if last else []):
        row = dict(e)
        row["sample_count"] = len(e.get("samples") or [])
        row["snapshot"] = snaps.get(e.get("snapshot_id"))
        entries.append(row)
    employees = list(Employee.objects.select_related("user")
                     .order_by("user__first_name", "user__username")[:300])
    return render(request, "robot/face_enrollment.html", {
        "device": device,
        "session": session,
        "last": last,
        "entries": entries,
        "employees": employees,
        "enrolled_count": sum(1 for e in employees if e.face_encoding),
        "error": request.session.pop("robot_enroll_error", None),
        "samples_needed": enrollment.SAMPLES_NEEDED,
    })


@login_required(login_url="/login/")
@role_required("owner", "admin", "manager")
def live_frame(request, pk):
    """Serve the device's latest live JPEG frame (for the auto-refreshing <img>).

    Same roles as `device_control`, which is the page that shows it: this is a
    live camera in the shop, and gating the page while leaving its image URL
    open would let any logged-in employee watch the floor by guessing the URL.
    """
    device = get_object_or_404(RobotDevice, pk=pk)
    data = _read_frame_bytes(device)
    if data is None:
        return HttpResponse(status=204)
    return HttpResponse(data, content_type="image/jpeg")


def _read_frame_bytes(device):
    """Return the latest live-frame bytes, opening the file fresh, or None.

    Opening fresh (rather than `.read()` on a possibly-EOF/closed FieldFile)
    makes repeated reads for the live view reliable, and tolerates the frame
    being swapped out from under us by a concurrent camera push.
    """
    if not device.last_frame:
        return None
    try:
        f = device.last_frame.open("rb")
        try:
            return f.read()
        finally:
            f.close()
    except Exception:
        return None


# MJPEG viewing-session bounds. Module-level so a forgotten tab can't hold a
# stream open forever, and so tests can shorten a session instead of waiting.
_MJPEG_MAX_SECONDS = 90     # the <img> reconnects on its own afterwards
_MJPEG_FPS = 5
_MJPEG_BOUNDARY = "moussframe"


def _mjpeg_part(data: bytes) -> bytes:
    return (b"--" + _MJPEG_BOUNDARY.encode() + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
            + data + b"\r\n")


def _keep_camera_fast(pk):
    """A viewer opened the stream: the camera pushes at its fast cadence (it
    reads it back from every /camera/frame/ reply) for as long as the stream
    lasts. The <img> reconnects after each window and extends it; once the
    page is closed the camera drops back to its idle cadence shortly after.
    Without this the smooth stream re-sent one new frame every 1.5 s.
    """
    RobotDevice.objects.filter(pk=pk).update(
        stream_until=timezone.now() + timedelta(seconds=_MJPEG_MAX_SECONDS + 15))


def _next_frame(pk, schema_name):
    """Read the device's latest frame. Runs off the event loop, in a worker
    thread whose connection is bound to whatever schema served the last request
    there — so re-enter this tenant's schema explicitly rather than inheriting
    it. Without this a viewer could be served another tenant's camera.
    """
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        dev = (RobotDevice.objects.filter(pk=pk)
               .only("id", "last_frame", "last_frame_at").first())
        if dev is None:
            return None, None
        return _read_frame_bytes(dev), dev.last_frame_at


@login_required(login_url="/login/")
@role_required("owner", "admin", "manager")
def live_mjpeg(request, pk):
    """Smooth live video as an MJPEG (multipart/x-mixed-replace) stream.

    Re-reads the device's latest pushed frame a few times a second and yields it
    as an MJPEG part, so a plain <img> shows near-real-time video. Same
    owner/admin/manager gate as the control page that displays it.

    The generator is async on purpose. This project serves over ASGI (daphne),
    and Django hands a *sync* iterator on a StreamingHttpResponse to
    `sync_to_async(list)` — it drains the whole generator before sending a
    single byte, which turns a 90-second live stream into a 90-second wait
    followed by 90 seconds of stale frames held in memory, on a blocked
    thread-sensitive worker. An async generator is streamed part by part as
    intended, and `asyncio.sleep` leaves the loop free between frames.
    """
    import asyncio
    from asgiref.sync import sync_to_async
    from django.db import connection
    from django.http import StreamingHttpResponse

    # Captured here, while we are still inside the request's tenant binding.
    schema_name = connection.schema_name
    _keep_camera_fast(pk)
    get_frame = sync_to_async(_next_frame, thread_sensitive=True)

    async def generator():
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _MJPEG_MAX_SECONDS
        last_sent_at = None
        while loop.time() < deadline:
            try:
                data, stamp = await get_frame(pk, schema_name)
            except Exception:
                data, stamp = None, None
            # Only push when there's a newer frame (a still shop sends nothing).
            if data and stamp != last_sent_at:
                last_sent_at = stamp
                yield _mjpeg_part(data)
            await asyncio.sleep(1.0 / _MJPEG_FPS)

    resp = StreamingHttpResponse(
        generator(),
        content_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
    )
    resp["Cache-Control"] = "no-cache, no-store"
    return resp


@login_required(login_url="/login/")
@role_required("owner", "admin")
def page_employee(request, pk):
    """OWNER/ADMIN only: make the robot call an employee to the office.

    "أنا بس اللي أعمل كده" — restricted to owner/admin. Creates a RobotPageCall
    and a queued `page` command the robot announces by voice.
    """
    device = get_object_or_404(RobotDevice, pk=pk)
    if request.method == "POST":
        from hr.models import Employee
        emp = Employee.objects.filter(pk=request.POST.get("employee")).first()
        if emp:
            msg = (request.POST.get("message") or "").strip() or \
                f"{emp.name}، مطلوب في المكتب من فضلك."
            page = RobotPageCall.objects.create(
                device=device, target_employee=emp, message=msg,
                created_by=request.user,
            )
            RobotCommand.objects.create(
                device=device, kind="page", issued_by=request.user,
                payload={"page_id": page.id, "employee": emp.name, "text": msg},
            )
    return redirect("robot_ui:device_control", pk=device.pk)


@login_required(login_url="/login/")
@role_required("admin", "manager")
def resolve_signal(request, pk):
    """Close a low-stock signal: ordered ("queued") or not needed ("dismissed").

    Closing it lets the robot raise a fresh one the next time the part runs
    low (only one OPEN signal per part/branch is allowed).
    """
    if request.method == "POST":
        status = request.POST.get("status")
        if status in ("queued", "dismissed", "acknowledged"):
            ProcurementSignal.objects.filter(pk=pk, status="open").update(
                status=status, resolved_at=timezone.now(),
            )
    return redirect("robot_ui:dashboard")


@login_required(login_url="/login/")
@role_required("admin", "manager")
def alerts(request):
    """After-hours motion + connectivity alerts feed; POST marks one read."""
    if request.method == "POST":
        RobotAlert.objects.filter(pk=request.POST.get("alert_id")).update(is_read=True)
        return redirect("robot_ui:alerts")
    return render(request, "robot/alerts.html", {
        "alerts": RobotAlert.objects.select_related("device", "snapshot")
                  .order_by("-created_at")[:100],
        "unread": RobotAlert.objects.filter(is_read=False).count(),
    })


@login_required(login_url="/login/")
@role_required("admin", "manager")
def customers(request):
    """Customer memory: who the robot greeted, how often, and when last seen."""
    q = (request.GET.get("q") or "").strip()
    rows = RobotCustomerFace.objects.select_related("customer").order_by("-last_seen_at")
    if q:
        rows = rows.filter(customer__name__icontains=q)
    return render(request, "robot/customers.html", {
        "rows": rows[:200],
        "q": q,
        "total": RobotCustomerFace.objects.count(),
    })


@login_required(login_url="/login/")
@role_required("admin", "manager")
def restock_requests(request):
    """«أبلغك أول ما ييجي؟»: customers waiting for a part — the ones whose
    part is back first, each with its WhatsApp message ready. POST marks one
    sent («اتبلّغ») or cancelled."""
    from . import restock

    if request.method == "POST":
        status = request.POST.get("status")
        if status in ("notified", "cancelled"):
            RobotRestockRequest.objects.filter(
                pk=request.POST.get("request_id"), status__in=("waiting", "arrived"),
            ).update(status=status, notified_at=timezone.now() if status == "notified" else None)
        return redirect("robot_ui:restock")
    rows = list(RobotRestockRequest.objects.filter(status__in=("arrived", "waiting"))
                .select_related("product", "branch", "customer")
                .order_by("status", "-created_at")[:300])      # «arrived» sorts before «waiting»
    for row in rows:
        row.whatsapp = restock.whatsapp_link(row) if row.status == "arrived" else ""
    return render(request, "robot/restock.html", {
        "rows": rows,
        "arrived": sum(1 for r in rows if r.status == "arrived"),
        "done": RobotRestockRequest.objects.filter(status="notified").count(),
    })


@login_required(login_url="/login/")
@role_required("admin", "manager")
def staff_pins(request):
    """«🔢 أرقام الكاشير»: the PIN each employee types on «مخ موس»'s counter
    page (cash sales, expenses, counts with the net down). Only the hash is
    kept (robot/brain/pins.py); the robot gets it with its catalog."""
    from hr.models import Employee

    from . import permissions
    from .brain import pins

    message, error = "", ""
    if request.method == "POST":
        employee = Employee.objects.filter(pk=request.POST.get("employee_id")).first()
        if employee is None:
            error = "الموظف مش موجود."
        elif request.POST.get("remove"):
            RobotStaffPin.objects.filter(employee=employee).delete()
            message = f"اتشال الرقم السري بتاع {employee.name}."
        else:
            pin = (request.POST.get("pin") or "").strip()
            if not pins.valid(pin):
                error = f"الرقم السري لازم يكون من {pins.MIN_DIGITS} لـ {pins.MAX_DIGITS} أرقام."
            else:
                RobotStaffPin.objects.update_or_create(
                    employee=employee, defaults={"pin_hash": pins.hash_pin(pin), "set_by": request.user})
                message = (f"اتحفظ الرقم السري بتاع {employee.name}. الروبوت هياخده مع الكتالوج "
                           "(كل ربع ساعة وهو على النت).")
    with_pin = set(RobotStaffPin.objects.values_list("employee_id", flat=True))
    rows = []
    for employee in Employee.objects.select_related("user").order_by("user__first_name", "user__username"):
        if employee.user is not None and not employee.user.is_active:
            continue
        can = [permissions.action_label(a) for a in ("sale", "expense", "stock_take")
               if permissions.employee_can(employee, a)]
        rows.append({"employee": employee, "has_pin": employee.pk in with_pin, "can": can})
    return render(request, "robot/staff_pins.html", {"rows": rows, "message": message, "error": error})


@login_required(login_url="/login/")
@role_required("admin", "manager")
def shop_replies(request):
    """«🗨️ الردود الجاهزة»: what the robot answers to the hours, the address,
    the phone, paying, the warranty, returns and delivery — the questions as
    people ask them, one per line, and the reply. Said at once by voice
    (robot/replies.py), and by «مخ موس» with the net down: it gets them with
    its catalog. The page suggests the questions; the answer is the owner's."""
    from django.utils.translation import gettext as _

    from inventory.models import Branch

    topics = dict(RobotReply.TOPIC)
    message, error = "", ""
    if request.method == "POST":
        reply = RobotReply.objects.filter(pk=request.POST.get("reply_id") or None).first()
        if request.POST.get("remove"):
            if reply is not None:
                reply.delete()
            message = _("اتشال الرد.")
        else:
            questions = matcher.questions(request.POST.get("questions"))
            answer = " ".join((request.POST.get("answer") or "").split())
            topic = request.POST.get("topic")
            branch_id = request.POST.get("branch_id") or None
            branch = Branch.objects.filter(pk=branch_id).first() if branch_id else None
            if not questions:
                error = _("اكتب سؤال واحد على الأقل، كل سؤال في سطر.")
            elif not answer:
                error = _("اكتب الرد اللي الروبوت هيقوله.")
            elif len(answer) > shop.MOST_CHARS or len(questions) > shop.MOST_QUESTIONS:
                error = _("الرد طويل أوي: خليه أقل من %(chars)s حرف و%(lines)s سؤال.") % {
                    "chars": shop.MOST_CHARS, "lines": shop.MOST_QUESTIONS}
            elif branch_id and branch is None:
                error = _("الفرع مش موجود.")
            else:
                reply = reply or RobotReply()
                reply.topic = topic if topic in topics else "other"
                reply.questions = "\n".join(questions)
                reply.answer = answer
                reply.branch = branch
                reply.is_active = bool(request.POST.get("is_active"))
                reply.updated_by = request.user
                reply.save()
                message = _("اتحفظ الرد. الروبوت هيقوله من دلوقتي، وجهاز المحل هياخده مع الكتالوج في خلال دقيقة.")

    saved = list(RobotReply.objects.select_related("branch").order_by("topic", "branch_id", "id"))
    written = {(r.topic, r.branch_id) for r in saved}
    branches = list(Branch.objects.order_by("id"))
    suggestions = []
    for topic, questions in shop.SUGGESTED:
        per_branch = topic in shop.PER_BRANCH
        for branch in (branches if per_branch else [None]):
            if (topic, None) in written or (topic, getattr(branch, "pk", None)) in written \
                    or (not per_branch and any(t == topic for t, _b in written)):
                continue                             # written already, for the shop or this branch
            # Only the branch's own address / phone is filled in — in Arabic
            # whatever the page's language: it's what the robot will say.
            answer = ""
            # 🐛 [FIX]: «رقم فرع الفرع الرئيسي»: الفرع اللي اسمه أوله «فرع»/«الفرع» مبيتقالش قبله «فرع» تاني.
            named = branch.name if branch is not None and branch.name.strip().startswith(("فرع", "الفرع")) \
                else f"فرع {getattr(branch, 'name', '')}"
            if branch is not None and topic == "address" and branch.location:
                answer = f"عنوان {named}: {branch.location}."
            if branch is not None and topic == "phone" and branch.phone:
                answer = f"رقم {named}: {branch.phone}."
            suggestions.append({"topic": topic, "label": topics[topic], "branch": branch,
                                "questions": "\n".join(questions), "answer": answer})
    return render(request, "robot/replies.html", {
        "saved": saved, "suggestions": suggestions, "branches": branches, "topics": RobotReply.TOPIC,
        "message": message, "error": error, "most_chars": shop.MOST_CHARS,
    })


@login_required(login_url="/login/")
@role_required("admin", "manager")
def hearing(request):
    """«🎓 تعلّم السمع»: the sentences the robot wasn't sure it heard, what a
    teacher (Azure / Google) heard in them, and what that taught it — each
    lesson can be turned off. The minutes each teacher used this month, of
    its budget (robot/teachers.py)."""
    import math

    from django.utils.translation import gettext as _

    from . import teachers

    message = ""
    if request.method == "POST" and request.POST.get("sentence"):
        # A whole sentence turned off: kept as a lesson that's off, so it isn't sent again.
        heard = " ".join(request.POST["sentence"].split())[:120]
        RobotHeardFix.objects.update_or_create(
            heard=heard, means=" ".join(request.POST.get("means", "").split())[:120],
            defaults={"is_active": False})
        message = _("الجملة اتقفلت: الروبوت مش هيستعملها.")
    elif request.method == "POST":
        fix = RobotHeardFix.objects.filter(pk=request.POST.get("fix_id") or None).first()
        if fix is not None and request.POST.get("remove"):
            fix.delete()
            message = _("اتشال التصحيح.")
        elif fix is not None:
            fix.is_active = not fix.is_active
            fix.save(update_fields=["is_active", "updated_at"])
            message = _("التصحيح اتشغّل.") if fix.is_active else _("التصحيح اتقفل: الروبوت مش هيستعمله.")
    ready = teachers.configured()
    # Whole minutes, the used ones rounded up: what's left is never shown as more than it is.
    budget = [{"name": name, "ready": name in ready, "free": int(teachers.free_minutes(name)),
               "used": math.ceil(teachers.used_seconds(name) / 60)} for name in teachers.TEACHERS]
    return render(request, "robot/hearing.html", {
        "budget": budget, "message": message, "seen_to_use": teachers.SEEN_TO_USE,
        "fixes": RobotHeardFix.objects.all()[:200],
        "sentences": teachers.sentences()[:100],
        "quality": teachers.hearing_quality(),
        "training": teachers.training_summary(),
        "recent": RobotHeard.objects.select_related("device")[:40],
    })


@login_required(login_url="/login/")
@role_required("admin", "manager")
def hearing_dataset(request):
    """«📦 بيانات تدريب موديل المحل»: every recording a teacher wrote words for,
    with those words — a zip for training a model of the shop's own (robot/teachers.training_zip).
    Streamed: hours of recordings never sit whole in the server's memory."""
    from django.core.handlers.asgi import ASGIRequest
    from django.http import StreamingHttpResponse
    from django.utils.http import content_disposition_header

    from . import teachers, services

    chunks = teachers.training_zip()
    if isinstance(request, ASGIRequest):
        # 🐛 [FIX]: under daphne (ASGI) Django 4.2 collects a sync iterator whole
        # (sync_to_async(list)) before the first byte goes out — every recording in the
        # server's memory. An async iterator streams; each piece made on the request's thread.
        chunks = _streamed(chunks)
    response = StreamingHttpResponse(chunks, content_type="application/zip")
    shop = "".join(c for c in (services.shop_name() or "shop") if c.isalnum() or c in "-_") or "shop"
    # 🐛 [FIX]: an Arabic shop name made Django MIME-encode the whole header («=?utf-8?b?…»):
    # the browser lost the file's name. RFC 5987 (filename*) carries it.
    response["Content-Disposition"] = content_disposition_header(
        True, f"mouss-hearing-{shop}-{timezone.localdate():%Y%m%d}.zip")
    return response


async def _streamed(chunks):
    """A sync generator's pieces as an async iterator, each made on the request's
    own thread (its database connection, its shop's schema)."""
    from asgiref.sync import sync_to_async

    end = object()
    step = sync_to_async(next, thread_sensitive=True)
    while True:
        piece = await step(chunks, end)
        if piece is end:
            return
        yield piece
