"""
robot/phone.py — «📱 كلّم موس»: the robot from a phone's browser.

The owner (or anyone on the staff) opens the robot's page on their phone and
talks to it — the phone hears them (the browser's own speech recognition, or a
recording the server hears), the server answers exactly as it answers the
robot in the shop (views._voice_turn), and the phone says the answer in the
robot's voice.

Signed in, they're known: no face, no PIN — a sale, a purchase, stock added,
an expense are asked step by step (robot/wizard.py) and allowed by their role
(robot/permissions.py), as at the counter.

Each person's phone is a robot of its own (`RobotDevice` «phone-<user id>»):
its conversation, its wizard, its memory never mix with the shop's robot.
It's never shown with the robots, never «offline».
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

from django.core.cache import cache

logger = logging.getLogger("mouss_tec_core")

PREFIX = "phone-"
TURNS_A_MINUTE = 30                # nobody says more; a runaway page is held to it
MOST_SAY = 600                     # letters voiced at once (a reply, not a book)
MOST_AUDIO = 5 * 1024 * 1024       # a recording of a sentence, not a speech


def is_phone(device) -> bool:
    return str(getattr(device, "device_uid", "")).startswith(PREFIX)


def employee_for(user):
    """The hr.Employee signed in, or None (an account not linked to one)."""
    from hr.models import Employee

    return Employee.objects.filter(user=user).select_related("user").first()


def _branch_for(user):
    from inventory.models import Branch
    from inventory.views.utils import _get_branch_for_user

    from .models import RobotDevice

    branch = _get_branch_for_user(user)
    if branch is not None:
        return branch
    robot = RobotDevice.objects.exclude(device_uid__startswith=PREFIX).select_related("branch").first()
    return robot.branch if robot is not None else Branch.objects.order_by("id").first()


def device_for(user):
    """This person's phone as a robot of its own — made the first time, on their branch.
    None when the shop has no branch yet."""
    from .models import RobotDevice

    branch = _branch_for(user)
    if branch is None:
        return None
    device = RobotDevice.objects.filter(device_uid=f"{PREFIX}{user.pk}").first()
    if device is None:
        device = RobotDevice(device_uid=f"{PREFIX}{user.pk}", branch=branch,
                             name=f"📱 {user.get_full_name() or user.get_username()}"[:80])
        device.issue_token()                # never handed out: the phone is signed in instead
        device.save()
    elif device.branch_id != branch.pk:     # the branch they work in now
        device.branch = branch
        device.save(update_fields=["branch"])
    return device


def too_many(user) -> bool:
    key = f"robot:phone:turns:{user.pk}:{int(time.time() // 60)}"
    try:
        cache.add(key, 0, 90)
        return cache.incr(key) > TURNS_A_MINUTE
    except Exception:                       # the cache down: the limit waits, the answer doesn't
        return False


def to_wav(data: bytes):
    """A phone's recording (webm/opus, mp4/aac, ogg…) as 16 kHz mono PCM WAV, or None."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not data or not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000",
             "-sample_fmt", "s16", "-f", "wav", "pipe:1"],
            input=data, capture_output=True, timeout=20, check=True)
    except Exception as exc:
        logger.warning("robot phone: the recording couldn't be read: %s", exc)
        return None
    return proc.stdout or None


def hear(device, data: bytes) -> str:
    """What was said in a phone's recording ('' when nothing could be heard): Azure first,
    within the shop's minutes, then Gemini — with the names of what the branch sells."""
    from . import audio as audio_svc
    from . import teachers

    wav = to_wav(data)
    if not wav:
        return ""
    try:
        return (teachers.hear(device, wav)
                or audio_svc.transcribe(wav, name=getattr(device, "wake_name", "") or "موس",
                                        hints=teachers.hints_for(device)) or "")
    except Exception as exc:                # speech-to-text down: said so, not a crash
        logger.warning("robot phone: speech-to-text failed: %s", exc)
        return ""


def ask(user, text: str) -> dict:
    """The robot's answer to `text`, said from `user`'s phone: /voice/'s own body."""
    from . import views

    device = device_for(user)
    if device is None:
        return {"addressed": False, "reason": "no_branch", "reply": "لازم يكون فيه فرع متسجل الأول."}
    request = SimpleNamespace(data={"transcript": text, "ptt": "1", "phone": "1"},
                              robot_employee=employee_for(user))
    body, _listen = views._voice_turn(request, device, None, {"t0": time.monotonic()})
    return body
