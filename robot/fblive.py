"""robot/fblive.py — «📺 الروبوت في لايف فيسبوك»: the robot answers the comments on the shop's live.

The owner goes live on the shop's Facebook page with the robot in the picture and turns this on
from the robot's page. While it's on, every few seconds (when the robot asks the server for its
commands anyway — no other loop to run; the reading itself in the background) the newest comments
on the live are read; each new one is answered by «المخ بأدوات» (robot/agent.py) as a customer —
prices only from the stock, the shop's own numbers never — the answer is posted as a reply under
the comment, and the robot says it aloud in the live: «يا كريم، أيوه فانوس الـ F30 موجود …» (the
answer, to them by name — never the comment itself).

* The page is the shop's own (omnichannel.TenantChannelConfig: its page and Meta token — the token
  needs pages_read_engagement and pages_manage_engagement).
* The live is found by itself (the page's video that's LIVE now), or given by its id.
* A few comments a turn (MOST_A_TURN), the oldest of the new first; the page's own comments, the
  ones from before it was turned on, and spam/insults/a bare «🔥» are left; each is answered once.
* It turns itself off after ON_HOURS, or from the page.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("mouss_tec_core")

ON_HOURS = 4
EVERY_S = 6                 # how often the comments are read
MOST_A_TURN = 2             # answered each time: the robot can't say more than that at once
SEEN_MOST = 500
TIMEOUT_S = 8
BUSY_S = 60                 # one reading at a time; a stuck one lets go after this
LIVE_CONTEXT = ("ده تعليق مكتوب على لايف المحل على فيسبوك من {name}. ردك هيتنشر تحت التعليق وهيتقال بصوت في "
                "اللايف: جملة أو اتنين، ودود ومحترم، وابدأ باسمه. لو عايز يشتري أو يحجز قوله يبعتلنا رسالة على "
                "الصفحة أو ييجي المحل. مبيعات المحل وحساباته متتقالش أبداً. لو التعليق شتيمة أو سبام أو مجرد "
                "تحية أو إيموجي مالهوش رد، اكتب SKIP بس.")
SKIP = "SKIP"


def _key(device) -> str:
    return f"robot:fblive:{device.pk}"


def state(device) -> dict:
    """{"video", "since", "seen", "polled", "answered"} while it's on — {} when it's off."""
    from django.core.cache import cache

    try:
        got = cache.get(_key(device))
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _save(device, st: dict) -> None:
    from django.core.cache import cache

    left = max(60, int(st.get("until", 0) - time.time()))
    cache.set(_key(device), st, left)


def start(device, video_id: str = "") -> dict:
    """On, for ON_HOURS: the comments from now on are answered."""
    st = {"video": (video_id or "").strip(), "since": time.time(), "until": time.time() + ON_HOURS * 3600,
          "seen": [], "polled": 0.0, "answered": 0}
    _save(device, st)
    return st


def stop(device) -> None:
    from django.core.cache import cache

    cache.delete(_key(device))


def _config():
    """The shop's Facebook page and its Meta token (omnichannel's settings), or None."""
    from django.db import connection

    try:
        from omnichannel.models import TenantChannelConfig
    except Exception:
        return None
    tenant = getattr(connection, "tenant", None)
    cfg = TenantChannelConfig.objects.filter(tenant=tenant).first() if tenant is not None else None
    if cfg is None or not cfg.facebook_page_id or not cfg.meta_access_token:
        return None
    return cfg


def connected() -> bool:
    """The shop's Facebook page is connected (no call to Facebook)."""
    try:
        return _config() is not None
    except Exception:
        return False


def page() -> tuple:
    """(page id, page token) of the shop's Facebook page, or ("", "") when it isn't connected."""
    from omnichannel.services.meta_api import resolve_page_token

    cfg = _config()
    if cfg is None:
        return "", ""
    return cfg.facebook_page_id, resolve_page_token(cfg.meta_access_token, cfg.facebook_page_id)


