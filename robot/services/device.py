"""🦾 خدمات الروبوت — الجهاز نفسه: تيار الموتورات، أوامر الحركة، أونلاين/أوفلاين، الراس، حارس بعد المواعيد، البطارية، نداء الموظفين."""

from __future__ import annotations

from datetime import timedelta

import logging
import math
import os
import re

from django.db import transaction
from django.utils import timezone


logger = logging.getLogger("mouss_tec_core")


# ---------------------------------------------------------------------------
# Predictive maintenance — the robot watches its own motors
# ---------------------------------------------------------------------------

# Stall-level current per actuator (amps). Above this the motor is jammed or
# at its end stop; the Mega also cuts it in firmware. Tune to your motors.
MOTOR_CURRENT_LIMIT_A = {"head": 8.0, "arm_left": 10.0, "arm_right": 10.0, "track": 15.0}
# A motor drawing this much more than its own running average is wearing out
# (bearings/brushes/gearbox) — worth a look before it fails.
_WEAR_RATIO = 1.5
_WEAR_MIN_SAMPLES = 20


def record_motor_currents(device, currents: dict) -> list:
    """Fold per-actuator current readings into `device.motor_health`.

    Keeps a running average per motor and raises a (deduped) `motor_fault`
    alert on a stall-level reading or a sustained rise above the motor's own
    baseline. Returns the alerts raised.
    """
    from ..models import RobotAlert

    health = dict(device.motor_health or {})
    alerts = []
    now = timezone.now()
    for actuator, value in (currents or {}).items():
        if actuator not in MOTOR_DIRECTIONS:
            continue
        try:
            amps = float(value)
        except (TypeError, ValueError):
            continue
        if amps < 0:
            continue
        h = dict(health.get(actuator) or {})
        n = int(h.get("samples", 0))
        avg = float(h.get("avg_a", 0.0))
        problem = None
        if amps >= MOTOR_CURRENT_LIMIT_A[actuator]:
            problem = f"⚠️ موتور {actuator} سحب {amps:.1f} أمبير (زنقة/نهاية مشوار) — اتفصل."
        elif n >= _WEAR_MIN_SAMPLES and avg > 0 and amps > avg * _WEAR_RATIO:
            problem = (f"🔧 موتور {actuator} بيسحب {amps:.1f} أمبير والمعتاد {avg:.1f} — "
                       "غالباً محتاج صيانة (تزييت/فحم/جيربوكس).")
        else:
            # Only healthy readings update the baseline, so a failing motor
            # doesn't slowly teach the robot that failing is normal.
            h["avg_a"] = round((avg * n + amps) / (n + 1), 3)
            h["samples"] = n + 1
        h["last_a"] = amps
        h["at"] = now.isoformat()
        health[actuator] = h
        if problem:
            recent = RobotAlert.objects.filter(
                device=device, kind="motor_fault", message__contains=actuator,
                created_at__gte=now - timedelta(minutes=30),
            ).exists()
            if not recent:
                alerts.append(RobotAlert.objects.create(
                    device=device, kind="motor_fault", message=problem[:255],
                ))
    device.motor_health = health
    device.save(update_fields=["motor_health"])
    return alerts


def motor_runtime_ms(device, *, days: int = 30) -> dict:
    """Total commanded run time per actuator (ms) — wear counter for the page."""
    from django.db.models import Sum
    from ..models import MotorCommandLog
    rows = (MotorCommandLog.objects
            .filter(device=device, created_at__gte=timezone.now() - timedelta(days=days))
            .exclude(direction="stop")
            .values("actuator").annotate(total=Sum("duration_ms")))
    return {r["actuator"]: int(r["total"] or 0) for r in rows}


# What each actuator can physically do (mirrors the Mega's handleCommand).
MOTOR_DIRECTIONS = {
    "head": {"left", "right", "stop"},
    "arm_left": {"up", "down", "stop"},
    "arm_right": {"up", "down", "stop"},
    "track": {"forward", "backward", "stop"},
}
# Longest single move, matching the Mega's DEFAULT_PULSE_CAP. A move always
# ends on its own: "0 = run until stop" would run a window motor into its end
# stop and stall it (burning the motor and relay), and a lost stop frame would
# leave the tracks driving.
MOTOR_MAX_MS = 5000
MOTOR_DEFAULT_MS = 500
# A queued move older than this is never sent (see views.motor_pending). The
# bridge polls every 500 ms, so a live command is picked up within a second.
MOTOR_COMMAND_TTL = timedelta(seconds=30)


