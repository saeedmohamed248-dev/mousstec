"""🔐 API الروبوت — توثيق الجهاز والموظف (وش/جلسة) والصلاحيات وغلاف الـ endpoints."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.response import Response

from .. import permissions, security
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch



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
            views.RobotAccessLog.objects
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
# command carries no face. We use whoever is in front of the camera RIGHT NOW:
# the LATEST face check on this device (the cam re-checks every few seconds
# while someone's there), and only if it's recent and a granted match. A
# manager who merely walked past earlier doesn't count — the moment a
# customer (or nobody) is in frame, the latest check is "unknown" and voice
# gets no staff identity. Sales/motion still need their own face match.
_VOICE_FACE_SECONDS = 15


def _voice_employee(request, device):
    """Employee for a voice turn: explicit face auth, else the face the
    camera matched in its latest check (≤ `_VOICE_FACE_SECONDS` old) — or, from
    «📱 كلّم موس» (robot/phone.py), whoever is signed in."""
    if getattr(request, "robot_employee", None) is not None:
        return request.robot_employee
    employee = views._authorized_employee(request, device)
    if employee is not None:
        return employee
    since = timezone.now() - timedelta(seconds=_VOICE_FACE_SECONDS)
    latest = (views.RobotAccessLog.objects
              .filter(device=device, created_at__gte=since)
              .select_related("employee").order_by("-created_at").first())
    if (latest and latest.result == "granted" and latest.employee
            and _employee_is_active(latest.employee)):
        return latest.employee
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
    employee = views._authorized_employee(request, device)
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


# All robot endpoints authenticate by device token, not Django session/JWT,
# and are rate-limited per device (not by the global anonymous-per-IP limit).
def _robot_endpoint(view, throttles=None):
    return api_view(["GET", "POST"])(
        authentication_classes([])(permission_classes([])(
            throttle_classes(throttles or [security.RobotDeviceThrottle])(view)))
    )


def _kiosk_lookup_endpoint(view):
    return _robot_endpoint(
        view, throttles=[security.RobotDeviceThrottle, security.KioskLookupThrottle])
