"""What the robot did with the net down that the server must hear about.

Kept on disk, one JSON line per event (`~/.mouss-brain/outbox.jsonl`), so a
restart or a power cut loses nothing; sent to /sync/push/ when the net is
back. Each event carries its own `client_uid`: the server applies it once
however often it's sent (a push cut off halfway is simply sent again).
"""

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

BATCH = 50
TRIES = 5                            # a batch the server keeps failing on is dropped after this


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(self, kind: str, payload: dict) -> str:
        uid = uuid.uuid4().hex
        line = json.dumps({"client_uid": uid, "kind": kind, "payload": payload, "at": time.time()},
                          ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return uid

    def pending(self) -> list:
        with self._lock:
            return self._read()

    def _read(self) -> list:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:               # half a line from a power cut: dropped
                continue
            if isinstance(event, dict) and event.get("client_uid"):
                out.append(event)
        return out

    def flush(self, push: Callable[[list], dict], log=print, on_sent: Callable[[list], None] = None) -> int:
        """Send what's waiting (BATCH at a time). → how many the server took.
        `push` raises when the server is down: everything stays.

        A batch with a failure is sent again next round (what the server
        already applied it skips as a duplicate); after TRIES rounds it's
        dropped, so one bad event can't hold back everything after it."""
        sent = 0
        while True:
            with self._lock:
                batch = self._read()[:BATCH]
            if not batch:
                return sent
            result = push([{k: e[k] for k in ("client_uid", "kind", "payload")} for e in batch]) or {}
            failed = int(result.get("failed") or 0)
            ids = {e["client_uid"] for e in batch}
            with self._lock:
                keep = []
                for event in self._read():
                    if event["client_uid"] in ids:
                        if not failed:
                            continue
                        event["tries"] = int(event.get("tries") or 0) + 1
                        if event["tries"] >= TRIES:
                            log(f"[SYNC] dropped after {TRIES} tries: {event['kind']} {event['client_uid']}")
                            continue
                    keep.append(event)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep), encoding="utf-8")
                tmp.replace(self.path)
            sent += len(batch) - failed
            if failed:
                return sent                  # the rest waits for the next round
            if on_sent is not None:
                on_sent(batch)
