"""
Shim — the canonical location is now `marketplace_c2c.services.trust`.

Kept so existing callers in clients/views/parts_marketplace_views.py and
marketplace_b2b/tests/test_marketplace_phase2_kyc.py keep working.
See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from marketplace_c2c.services.trust import *  # noqa: F401, F403
