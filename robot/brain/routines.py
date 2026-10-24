"""The shop's day, on the robot: the Quran when it opens, a word before it closes.

In brain.ini (see brain.ini.example):

    [day]
    open  = 12:00
    close = 23:00
    days  = all                  ; or: sat,sun,mon,tue,wed,thu  (السبت,الأحد,…)

    [routine quran]              ; any section whose name starts with "routine"
    from = open                  ; a window — plays from …
    to   = open+2:00             ; … until
    play = quran

    [routine closing]
    at  = close-0:30             ; a moment — once a day
    say = فاضل نص ساعة على القفل

Times: «12:00», «open», «close», «open+2:00», «close-0:30», «open+90» (minutes).
A window that ends after midnight (close = 01:00) works too.

What people say wins over the schedule: the Quran stopped by hand isn't
started again that day, and music someone chose isn't replaced. After a
restart inside a window, what the schedule had started starts again.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

GRACE_MIN = 10                       # a «say» missed by more than this isn't said late
_DAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "الاتنين": 0, "الاثنين": 0, "التلات": 1, "الثلاثاء": 1, "الاربع": 2, "الأربعاء": 2,
    "الاربعاء": 2, "الخميس": 3, "الجمعة": 4, "الجمعه": 4, "السبت": 5, "الأحد": 6, "الاحد": 6,
}
_SPEC = re.compile(r"^\s*(open|close|\d{1,2}:\d{2})\s*(?:([+-])\s*(\d{1,2}:\d{2}|\d+))?\s*$")


def _minutes(clock: str) -> int:
    h, m = clock.split(":")
    return int(h) * 60 + int(m)


def resolve(spec: str, open_min: int, close_min: int) -> Optional[int]:
    """«open+2:00» → minutes after midnight (wrapped to the day), or None."""
    m = _SPEC.match(spec or "")
    if not m:
        return None
    base, sign, offset = m.groups()
    value = open_min if base == "open" else close_min if base == "close" else _minutes(base)
    if offset:
        delta = _minutes(offset) if ":" in offset else int(offset)
        value += delta if sign == "+" else -delta
    return value % (24 * 60)


@dataclass
class Routine:
    name: str
    start: int                       # minutes after midnight
    end: Optional[int] = None        # set: a window; None: a moment
    play: str = ""
    say: str = ""

    def inside(self, minute: int) -> bool:
        if self.end is None:
            return False
        if self.start <= self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end      # past midnight

    def opened_on(self, now: datetime) -> str:
        """The day this window opened: yesterday, for 00:30 in a 23:00–01:00 one."""
        minute = now.hour * 60 + now.minute
        past_midnight = self.end is not None and self.start > self.end and minute < self.end
        return (now.date() - timedelta(days=1) if past_midnight else now.date()).isoformat()


class Routines:
    def __init__(self, routines: list, *, days: Optional[set] = None, state_path: Optional[Path] = None):
        self.routines = routines
        self.days = days                     # None = every day
        self.state_path = Path(state_path) if state_path else None
        self.state = self._load()
        self._started = {}                   # routine → the day this run started it

    @classmethod
    def from_ini(cls, ini, state_path=None, log=print) -> "Routines":
        get = lambda sec, key: ini.get(sec, key, fallback="").strip()   # noqa: E731
        open_min = resolve(get("day", "open") or "09:00", 0, 0)
        close_min = resolve(get("day", "close") or "23:00", 0, 0)
        names = [d.strip().lower() for d in re.split(r"[,،\s]+", get("day", "days")) if d.strip()]
        days = None if not names or "all" in names else {_DAYS[d] for d in names if d in _DAYS}
        routines = []
        for section in ini.sections():
            if not section.lower().startswith("routine"):
                continue
            name = section[len("routine"):].strip() or section
            play, say = get(section, "play").lower(), get(section, "say")
            if get(section, "from"):
                start = resolve(get(section, "from"), open_min, close_min)
                end = resolve(get(section, "to") or "close", open_min, close_min)
                if start is None or end is None or not play:
                    log(f"[DAY] routine «{name}»: needs from/to times and play = quran|calm")
                    continue
                routines.append(Routine(name, start, end, play=play))
            elif get(section, "at"):
                at = resolve(get(section, "at"), open_min, close_min)
                if at is None or not (say or play):
                    log(f"[DAY] routine «{name}»: needs an at time and say = … (or play = …)")
                    continue
                routines.append(Routine(name, at, None, play=play, say=say))
            else:
                log(f"[DAY] routine «{name}»: needs from/to (a window) or at (a moment)")
        return cls(routines, days=days, state_path=state_path)

    # ---- state that survives a restart: what was said today, what was stopped by hand
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def stopped_by_hand(self, kind: str, now: datetime) -> None:
        """Someone stopped (or replaced) `kind`: the schedule leaves it be today."""
        minute = now.hour * 60 + now.minute
        changed = False
        for r in self.routines:
            if r.play == kind and r.inside(minute):
                self.state.setdefault("stopped", {})[r.name] = r.opened_on(now)
                changed = True
        if changed:
            self._save()

    # ---- what to do now
    def due(self, now: datetime, playing: str) -> list:
        """[("play", kind) | ("stop", kind) | ("say", text)] for this moment."""
        if self.days is not None and now.weekday() not in self.days:
            return []
        day, minute = now.date().isoformat(), now.hour * 60 + now.minute
        actions, changed = [], False
        for r in self.routines:
            if r.end is not None:                                  # a window
                if r.inside(minute):
                    opened = r.opened_on(now)
                    stopped = self.state.get("stopped", {}).get(r.name) == opened
                    if self._started.get(r.name) != opened and not stopped and playing == "":
                        actions.append(("play", r.play))
                        self._started[r.name] = opened
                elif r.name in self._started:
                    del self._started[r.name]
                    if playing == r.play:
                        actions.append(("stop", r.play))
                continue
            if r.start <= minute < r.start + GRACE_MIN and self.state.get("said", {}).get(r.name) != day:
                actions.append(("say", r.say) if r.say else ("play", r.play))
                self.state.setdefault("said", {})[r.name] = day
                changed = True
        if changed:
            self._save()
        return actions