def _graph(path: str, token: str, **params) -> dict:
    import requests

    from omnichannel.services.meta_api import _graph_base

    resp = requests.get(f"{_graph_base()}/{path}", params={**params, "access_token": token}, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def live_video(page_id: str, token: str) -> str:
    """The page's video that's live now, or ''."""
    data = _graph(f"{page_id}/live_videos", token, broadcast_status='["LIVE"]', fields="id,status", limit=1)
    rows = data.get("data") or []
    return str(rows[0].get("id") or "") if rows else ""


def new_comments(video_id: str, token: str, page_id: str, st: dict) -> list:
    """The comments not answered yet, oldest first: [{"id", "name", "message"}]."""
    from django.utils.dateparse import parse_datetime

    data = _graph(f"{video_id}/comments", token, order="reverse_chronological", limit=25,
                  fields="id,message,from{id,name},created_time")
    seen = set(st.get("seen") or [])
    out = []
    for row in data.get("data") or []:
        cid, message = str(row.get("id") or ""), " ".join(str(row.get("message") or "").split())
        who = row.get("from") or {}
        when = parse_datetime(str(row.get("created_time") or ""))
        if not cid or cid in seen or len(message) < 2 or str(who.get("id") or "") == str(page_id):
            continue
        if when is not None and when.timestamp() < st.get("since", 0) - 5:
            continue                       # said before it was turned on
        out.append({"id": cid, "name": str(who.get("name") or "").strip(), "message": message[:300]})
    return list(reversed(out))


def due(device) -> bool:
    """Asked each time the robot asks for its commands: it's on and its time has come — and nobody
    else is reading the comments now. The reading itself runs in the background (robot.tasks.
    fblive_turn): the robot's poll never waits on Facebook or Gemini."""
    from django.core.cache import cache

    st = state(device)
    if not st or time.time() - float(st.get("polled") or 0) < EVERY_S:
        return False
    try:
        if not cache.add(_key(device) + ":busy", 1, BUSY_S):
            return False
    except Exception:
        return False
    st["polled"] = time.time()
    _save(device, st)
    return True


def done(device) -> None:
    from django.core.cache import cache

    try:
        cache.delete(_key(device) + ":busy")
    except Exception:
        pass


def _mark(device, ids: list, answered: int = 0) -> bool:
    """Comments done with (answered or let go), on the state as it is now — False when it was
    turned off meanwhile (then nothing more is said)."""
    st = state(device)
    if not st:
        return False
    st["seen"] = (list(st.get("seen") or []) + list(ids))[-SEEN_MOST:]
    st["answered"] = int(st.get("answered") or 0) + answered
    _save(device, st)
    return True


def turn(device) -> int:
    """The newest comments on the live answered — on Facebook and aloud. How many were answered."""
    st = state(device)
    if not st:
        return 0
    try:
        page_id, token = page()
        if not token:
            return 0
        video = st.get("video") or live_video(page_id, token)
        if not video:
            return 0
        fresh = new_comments(video, token, page_id, st)
    except Exception as exc:
        logger.warning("robot fblive: couldn't read the live's comments: %s", exc)
        return 0
    if not st.get("video"):
        now = state(device)
        if not now:
            return 0
        now["video"] = video
        _save(device, now)
    # A flood: the robot can't catch up on all of it — the oldest are let go, the newest answered.
    behind = len(fresh) - MOST_A_TURN * 3
    if behind > 0:
        _mark(device, [c["id"] for c in fresh[:behind]])
        fresh = fresh[behind:]
    answered = 0
    for comment in fresh[:MOST_A_TURN]:
        if not _mark(device, [comment["id"]]):
            break
        if _answer(device, comment, token):
            answered += 1
    if answered:
        _mark(device, [], answered)
    return answered


def _answer(device, comment: dict, token: str) -> Optional[str]:
    from omnichannel.services.meta_api import reply_to_comment

    from . import agent, language
    from .models import RobotCommand

    name = comment["name"] or "حضرتك"
    lang = language.detect(comment["message"])
    reply = agent.answer(device, None, comment["message"], lang,
                         context=LIVE_CONTEXT.format(name=name))
    if not reply or SKIP in reply.upper():
        return None
    try:
        reply_to_comment(access_token=token, comment_id=comment["id"], message=reply)
    except Exception as exc:
        logger.warning("robot fblive: reply not posted: %s", exc)
    # Aloud: the answer, to them by name — never the comment itself (whatever a viewer writes
    # isn't read out on the shop's live).
    first = comment["name"].split()[0] if comment["name"] else ""
    said = reply if not first or first in reply else f"يا {first}، {reply}"
    RobotCommand.objects.create(device=device, kind="say", payload={"text": said[:600], "source": "fblive"})
    return reply
