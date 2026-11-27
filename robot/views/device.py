"""📡 API الروبوت — نبض الجهاز وحالته، القياسات، الصور، الأوامر المعلّقة، المزامنة، تسجيل الوشوش."""

from __future__ import annotations


from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from .. import enrollment, restock, services
from ..models import (
    RobotSnapshot,
)
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from ._common import _robot_endpoint
from .speech import _recorded_at


@_robot_endpoint
def heartbeat(request):
    device, err = views._device_or_401(request)
    if err:
        return err
    fw = request.data.get("firmware_version")
    if fw:
        device.firmware_version = str(fw)[:20]
        device.save(update_fields=["firmware_version"])
    # A part customers were waiting for is back: tell the staff. Checked here
    # (every 30 s) so any way the stock rose — a purchase, a transfer, a count
    # — is seen. An extra: the heartbeat answers regardless.
    try:
        restock.sweep(device)
    except Exception:
        services.logger.warning("robot heartbeat: restock sweep failed", exc_info=True)
    return Response({
        "ok": True,
        "device": device.name,
        "branch": device.branch.name,
        "server_time": timezone.now().isoformat(),
        # The bridge turns on its fast voice only when the server can take it:
        # µ-law both ways, and /voice/ answering with the spoken reply itself.
        "voice": {"ulaw": True},
    })


@_robot_endpoint
def service_status(request):
    """«🩺 صحة الخدمات» for «مخ موس»: is each online service answering (Azure,
    Google, Gemini), why not, what to do, the minutes left — said in its log
    when it starts and when the net comes back. And the brain this server
    ships (robot/brain/__init__.VERSION): «a newer brain is on the dashboard»."""
    device, err = views._device_or_401(request)
    if err:
        return err
    from .. import health
    from ..brain import VERSION
    return Response({"services": health.report(), "brain": VERSION})


@_robot_endpoint
def brain_update(request):
    """«مخ موس» updating itself (robot/brain/selfupdate.py): the brain this
    server ships — the dashboard's «⬇️ تحديث المخ» zip, never with brain.ini
    or a key. Only to a robot with its own key."""
    device, err = views._device_or_401(request)
    if err:
        return err
    from django.http import HttpResponse

    from .. import brain_package
    from ..brain import VERSION
    resp = HttpResponse(brain_package.build("", "", device.wake_name or "موس"), content_type="application/zip")
    resp["X-Brain-Version"] = VERSION
    resp["Cache-Control"] = "no-store"
    return resp


@_robot_endpoint
def telemetry(request):
    """Device reports its health (battery/temp/disk/wifi) for the live view.

    Body: any of `battery_percent`, `cpu_temp`, `free_disk_mb`, `wifi_rssi`.
    Raises a low-battery alert (deduped) when the battery drops below 15%.
    """
    device, err = views._device_or_401(request)
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
    device, err = views._device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    if image is None:
        return Response({"detail": "no image"}, status=status.HTTP_400_BAD_REQUEST)
    reason = str(request.data.get("reason") or "manual")
    if reason not in dict(RobotSnapshot.REASON):
        reason = "manual"
    # Photos the camera kept on its SD card while offline come with the time
    # they were taken; motion in the guard window then is still an intrusion.
    captured = _recorded_at(request.data.get("captured_at"))
    after_hours = (captured is not None and reason == "motion"
                   and services.is_after_hours(device, timezone.localtime(captured)))
    if after_hours:
        reason = "after_hours"
    snap = RobotSnapshot.objects.create(device=device, image=image, reason=reason)
    if captured is not None:
        RobotSnapshot.objects.filter(pk=snap.pk).update(created_at=captured)
    if after_hours:
        services.raise_after_hours_alert(
            device, snapshot=snap,
            message=(f"🚨 حركة بعد غلق المحل الساعة {timezone.localtime(captured):%H:%M} "
                     "والنت كان فاصل: الكاميرا صوّرتها على الكارت وبعتتها لما النت رجع."))
    # Ack the originating command if one was referenced.
    cmd_id = request.data.get("command_id")
    if cmd_id:
        views.RobotCommand.objects.filter(pk=cmd_id, device=device).update(
            status="done", done_at=timezone.now(), result={"snapshot_id": snap.id},
        )
    return Response({"ok": True, "snapshot_id": snap.id}, status=status.HTTP_201_CREATED)


@_robot_endpoint
def commands_pending(request):
    """Device polls queued dashboard/owner commands, which are marked sent."""
    device, err = views._device_or_401(request)
    if err:
        return err
    # Snapshot/scan belong to the camera board (delivered via /camera/frame/);
    # handing them to the bridge would ack them without a photo ever taken.
    pending = list(device.commands.filter(status="pending")
                   .exclude(kind__in=views.RobotCommand.CAMERA_KINDS)
                   .order_by("created_at")[:20])
    out = [{"command_id": c.id, "kind": c.kind, "payload": c.payload} for c in pending]
    views.RobotCommand.objects.filter(id__in=[c.id for c in pending]).update(
        status="sent", sent_at=timezone.now(),
    )
    return Response({"commands": out})


@_robot_endpoint
def commands_ack(request):
    """Device confirms a command finished (with optional result)."""
    device, err = views._device_or_401(request)
    if err:
        return err
    cmd = views.RobotCommand.objects.filter(
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
        from ..models import RobotPageCall
        page_id = (cmd.payload or {}).get("page_id")
        if page_id:
            RobotPageCall.objects.filter(pk=page_id, device=device).update(
                status="announced", announced_at=timezone.now(),
            )
    return Response({"ok": True})


@_robot_endpoint
def sync_pull(request):
    """Download a RETAIL-ONLY offline catalog so the robot works with no net."""
    device, err = views._device_or_401(request)
    if err:
        return err
    catalog = services.offline_catalog(device.branch)
    # The name it answers to, so «يا موس» is recognized with the net down too.
    catalog["wake"] = {"name": getattr(device, "wake_name", "") or "موس",
                       "aliases": list(getattr(device, "wake_aliases", None) or [])}
    # 🐛 [FIX]: الروبوت بيسأل كل دقيقة ([robot] answer_here): نفس الكتالوج كان بينزل كله
    #    على خط المحل كل مرة. دلوقتي لو مفيش جديد بيرجع 304 فاضي.
    tag = services.catalog_tag(catalog)
    # Caddy's compression can send the tag back as W/"…" or "…-gzip": the digest is what counts.
    if tag.strip('"') in request.headers.get("If-None-Match", ""):
        return Response(status=status.HTTP_304_NOT_MODIFIED, headers={"ETag": tag})
    return Response(catalog, headers={"ETag": tag})


@_robot_endpoint
def sync_push(request):
    """Replay queued offline events when the net returns (idempotent)."""
    device, err = views._device_or_401(request)
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
    device, err = views._device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    if image is None:
        return Response({"detail": "no image"}, status=status.HTTP_400_BAD_REQUEST)
    return Response(enrollment.capture(device, image.read()))