def validate_motor_command(actuator, direction, duration_ms=None):
    """Normalize a requested move → (actuator, direction, duration_ms).

    Raises ValueError with a spoken-friendly reason for anything the hardware
    can't do. `stop` always has duration 0; any other move is clamped to
    1..MOTOR_MAX_MS (missing/0 → MOTOR_DEFAULT_MS).
    """
    actuator = str(actuator or "").strip().lower()
    direction = str(direction or "").strip().lower()
    if actuator not in MOTOR_DIRECTIONS:
        raise ValueError(f"جزء غير معروف: {actuator or '—'}")
    if direction not in MOTOR_DIRECTIONS[actuator]:
        raise ValueError(f"الاتجاه «{direction or '—'}» غير متاح لـ {actuator}")
    if direction == "stop":
        return actuator, direction, 0
    try:
        ms = int(float(duration_ms)) if duration_ms not in (None, "") else 0
    except (TypeError, ValueError):
        raise ValueError("مدة الحركة غير صالحة")
    if ms <= 0:
        ms = MOTOR_DEFAULT_MS
    return actuator, direction, min(ms, MOTOR_MAX_MS)


# A device silent for this long counts as offline (the dashboard's "online"
# dot uses 90s; alerts wait longer so a Wi-Fi blip doesn't page anyone).
OFFLINE_AFTER_SECONDS = 5 * 60


