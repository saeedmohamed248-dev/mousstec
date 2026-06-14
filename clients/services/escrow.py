"""
Shim — the canonical location is now `marketplace_b2b.services.escrow`.

Kept so existing imports keep working
(`from clients.services import escrow as escrow_svc` in
clients/views/parts_marketplace_views.py).
See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from marketplace_b2b.services.escrow import *  # noqa: F401, F403
