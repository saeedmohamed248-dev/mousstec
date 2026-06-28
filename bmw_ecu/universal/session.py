"""Persistent session for the UniversalSmartOrchestrator.

The orchestrator is a forward-only state machine, but HTTP is stateless: each
chatbot click is a separate POST. We can't keep a live socket (or the
orchestrator object) between requests, so we persist the *resumable* parts:

    • the orchestrator snapshot (state + UData)               → restore()
    • the backup's SHA + identity                             → reload EcuBackup
    • how to rebuild the live I/O (transport cfg, profile)    → reconnect

On each request we: load the record → rebuild I/O → restore the orchestrator →
handle the event → save the updated record. The actual backup bytes live in the
content-addressed `BackupStore` (disk + DB mirror), so rollback survives even a
laptop reboot — we only need to remember the SHA here.

Storage is Django's cache framework (LocMemCache in dev/tests, Redis/Memcached
in prod) — no schema migration required.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from django.core.cache import cache as _default_cache

_KEY_PREFIX = "bmw_ecu:smart_session:"
_DEFAULT_TTL = 60 * 60  # 1 hour of inactivity, then the session expires.


@dataclass
class SmartSessionRecord:
    """Everything needed to resume a UniversalSmartOrchestrator next request."""
    session_id: str
    snapshot: dict[str, Any] = field(default_factory=dict)   # orch.snapshot()
    backup_sha256: str = ""
    backup_ecu_name: str = ""
    vin: str = ""
    profile_name: str = ""
    simulator: bool = False
    transport: dict[str, Any] = field(default_factory=dict)  # transport cfg
    # Simulator-only knobs so the mock reproduces the same vehicle each step.
    sim: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SmartSessionRecord":
        return cls(**d)


class SmartSessionStore:
    """Thin cache wrapper. Inject a cache in tests; defaults to Django's."""

    def __init__(self, cache=None, ttl: int = _DEFAULT_TTL) -> None:
        self._cache = cache if cache is not None else _default_cache
        self._ttl = ttl

    @staticmethod
    def new_session_id() -> str:
        return uuid.uuid4().hex

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    def load(self, session_id: str) -> Optional[SmartSessionRecord]:
        raw = self._cache.get(self._key(session_id))
        if not raw:
            return None
        return SmartSessionRecord.from_dict(raw)

    def save(self, record: SmartSessionRecord) -> None:
        self._cache.set(self._key(record.session_id), record.to_dict(), self._ttl)

    def delete(self, session_id: str) -> None:
        self._cache.delete(self._key(session_id))
