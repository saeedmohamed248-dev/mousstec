"""
Shim — the canonical location is now `marketplace_b2b.services.fitment`.

Kept so existing imports keep working
(`from clients.services.fitment import open_wanted_requests`).
See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from marketplace_b2b.services.fitment import *  # noqa: F401, F403
