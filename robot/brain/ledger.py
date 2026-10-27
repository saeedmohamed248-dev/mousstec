"""What the counter page sold that the catalog on disk doesn't know yet.

The catalog's stock is the server's at the time it was fetched. A part sold
on the counter page since is still in it: in the outbox (not sent yet), or
sent and not in a catalog fetched since. The page checks what's left against
the catalog minus both — so «a part that's out isn't sold» holds through a
day with the net down, and through the minutes between sending and the next
catalog.
"""

import json
import threading
from collections import Counter
from pathlib import Path


def key(part_number: str, branch_id=None) -> str:
    """What's sold of a part is counted per branch: this robot's own branch by
    the part number, another branch («بيع لفرع تاني» on the counter page) as
    «<branch id>/<part number>»."""
    return f"{branch_id}/{part_number}" if branch_id not in (None, "") else str(part_number)


def _items(events) -> Counter:
    sold = Counter()
    for event in events:
        if event.get("kind") != "sale":
            continue
        payload = event.get("payload") or {}
        for item in payload.get("items") or []:
            try:
                sold[key(str(item["part_number"]), payload.get("branch_id"))] += int(item["qty"])
            except (KeyError, TypeError, ValueError):
                continue
    return sold


class Ledger:
    def __init__(self, path: Path, outbox):
        self.path = Path(path)
        self.outbox = outbox
        self._lock = threading.Lock()

    def _sent(self) -> Counter:
        try:
            return Counter({str(k): int(v) for k, v in json.loads(self.path.read_text(encoding="utf-8")).items()})
        except (OSError, ValueError, AttributeError, TypeError):
            return Counter()

    def _save(self, sent: Counter) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sent), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def sold(self) -> Counter:
        """{key(part_number, branch): qty} sold here that the catalog doesn't show yet."""
        with self._lock:
            return _items(self.outbox.pending()) + self._sent()

    def sent(self, events) -> None:
        """The server took these: their parts wait for the next catalog."""
        sold = _items(events)
        if sold:
            with self._lock:
                self._save(self._sent() + sold)

    def catalog_fetched(self) -> None:
        """A catalog fetched after the sending has them in its stock."""
        with self._lock:
            if self.path.exists():
                self._save(Counter())

    def waiting_for_catalog(self) -> bool:
        with self._lock:
            return bool(self._sent())
