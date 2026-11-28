"""robot/brain/pagelog.py — «📜 لوج الروبوت» on its page on the server: the log's newest lines go up
with each heartbeat (server.heartbeat), so a problem is seen — and fixed — without opening the robot's
computer (it's inside the robot). Lines that couldn't go up go with the next beat; the newest
LOG_KEPT are kept meanwhile. The page can also ask for a restart: the heartbeat's answer says so."""

from __future__ import annotations

import collections
from datetime import datetime

LOG_KEPT, LOG_A_BEAT, LOG_LONGEST = 400, 150, 300


class PageLog:
    def __init__(self, show):
        self.show = show                     # where the log is printed here (the window)
        self.unsent = collections.deque(maxlen=LOG_KEPT)

    def __call__(self, line: str) -> None:
        self.show(line)
        self.unsent.append(f"{datetime.now():%H:%M:%S} {line}"[:LOG_LONGEST])

    def take(self) -> list:
        return [self.unsent.popleft() for _ in range(min(len(self.unsent), LOG_A_BEAT))]

    def back(self, lines: list) -> None:
        self.unsent.extendleft(reversed(lines))


def restart_asked(monitor, log) -> bool:
    if (getattr(monitor, "last_heartbeat", None) or {}).get("restart") is True:
        monitor.last_heartbeat = {}
        log("[HB] the robot's page asked for a restart")
        return True
    return False
