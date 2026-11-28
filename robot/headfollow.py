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

Off by default — the head motor and its limit switches must be wired first. Turned on from the
robot's control page («🔄 الراس تلف ناحية اللي بيتكلم», RobotDevice.head_follow), where «اتبعني ٢٠
ثانية» tries it without a word said; or for every robot from the server's .env (ROBOT_HEAD_FOLLOW=1).

The head calibrates itself as it goes (learn): the first look after a move says how it went —
* the face farther the same way, twice in a row: the head turns the wrong way round (the relays or
  the camera mounted the other way) — its direction is flipped, and it turns toward them;
* the face now on the other side: it swung past them — its turn is made shorter (by a fifth);
* nearer, but not halfway: it crept — its turn is made longer.
What it learned is the robot's own (head_flip, head_edge_ms), on its page to see or set by hand; the
.env's ROBOT_HEAD_EDGE_MS / ROBOT_HEAD_FLIP are the starting point.
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
# Learning from a move: only the look right after it has settled (the person hardly moved meanwhile).
LEARN_WITHIN_S = 2
WRONG_WAY = 2              # moves that took them farther, in a row, before the direction is flipped
SHORTER, LONGER = 0.8, 1.2
CREPT = 0.5                # a move that brought them less than this of the way crept
EDGE_LEAST = 2 * services.HEAD_MIN_MS
EDGE_MOST = 1500
TRY_S = 20                 # «اتبعني ٢٠ ثانية» on the robot's page

_lock = threading.Lock()


def enabled(device=None) -> bool:
    """On for this robot (its page), or for every robot (the server's .env)."""
    if (os.getenv("ROBOT_HEAD_FOLLOW") or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return getattr(device, "head_follow", False) is True


def following(device, now=None) -> bool:
    """Whether the camera's frames are looked at now: on, and someone's talking."""
    return enabled(device) and wakename.in_conversation(device, now)


def try_it(device, now=None) -> None:
    """«اتبعني ٢٠ ثانية»: the head follows whoever's in front for TRY_S seconds, nothing said —
    to see it turn the right way (it learns that too) before a customer does."""
    device.listening_until = (now or timezone.now()) + timedelta(seconds=TRY_S)
    device.save(update_fields=["listening_until"])


def can_see_faces() -> bool:
    """dlib is on the server (the head can find a face) — for the robot's page."""
    import importlib.util

    return importlib.util.find_spec("dlib") is not None


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
    moved = _recent(device, "moved", now, RETRY_AFTER_S) or {}
    wrong = 0
    if moved and not moved.get("judged") and now.timestamp() - moved.get("done", moved["at"]) <= LEARN_WITHIN_S:
        wrong = learn(device, moved, offset)
        if wrong is None:           # it was turning the wrong way: flipped, a fresh start
            moved, wrong = {}, 0
        else:
            moved["judged"] = True
            cache.set(_key(device, "moved"), moved, RETRY_AFTER_S + 10)
    if abs(offset) < services.HEAD_DEAD_ZONE:          # facing them
        cache.delete(_key(device, "moved"))
        return None
    # A move that way and they're no nearer the middle: the head is at its end
    # (its limit switch), or the camera isn't on it — then turning again would
    # only drive it into the switch. It waits, and tries again later.
    side = 1 if offset > 0 else -1
    futile = moved.get("futile", 0) + 1 if (
        moved.get("side") == side and abs(offset) > moved.get("x", 1.0) - NEARER) else 0
    if futile >= GIVE_UP:
        return None
    move = services.look_at(device, offset)
    if move is not None:            # the next look starts over, from where the head stops
        cache.delete(_key(device))
        cache.set(_key(device, "moved"), {
            "side": side, "x": abs(offset), "futile": futile, "wrong": wrong, "at": now.timestamp(),
            "done": now.timestamp() + move.duration_ms / 1000 + SETTLE.total_seconds(),
        }, RETRY_AFTER_S + 10)
    return move


def learn(device, moved: dict, offset: float):
    """What the look right after a move says of the head — its turn made shorter or longer, or its
    direction flipped (then None). Else how many moves in a row took them farther."""
    x, side = moved.get("x", 0.0), moved.get("side", 0)
    if abs(offset) < services.HEAD_DEAD_ZONE:
        return 0                                       # it faces them: just right
    if (1 if offset > 0 else -1) != side:              # swung past them
        _edge(device, SHORTER)
        return 0
    if abs(offset) > x + NEARER:                       # farther the same way: turning away from them
        wrong = int(moved.get("wrong") or 0) + 1
        if wrong < WRONG_WAY:
            return wrong
        device.head_flip = not (getattr(device, "head_flip", False) is True)
        device.save(update_fields=["head_flip"])
        logger.info("robot head follow: %s turned away from them %d times — its direction flipped",
                    device.pk, wrong)
        return None
    if NEARER <= x - abs(offset) < x * CREPT:          # nearer, but not halfway: it crept
        _edge(device, LONGER)
    return 0                                           # no nearer: its end (futile, in follow)


def _edge(device, by: float) -> None:
    ms = max(EDGE_LEAST, min(EDGE_MOST, round(services.head_edge_ms(device) * by)))
    if ms != services.head_edge_ms(device):
        device.head_edge_ms = ms
        device.save(update_fields=["head_edge_ms"])
        logger.info("robot head follow: %s's turn to a face at the edge is now %d ms", device.pk, ms)
