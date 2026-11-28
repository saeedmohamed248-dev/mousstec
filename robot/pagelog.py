"""robot/pagelog.py — «📜 لوج الروبوت» on the robot's page: the lines «مخ موس» sends up with each
heartbeat (robot/brain/pagelog.py), the newest MOST kept for a week — what it heard, understood,
answered, and what went wrong — read and acted on without opening the robot's computer. And
«🔄 أعد تشغيل المخ»: the next heartbeat tells it to restart (its watch loop starts it again)."""

from __future__ import annotations

MOST = 600
KEPT_S = 7 * 24 * 3600
LONGEST = 300
A_BEAT = 200


def _key(device, what="lines") -> str:
    return f"robot:pagelog:{what}:{device.pk}"


def keep(device, lines) -> None:
    from django.core.cache import cache

    if not isinstance(lines, list) or not lines:
        return
    new = [str(line)[:LONGEST] for line in lines[:A_BEAT] if str(line).strip()]
    try:
        cache.set(_key(device), (lines_of(device) + new)[-MOST:], KEPT_S)
    except Exception:
        pass                                   # the log is an extra: never the heartbeat's cost


def lines_of(device) -> list:
    from django.core.cache import cache

    try:
        got = cache.get(_key(device))
    except Exception:
        return []
    return list(got) if isinstance(got, list) else []


def ask_restart(device) -> None:
    from django.core.cache import cache

    cache.set(_key(device, "restart"), 1, 10 * 60)


def restart_due(device) -> bool:
    from django.core.cache import cache

    try:
        asked = cache.get(_key(device, "restart"))
        if asked:
            cache.delete(_key(device, "restart"))   # said once
        return bool(asked)
    except Exception:
        return False
