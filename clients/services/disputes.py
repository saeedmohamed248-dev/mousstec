"""
Shim — the canonical location is now `marketplace_c2c.services.disputes`.

Kept so `from clients.services.disputes import open_dispute` in
parts_marketplace_views.py keeps working. See docs/ARCHITECTURE.md
Wave 2 Phase 2A.
"""
from marketplace_c2c.services.disputes import *  # noqa: F401, F403
