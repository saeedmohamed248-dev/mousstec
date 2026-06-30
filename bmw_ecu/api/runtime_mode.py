"""Runtime hardware/simulator mode — one source of truth for both API paths.

Two env switches, with a deliberate safety asymmetry (reliability over
convenience, per workshop policy):

  • ``BMW_ECU_SIMULATOR``        opt-in dev/demo Mock (default OFF).
  • ``BMW_ECU_REQUIRE_HARDWARE`` production hardware LOCK (default OFF).

The lock WINS. When ``BMW_ECU_REQUIRE_HARDWARE`` is truthy the server can
NEVER fall back to the simulator — even if ``BMW_ECU_SIMULATOR`` is also set
(e.g. left over in a shell, a stale unit file). We refuse to silently serve
fake ECU data on a bench that's wired to a real car. Instead the live path
runs and, if no interface answers, surfaces an honest "Hardware Not Found".
"""
from __future__ import annotations

import os

from ..logging_setup import get_logger

log = get_logger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def require_hardware() -> bool:
    """Production lock: live hardware only, simulator can never engage."""
    return _truthy("BMW_ECU_REQUIRE_HARDWARE")


def simulator_enabled() -> bool:
    """Whether the in-process Mock should be used instead of real hardware.

    Hardware-locked mode forces this False and warns if a stale SIMULATOR
    flag was present, so a leftover env var can never silently fake data.
    """
    sim = _truthy("BMW_ECU_SIMULATOR")
    if require_hardware():
        if sim:
            log.warning(
                "BMW_ECU_SIMULATOR is set but BMW_ECU_REQUIRE_HARDWARE wins — "
                "refusing to simulate; running live hardware only.")
        return False
    return sim