def note_device_seen(device, now=None):
    """Record that the device just talked to us; alert if it was offline.

    Returns the `back_online` RobotAlert when this call ends an outage, else
    None. Called on every authenticated device request, before `last_seen_at`
    is overwritten.
    """
    from ..models import RobotAlert

    now = now or timezone.now()
    last = device.last_seen_at
    if last is None or (now - last).total_seconds() < OFFLINE_AFTER_SECONDS:
        return None
    gone = int((now - last).total_seconds() // 60)
    return RobotAlert.objects.create(
        device=device, kind="back_online",
        message=f"✅ الروبوت رجع أونلاين بعد انقطاع حوالي {gone} دقيقة.",
    )


def raise_offline_alerts(now=None) -> int:
    """Alert once per outage for every active device that went silent.

    Run periodically (see `robot.tasks`). An outage is alerted once: we skip a
    device that already has an `offline` alert newer than its last contact.
    """
    from ..models import RobotAlert, RobotDevice

    now = now or timezone.now()
    cutoff = now - timedelta(seconds=OFFLINE_AFTER_SECONDS)
    raised = 0
    for device in RobotDevice.objects.filter(is_active=True, last_seen_at__lt=cutoff):
        already = RobotAlert.objects.filter(
            device=device, kind="offline", created_at__gte=device.last_seen_at,
        ).exists()
        if already:
            continue
        RobotAlert.objects.create(
            device=device, kind="offline",
            message=(f"📡 الروبوت {device.name} فقد الاتصال — آخر اتصال "
                     f"{timezone.localtime(device.last_seen_at):%Y-%m-%d %H:%M}."),
        )
        raised += 1
    return raised


def take_camera_commands(device, *, limit: int = 5) -> list:
    """Hand the camera its pending snapshot/scan commands (marked sent)."""
    from ..models import RobotCommand
    cmds = list(device.commands.filter(
        status="pending", kind__in=RobotCommand.CAMERA_KINDS,
    ).order_by("created_at")[:limit])
    if cmds:
        RobotCommand.objects.filter(id__in=[c.id for c in cmds]).update(
            status="sent", sent_at=timezone.now(),
        )
    return [{"command_id": c.id, "kind": c.kind, "payload": c.payload} for c in cmds]


# The head as it's built, calibrated in the shop (the server's .env):
#   ROBOT_HEAD_EDGE_MS  the pulse for a face at the frame's edge — less when the
#                       head swings past the person, more when it creeps up on them
#   ROBOT_HEAD_FLIP=1   it turns away from them (the camera or the relays the other way round)
HEAD_EDGE_MS = 250
HEAD_DEAD_ZONE = 0.15      # already looking at them: a face this near the middle
HEAD_MIN_MS = 40           # shorter, the relay closes and the motor doesn't move


def head_edge_ms() -> int:
    try:
        ms = int(float(os.getenv("ROBOT_HEAD_EDGE_MS") or HEAD_EDGE_MS))
    except ValueError:
        ms = HEAD_EDGE_MS
    return max(HEAD_MIN_MS, min(ms, MOTOR_MAX_MS))


def head_flipped() -> bool:
    return (os.getenv("ROBOT_HEAD_FLIP") or "").strip().lower() in ("1", "true", "yes", "on")


def look_at(device, offset: float, *, issued_by=None):
    """Turn the robot's head toward a target at horizontal `offset` ∈ [-1,1].

    offset < 0 → target is to the LEFT, > 0 → RIGHT, ~0 → centered (no move).
    Emits a head MotorCommandLog whose duration is proportional to how far
    off-center the target is; the ESP32 picks it up from /motor/pending/ and the
    Mega pans the head. Used to face the customer/speaker (robot/headfollow.py).
    """
    from ..models import MotorCommandLog

    try:
        offset = float(offset)
    except (TypeError, ValueError):
        return None
    # 🐛 [FIX]: «nan» من الكاميرا كان بيعدّي من الـ clamp كـ 1.0 (min(1.0, nan) = 1.0)
    # والراس تلف لفة كاملة يمين.
    if math.isnan(offset):
        return None
    offset = max(-1.0, min(1.0, offset))
    if abs(offset) < HEAD_DEAD_ZONE:  # already looking at them
        return None
    if head_flipped():
        offset = -offset
    direction = "right" if offset > 0 else "left"
    # 🐛 [FIX]: كانت 700 ms ثابتة لوش على طرف الصورة — موتور عربية بيلف في الوقت ده
    # أبعد بكتير من نص زاوية الكاميرا، فالراس كانت بتعدّي اللي بيتكلم وترجع تتهز.
    # دلوقتي بتتظبط على الراس نفسها من الـ .env (ROBOT_HEAD_EDGE_MS، و ROBOT_HEAD_FLIP لو بتلف عكس).
    duration_ms = max(HEAD_MIN_MS, round(abs(offset) * head_edge_ms()))
    return MotorCommandLog.objects.create(
        device=device, actuator="head", direction=direction,
        duration_ms=duration_ms, issued_by=issued_by,
    )


# ---------------------------------------------------------------------------
# After-hours guard
# ---------------------------------------------------------------------------

def is_after_hours(device, now=None) -> bool:
    """True if `now` (local) falls in the device's guard window (shop closed).

    Uses the device's explicit guard_from/guard_to when set. When they're not,
    falls back to a sane default night window (20:00–08:00) so guarding is armed
    out of the box rather than silently off.
    """
    now = now or timezone.localtime()
    t = now.time()
    start = device.guard_from
    end = device.guard_to
    if start is None or end is None:
        start = start or _dt_time(20, 0)
        end = end or _dt_time(8, 0)
    if start <= end:
        return start <= t <= end
    # Window crosses midnight (e.g. 20:00 → 08:00).
    return t >= start or t <= end


def guard_window(device) -> dict:
    """The guard window as the camera needs it to tell night from day on its
    own during an outage: local "HH:MM" bounds (the same defaults as
    `is_after_hours`) and the current UTC offset in seconds (Cairo changes
    it for summer time; the camera's clock is UTC)."""
    start = device.guard_from or _dt_time(20, 0)
    end = device.guard_to or _dt_time(8, 0)
    offset = timezone.localtime().utcoffset()
    return {"from": start.strftime("%H:%M"), "to": end.strftime("%H:%M"),
            "utc_offset": int(offset.total_seconds()) if offset else 0}


def _dt_time(h, m):
    from datetime import time as _t
    return _t(h, m)


def raise_low_battery_alert(device, percent):
    """Raise a low-battery alert, deduped to one per 30 minutes per device."""
    from ..models import RobotAlert
    recent = (RobotAlert.objects
              .filter(device=device, kind="low_battery",
                      created_at__gte=timezone.now() - timedelta(minutes=30))
              .exists())
    if recent:
        return None
    return RobotAlert.objects.create(
        device=device, kind="low_battery",
        message=f"🔋 بطارية الروبوت منخفضة ({percent}%) — محتاجة شحن.",
    )


# «هسأل لك حد من زمايلي» — and nobody was asked. The robot calls out for one, aloud, and the
# dashboard's alerts say who asked what.
STAFF_CALL = "يا موظف! يا موظف! يا موظف!"
STAFF_CALL_EVERY_S = 60              # one call a minute: a customer asking twice isn't two calls
_STAFF_WORDS = re.compile(r"زمايلي|زميلي|زميل|الموظفين|موظف")
_STAFF_ASKED = re.compile(r"هسأل|اسأل|أسأل|نسأل|هند|ند[هي]|ناد|هراجع|هشوف|حالا|حالاً|فورا|فوراً|لحظة|ثانية|يساعد|هيجي|جاي")


def hands_to_staff(reply: str) -> bool:
    """The reply says a member of staff will (or should) help: «هسأل لك حد من
    زمايلي»، «اسأل حد من الموظفين»، «لحظة وهندهلك»."""
    return bool(reply and _STAFF_WORDS.search(reply) and _STAFF_ASKED.search(reply))


def call_staff(device, question: str) -> bool:
    """Ask the staff over — once a minute at most for a robot. True when the
    call is to be said now; the dashboard's alerts keep what was asked."""
    from django.core.cache import cache

    from ..models import RobotAlert

    key = f"robot:staff:called:{device.pk}"
    try:
        if not cache.add(key, 1, STAFF_CALL_EVERY_S):
            return False
    except Exception:                    # the cache down: called, as if it were up
        pass
    RobotAlert.objects.create(device=device, kind="other",
                              message=f"🙋 عميل عند {device.name} محتاج موظف: «{(question or '')[:200]}»")
    return True


@transaction.atomic
def raise_after_hours_alert(device, *, snapshot=None, message=""):
    """Create an after-hours motion alert (deduped to one per 5 minutes)."""
    from ..models import RobotAlert

    recent = (RobotAlert.objects
              .filter(device=device, kind="after_hours_motion",
                      created_at__gte=timezone.now() - timedelta(minutes=5))
              .exists())
    if recent:
        return None
    return RobotAlert.objects.create(
        device=device, kind="after_hours_motion", snapshot=snapshot,
        message=message or "🚨 حركة مرصودة بعد غلق المحل!",
    )
