"""
Shim — the canonical location is now `workshop.predictive_engine`.

Kept so existing imports keep working without edits:
    inventory/tasks.py:407       → refresh_all_nudges
    inventory/signals.py:363     → compute_nudges_for_vehicle
    inventory/views/vehicles.py  → compute_nudges_for_vehicle
    inventory/views/service.py   → refresh_all_nudges
    workshop/tests/test_predictive_engine.py → references underscore-
        prefixed helpers (_classify_urgency, _matches_category) which
        `import *` would skip.

See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from workshop.predictive_engine import *  # noqa: F401, F403
from workshop.predictive_engine import (  # noqa: F401
    _matches_category,
    _last_done_for_category,
    _classify_urgency,
    _upsert_nudges,
)
