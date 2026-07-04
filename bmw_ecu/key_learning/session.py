"""Persistent session for the BenchOrchestrator (key programming).

HTTP is stateless — each chatbot click is a separate POST — but the bench
key-programming flow is a forward-only state machine. We persist the
*resumable* part (the orchestrator snapshot: state + BenchData) between
requests so the UI can drive it click by click, and a tab refresh /
backend restart resumes from the last confirmed step.

Mirrors `bmw_ecu.universal.session.SmartSessionStore` exactly (same cache
framework, same TTL contract) so both wizards behave identically. No
schema migration required — storage is Django's cache (LocMemCache in
dev/tests, Redis/Memcached in prod).
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from django.core.cache import cache as _default_cache

_KEY_PREFIX = "bmw_ecu:key_session:"
_DEFAULT_TTL = 60 * 60  # 1 hour of inactivity, then the session expires.


@dataclass
class KeyLearningSessionRecord:
    """Everything needed to resume a BenchOrchestrator next request."""
    session_id: str
    snapshot: dict[str, Any] = field(default_factory=dict)   # orch.snapshot()
    vin: str = ""
    family: str = ""            # CAS3 / CAS3+ / FEM / BDC
    used_key: bool = False
    simulator: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KeyLearningSessionRecord":
        return cls(**d)


class KeyLearningSessionStore:
    """Thin cache wrapper. Inject a cache in tests; defaults to Django's."""

    def __init__(self, cache=None, ttl: int = _DEFAULT_TTL) -> None:
        self._cache = cache if cache is not None else _default_cache
        self._ttl = ttl

    @staticmethod
    def new_session_id() -> str:
        return uuid.uuid4().hex

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    def load(self, session_id: str) -> Optional[KeyLearningSessionRecord]:
        raw = self._cache.get(self._key(session_id))
        if not raw:
            return None
        return KeyLearningSessionRecord.from_dict(raw)

    def save(self, record: KeyLearningSessionRecord) -> None:
        self._cache.set(self._key(record.session_id), record.to_dict(), self._ttl)

    def delete(self, session_id: str) -> None:
        self._cache.delete(self._key(session_id))
