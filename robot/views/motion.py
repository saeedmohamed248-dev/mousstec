"""🦾 API الروبوت — الحركة: أوامر الموتورات، الكلام، إطار الكاميرا، بص على."""

from __future__ import annotations


from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from .. import audio as audio_svc
from .. import enrollment, headfollow, services
from ..models import (
    RobotSnapshot,
)
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from ._common import _robot_endpoint


@_robot_endpoint
def motor(request):
    """Queue a physical-articulation command (head/arms/tracks).

    Requires a face-authorized employee (safety: no arm lift by anonymous
    request). Body: `actuator`, `direction`, `duration_ms`, face auth.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    employee, perr = views._require_permission(request, device, "motor")
    if perr:
        return perr
    try:
        actuator, direction, duration_ms = services.validate_motor_command(
            request.data.get("actuator"), request.data.get("direction"),
            request.data.get("duration_ms"),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    cmd = views.MotorCommandLog.objects.create(
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
    device, err = views._device_or_401(request)
    if err:
        return err
    # 🐛 [FIX]: الأوامر مكانش ليها صلاحية. لو البريدج مش بيسحب (HAS_MOTORS 0،
    # أو النت قاطع) أوامر «لف الراس ناحية اللي بيتكلم» والداشبورد بتتراكم، وأول
    # ما يرجع يسحب كان بينفّذ القديم كله ورا بعض — راس ودراع بيتحركوا من غير ما
    # حد طالب دلوقتي. القديم ميتبعتش ويفضل acknowledged=False (متنفّذش فعلاً).
    fresh_from = timezone.now() - services.MOTOR_COMMAND_TTL
    pending = list(device.motor_commands
                   .filter(acknowledged=False, created_at__gte=fresh_from)
                   .order_by("created_at")[:20])
    frames = [{"command_id": c.id, "frame": c.serial_frame()} for c in pending]
    views.MotorCommandLog.objects.filter(id__in=[c.id for c in pending]).update(
        acknowledged=True, acknowledged_at=timezone.now(),
    )
    return Response({"commands": frames})


@_robot_endpoint
def speak(request):
    """Text-to-speech: return synthesized audio bytes for the amp to play.

    Body: `text`. Returns audio/mpeg when a TTS provider is available, else 204
    so the firmware can fall back to on-device synthesis.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    text = (request.data.get("text") or "").strip()[:600]
    from django.http import HttpResponse
    # `format=wav` → 16 kHz mono PCM the ESP32 streams straight to the amp;
    # with `codec=ulaw`, µ-law: half the bytes over a slow line.
    if str(request.data.get("format", "")).lower() == "wav":
        codec = audio_svc.ULAW if str(request.data.get("codec", "")).lower() == audio_svc.ULAW \
            else audio_svc.PCM
        wav = audio_svc.synthesize_wav(text, codec=codec) if text else None
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
    device, err = views._device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    # «الراس يلف مع اللي بيتكلم»: while someone talks with the robot, the face
    # in this frame turns the head toward them (robot/headfollow.py). An extra:
    # it never costs the upload.
    following = headfollow.following(device)
    if image is not None and following:
        try:
            headfollow.follow(device, image.read())
        except Exception:
            services.logger.warning("robot camera: head follow failed", exc_info=True)
        finally:
            image.seek(0)
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
    push_ms = device.desired_push_interval_ms()
    if following:                         # a face seen twice in under a second
        push_ms = min(push_ms, headfollow.PUSH_MS)
    return Response({
        "ok": True, "motion": motion, "after_hours_alert": alerted,
        "push_interval_ms": 700 if enroll else push_ms,
        "commands": cam_cmds,
        "enroll": enroll,
        # For an outage: when the shop is closed the camera keeps a photo
        # every 2 s of motion, open hours one every 30 s.
        "guard": services.guard_window(device),
    })


@_robot_endpoint
def look(request):
    """Face the speaker: turn the head toward a horizontal `offset` ∈ [-1,1].

    The ESP32-CAM computes the offset from the detected face's position in frame
    and calls this; the head pans to center them. No auth beyond the device
    token — turning to look at someone is harmless.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    cmd = services.look_at(device, request.data.get("offset", 0),
                           issued_by=None)
    return Response({"ok": True, "turned": bool(cmd),
                     "frame": cmd.serial_frame() if cmd else None})
