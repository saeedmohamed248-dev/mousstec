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

from .models import (
    MotorCommandLog, ProcurementSignal, RobotAccessLog, RobotAlert,
    RobotCommand, RobotCustomerFace, RobotDevice, RobotKnowledge, RobotPageCall,
    RobotScanEvent, RobotSnapshot, RobotStockTakeSession, RobotVoiceInteraction,
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

    # Expenses / purchases the robot's intake created, if any (best-effort).
    expenses = _robot_expenses(since)

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
    unanswered = unanswered_qs[:30]

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

    return render(request, "robot/dashboard.html", {
        "devices": devices, "scans": scans, "intakes": intakes,
        "robot_invoices": robot_invoices, "movements": movements,
        "expenses": expenses, "voice": voice, "access": access,
        "signals": signals, "knowledge": knowledge, "stock_takes": stock_takes,
        "motors": motors, "stats": stats, "unanswered": unanswered,
        "taught": request.session.pop("robot_taught", None),
    })


@login_required(login_url="/login/")
@role_required("admin", "manager")
def teach(request):
    """Supervisor teaches the robot an alias: "<phrase> means <part number>".

    The dashboard's answer to its "questions I couldn't answer" list — every
    gap a customer hits becomes a word the robot understands next time (by
    voice, in sentences, and offline via the synced catalog).
    """
    from . import services
    if request.method == "POST":
        phrase = (request.POST.get("phrase") or "").strip()
        product = services.find_product((request.POST.get("part_number") or "").strip())
        if len(phrase) >= 3 and product is not None:
            services.learn_from_confirmation(
                product=product, label=phrase, details={"source": "dashboard"},
            )
            request.session["robot_taught"] = f"✅ اتعلم: «{phrase}» = {product.name}"
        else:
            request.session["robot_taught"] = "⚠️ لازم عبارة (٣ حروف+) ورقم قطعة موجود."
    return redirect("robot_ui:dashboard")


def _robot_expenses(since):
    """Best-effort list of expenses/purchases attributable to the robot.

    Kept defensive: the finance model name can vary by deployment, so we return
    an empty list rather than break the page if it isn't present.
    """
    try:
        from inventory.models import PurchaseInvoice
        return (PurchaseInvoice.objects.filter(date_created__gte=since)
                .order_by("-date_created")[:20])
    except Exception:
        return []


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
            from django.utils.crypto import get_random_string
            device.api_token = get_random_string(48)
            request.session[f"robot_new_token_{device.pk}"] = device.api_token
        device.save()
        return redirect("robot_ui:device_profile", pk=device.pk)

    from inventory.models import Branch
    # The device token is the robot's whole credential, so the page shows only
    # its last 4 characters. A freshly rotated one is handed over once here and
    # then dropped from the session.
    new_token = request.session.pop(f"robot_new_token_{device.pk}", None)
    return render(request, "robot/device_profile.html", {
        "device": device,
        "branches": Branch.objects.all(),
        "token_hint": (device.api_token or "")[-4:],
        "new_token": new_token,
        "recent_scans": device.scans.order_by("-created_at")[:15],
        "recent_access": device.access_logs.order_by("-created_at")[:15],
    })


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
        "employees": Employee.objects.all()[:200],
        "recent_snapshots": device.snapshots.order_by("-created_at")[:12],
        "recent_commands": device.commands.order_by("-created_at")[:15],
        "recent_motion": device.alerts.order_by("-created_at")[:10],
        # Owner/admin may page staff.
        "can_page": _user_role(request) in ("owner", "admin"),
    })


def _user_role(request) -> str:
    prof = getattr(getattr(request, "user", None), "employee_profile", None)
    if getattr(request.user, "is_superuser", False):
        return "owner"
    return getattr(prof, "role", "") or ""


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
