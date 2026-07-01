"""Persistent session for the DmeSwapOrchestrator.

HTTP is stateless but the used-DME swap is a long, physical, multi-click job —
especially once it diverts into the paused BSL fallback wizard, where the
technician may spend minutes on the bench (opening the DME casing, bridging the
boot pin, wiring the FTDI) between one click and the next. So we persist the
*resumable* parts between requests exactly like the Smart flow does:

    • the orchestrator snapshot (state + SwapData, incl. DME_BSL_FALLBACK +
      uds_reject_nrc)                                            → restore()
    • how to rebuild the provider (simulator? transport cfg, profile)

On each request we: load the record → rebuild the provider → restore the
orchestrator → handle the event → save the updated record. This is what lets
the BSL wizard survive a laptop sleep mid-bench-work and resume click-by-click.

Storage is Django's cache framework (LocMemCache in dev/tests, Redis in prod),
so no schema migration is required. A separate key prefix keeps swap sessions
from ever colliding with Smart sessions.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from django.core.cache import cache as _default_cache

_KEY_PREFIX = "bmw_ecu:swap_session:"
_DEFAULT_TTL = 60 * 60  # 1 hour of inactivity — bench work is slow; be generous.


@dataclass
class SwapSessionRecord:
    """Everything needed to resume a DmeSwapOrchestrator next request."""
    session_id: str
    snapshot: dict[str, Any] = field(default_factory=dict)   # orch.snapshot()
    profile_key: str = ""
    vin: str = ""
    simulator: bool = False
    transport: dict[str, Any] = field(default_factory=dict)  # transport cfg
    # Simulator-only knobs so the mock reproduces the same fallback scenario each
    # step (e.g. uds_reject_nrc to demo the UDS→BSL divert without hardware).
    sim: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SwapSessionRecord":
        return cls(**d)


class SwapSessionStore:
    """Thin cache wrapper. Inject a cache in tests; defaults to Django's."""

    def __init__(self, cache=None, ttl: int = _DEFAULT_TTL) -> None:
        self._cache = cache if cache is not None else _default_cache
        self._ttl = ttl

    @staticmethod
    def new_session_id() -> str:
        return uuid.uuid4().hex

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    def load(self, session_id: str) -> Optional[SwapSessionRecord]:
        raw = self._cache.get(self._key(session_id))
        if not raw:
            return None
        return SwapSessionRecord.from_dict(raw)

    def save(self, record: SwapSessionRecord) -> None:
        self._cache.set(self._key(record.session_id), record.to_dict(), self._ttl)

    def delete(self, session_id: str) -> None:
        self._cache.delete(self._key(session_id))
