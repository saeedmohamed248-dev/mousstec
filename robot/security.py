"""
robot/security.py — device auth + facial-recognition access control.

Two jobs:
  1. Authenticate the ESP32 device itself (per-device bearer token).
  2. Resolve a face embedding from the ESP32-CAM to an authorized `hr.Employee`,
     gate privileged actions (creating a sale, dispensing a part) on a match, and
     drive attendance clock-in/out via `hr.AttendanceRecord`.

Face matching uses the embeddings already stored on `hr.Employee.face_encoding`
(a JSON list of floats) and the tenant threshold in `hr.HRSettings`. The actual
embedding is produced on the device/edge or by a pluggable extractor; here we
compare vectors with cosine similarity. No third-party face SDK is imported so
this runs anywhere; swap `compare_embeddings` for your model of choice.
"""

from __future__ import annotations

import math
import os
from decimal import Decimal
from typing import Optional

from django.utils import timezone


# ---------------------------------------------------------------------------
# Device authentication
# ---------------------------------------------------------------------------

def authenticate_device(request):
    """Return the RobotDevice for a valid `X-Robot-Token`, else None.

    The firmware sends the token in the `X-Robot-Token` header. On success we
    stamp `last_seen_at`/`last_ip` (heartbeat) and return the device.
    """
    from .models import RobotDevice

    token = request.headers.get("X-Robot-Token") or request.META.get("HTTP_X_ROBOT_TOKEN")
    if not token:
        return None
    device = RobotDevice.objects.filter(api_token=token, is_active=True).first()
    if not device:
        return None
    try:
        from .services import note_device_seen
        note_device_seen(device)  # raises a "back online" alert after an outage
    except Exception:
        pass  # an alert must never lock the device out
    device.last_seen_at = timezone.now()
    device.last_ip = _client_ip(request)
    device.save(update_fields=["last_seen_at", "last_ip"])
    return device


def _client_ip(request) -> Optional[str]:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


# ---------------------------------------------------------------------------
# Face matching
# ---------------------------------------------------------------------------

def compare_embeddings(a, b) -> float:
    """Cosine similarity in [0,1] between two embedding vectors.

    Returns 0.0 on mismatched/empty vectors. Replace with your face model's own
    distance if you use one — keep the [0,1] contract so the threshold holds.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    sim = dot / (na * nb)
    # Cosine ∈ [-1,1] → map to [0,1].
    return max(0.0, min(1.0, (sim + 1.0) / 2.0))


def _threshold() -> float:
    """Tenant face-match threshold from HRSettings (fallback 0.85)."""
    try:
        from hr.models import HRSettings
        s = HRSettings.objects.first()
        if s and s.face_match_threshold is not None:
            return float(s.face_match_threshold)
    except Exception:
        pass
    return 0.85


# Set ROBOT_FACE_ALLOW_INSECURE_MATCH=1 to let the non-biometric fallback
# authorize people. It is for demos on fake data only — see `matching_available`.
_ALLOW_INSECURE_MATCH = os.getenv("ROBOT_FACE_ALLOW_INSECURE_MATCH", "") == "1"


def matching_available() -> bool:
    """True when face matching is safe to authorize on.

    Face matches gate sales, part dispensing and attendance, so they may only
    run on a real face model. Without one installed, `robot.faces` falls back to
    a grayscale thumbnail that scores ~0.99 between *different* people — it
    would authorize the first enrolled employee for anybody who stood in front
    of the camera. So we fail closed instead: install `face_recognition`, or set
    ROBOT_FACE_ALLOW_INSECURE_MATCH=1 knowingly for a demo.
    """
    from . import faces

    return faces.is_biometric() or _ALLOW_INSECURE_MATCH


def identify_employee(embedding, *, branch=None):
    """Best matching authorized employee for a face embedding.

    Returns (employee, score). Only active employees with a stored
    `face_encoding` are considered; when `branch` is given, employees of that
    branch are preferred but company-wide is allowed. Returns (None, best_score)
    if nothing clears the threshold, and (None, 0.0) outright when no real face
    model is installed (see `matching_available`).
    """
    from hr.models import Employee

    if not embedding:
        return None, 0.0

    if not matching_available():
        return None, 0.0

    qs = Employee.objects.exclude(face_encoding__isnull=True)
    # `is_active` may live on the linked user; filter defensively.
    qs = qs.filter(user__is_active=True) if _employee_has_user_active() else qs

    best_emp, best_score = None, 0.0
    for emp in qs.iterator():
        enc = emp.face_encoding
        if not enc:
            continue
        score = compare_embeddings(embedding, enc)
        if score > best_score:
            best_emp, best_score = emp, score

    if best_emp and best_score >= _threshold():
        return best_emp, best_score
    return None, best_score


def _employee_has_user_active() -> bool:
    try:
        from hr.models import Employee
        Employee._meta.get_field("user")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Attendance (clock-in / clock-out) on a recognized face
# ---------------------------------------------------------------------------

# A face seen again sooner than this after clocking in is the same visit (they
# walked past the robot twice), not a departure.
MIN_SHIFT_MINUTES = 60


def register_attendance(employee, *, match_score: float, purpose: str = "attendance"):
    """Clock the employee in/out from a recognized face.

    `purpose` says why the camera looked:
      * "attendance" — the employee deliberately checked in/out at the robot:
        first match of the day clocks in, the next one (after
        MIN_SHIFT_MINUTES) clocks out.
      * "authorize"  — the camera saw them while authorizing an action or on
        motion. The first sighting of the day still clocks them in (they're
        here), later sightings only move `clock_out` forward as "last seen"
        and are reported as "authorize". Walking past the robot never ends a
        shift minutes after it began.

    Creates/updates today's `hr.AttendanceRecord`, marking `face_verified=True`.
    Returns ("clock_in" | "clock_out" | "authorize", record).
    """
    from hr.models import AttendanceRecord

    today = timezone.localdate()
    record, _created = AttendanceRecord.objects.get_or_create(
        employee=employee, date=today,
        defaults={"status": "present"},
    )
    now = timezone.now()
    if not record.clock_in:
        record.clock_in = now
        record.face_verified = True
        record.status = "present"
        record.save(update_fields=["clock_in", "face_verified", "status"])
        return "clock_in", record

    worked_min = (now - record.clock_in).total_seconds() / 60
    if worked_min < MIN_SHIFT_MINUTES:
        return "authorize", record

    if purpose == "attendance":
        if not record.clock_out:
            record.clock_out = now
            record.face_verified = True
            record.save(update_fields=["clock_out", "face_verified"])
            return "clock_out", record
        # Already clocked both ways today — treat as a presence ping.
        return "authorize", record

    # Passive sighting: they're still here, so the day ends no earlier than now.
    if record.clock_out is None or record.clock_out < now:
        record.clock_out = now
        record.save(update_fields=["clock_out"])
    return "authorize", record
