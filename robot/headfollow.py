"""robot/headfollow.py — «الراس يلف مع اللي بيتكلم»: while someone talks with
the robot, its head turns to face them.

The ESP32-CAM pushes a live frame to /camera/frame/ (views.camera_frame) every
second or so. While the robot is in a conversation (wakename.in_conversation:
called by its name in the last 20 s) each frame is looked at here: where is the
face? Off to one side — and there in the frame before as well, so a face that
only passes by isn't chased — and the head turns toward it by a short pulse
(services.look_at): the bridge takes it from /motor/pending/ and the Mega runs
the head motor, its limit switches stopping it at either end. Then it waits for
the head to settle before looking again, so it never swings back and forth.

dlib's face detector finds the face (dlib-bin, already in the server image for
face matching): a frame looked at 480 wide, twice that size inside, sees a face
up to about 1.5 m away in 50–100 ms. Without dlib the head stays put.

Off by default — the head motor and its limit switches must be wired first.
On the server's .env:
  ROBOT_HEAD_FOLLOW=1   the head follows whoever talks to the robot
and the head's own calibration (services.look_at): ROBOT_HEAD_EDGE_MS, ROBOT_HEAD_FLIP.
"""

from __future__ import annotations

import io
import logging
import os
import threading
from datetime import timedelta
from functools import lru_cache
from typing import Optional

from django.core.cache import cache
from django.utils import timezone

from . import services, wakename

logger = logging.getLogger("mouss_tec_core")

PUSH_MS = 400              # the camera's cadence while following: a face seen twice in under a second
# The frame is looked at this wide, at twice the size: dlib's smallest face is
# 80 px, a face 1.5 m from the camera's VGA is 40 — the 640 as sent sees 90 cm.
LOOK_WIDTH = 480
UPSAMPLE = 1
SETTLE = timedelta(seconds=1)   # after a move: the bridge's poll (0.5 s), then a frame from where it stopped
SEEN_WITHIN_S = 1.5        # the frame before is this recent at most…
SAME_PLACE = 0.3           # …and its face this near, or it's someone passing by
SAME_SIZE = 0.7            # of two faces about as near, the one it was already facing
NEARER = 0.1               # a move brings the face at least this much nearer the middle…
GIVE_UP = 2                # …or after this many that didn't, it waits
RETRY_AFTER_S = 20         # (a conversation's window) before trying that way again

_lock = threading.Lock()


def enabled() -> bool:
    return (os.getenv("ROBOT_HEAD_FOLLOW") or "").strip().lower() in ("1", "true", "yes", "on")


def following(device, now=None) -> bool:
    """Whether the camera's frames are looked at now: on, and someone's talking."""
    return enabled() and wakename.in_conversation(device, now)


@lru_cache(maxsize=1)
def _detector():
    import importlib.util

    if importlib.util.find_spec("dlib") is None:
        logger.warning("robot head follow: dlib isn't installed — the head stays put")
        return None
    import dlib  # type: ignore

    return dlib.get_frontal_face_detector()


def face_offset(image_bytes: bytes, near: Optional[float] = None) -> Optional[float]:
    """Where the face is across the frame: -1 its left edge, 0 the middle, 1
    its right edge (as the camera sees it); None when there's no face (or no
    dlib). The nearest face — the biggest — or, of the faces about as big,
    the one nearest `near` (where the face it follows was)."""
    detector = _detector()
    if detector is None or not image_bytes:
        return None
    import numpy as np
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.draft("L", (LOOK_WIDTH, 1))   # a big JPEG decodes at about this width, fast
        img = img.convert("L")
        if img.width > LOOK_WIDTH:
            img = img.resize((LOOK_WIDTH, max(1, round(img.height * LOOK_WIDTH / img.width))))
        with _lock:
            boxes = list(detector(np.asarray(img), UPSAMPLE))
    except Exception:
        logger.warning("robot head follow: can't read the frame", exc_info=True)
        return None
    if not boxes:
        return None
    half = img.width / 2
    faces = [(max(-1.0, min(1.0, (b.left() + b.right()) / 2 / half - 1)), b.width()) for b in boxes]
    biggest = max(w for _x, w in faces)
    faces = [(x, w) for x, w in faces if w >= biggest * SAME_SIZE]
    if near is None:
        return max(faces, key=lambda f: f[1])[0]
    return min(faces, key=lambda f: abs(f[0] - near))[0]


def _key(device, what="seen") -> str:
    return f"robot:headfollow:{what}:{device.pk}"


def _recent(device, what, now, within):
    got = cache.get(_key(device, what))
    if got and 0 <= now.timestamp() - got.get("at", 0) <= within:
        return got
    return None


def follow(device, image_bytes: bytes, now=None):
    """Turn the head toward the face in `image_bytes` (a frame from the
    robot's camera) while someone talks to it. Returns the queued move
    (MotorCommandLog) or None: off, nobody talking, the head still settling
    from its last move, no face, seen once only, already facing them, or
    its last moves that way didn't bring them nearer."""
    now = now or timezone.now()
    if not following(device, now):
        return None
    # Any head move — this one's, a shelf pointed at, the dashboard's — and
    # the frames while it runs show where the head was, not where it is.
    last = device.motor_commands.filter(actuator="head").order_by("-created_at").first()
    if last and now < last.created_at + timedelta(milliseconds=last.duration_ms) + SETTLE:
        return None
    seen = _recent(device, "seen", now, SEEN_WITHIN_S)
    offset = face_offset(image_bytes, near=seen["x"] if seen else None)
    if offset is None:
        cache.delete(_key(device))
        return None
    cache.set(_key(device), {"x": offset, "at": now.timestamp()}, 10)
    if seen is None or abs(offset - seen["x"]) > SAME_PLACE:
        return None
    if abs(offset) < services.HEAD_DEAD_ZONE:          # facing them
        cache.delete(_key(device, "moved"))
        return None
    # A move that way and they're no nearer the middle: the head is at its end
    # (its limit switch), or the camera isn't on it — then turning again would
    # only drive it into the switch. It waits, and tries again later.
    side = 1 if offset > 0 else -1
    moved = _recent(device, "moved", now, RETRY_AFTER_S) or {}
    futile = moved.get("futile", 0) + 1 if (
        moved.get("side") == side and abs(offset) > moved.get("x", 1.0) - NEARER) else 0
    if futile >= GIVE_UP:
        return None
    move = services.look_at(device, offset)
    if move is not None:            # the next look starts over, from where the head stops
        cache.delete(_key(device))
        cache.set(_key(device, "moved"), {"side": side, "x": abs(offset), "futile": futile,
                                         "at": now.timestamp()}, RETRY_AFTER_S + 10)
    return move
