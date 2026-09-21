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
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from datetime import timedelta

from inventory.views.utils import role_required

from .models import (
    MotorCommandLog, ProcurementSignal, RobotAccessLog, RobotDevice,
    RobotKnowledge, RobotScanEvent, RobotStockTakeSession, RobotVoiceInteraction,
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

    stats = {
        "devices": devices.count(),
        "online": sum(1 for d in devices if d.is_online),
        "scans_30d": RobotScanEvent.objects.filter(created_at__gte=since).count(),
        "intakes_30d": RobotScanEvent.objects.filter(purpose="intake", created_at__gte=since).count(),
        "sales_30d": RobotScanEvent.objects.filter(
            sale_invoice__isnull=False, created_at__gte=since).count(),
        "open_signals": ProcurementSignal.objects.filter(status="open").count(),
        "learned": RobotKnowledge.objects.count(),
    }

    return render(request, "robot/dashboard.html", {
        "devices": devices, "scans": scans, "intakes": intakes,
        "robot_invoices": robot_invoices, "movements": movements,
        "expenses": expenses, "voice": voice, "access": access,
        "signals": signals, "knowledge": knowledge, "stock_takes": stock_takes,
        "motors": motors, "stats": stats,
    })


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
