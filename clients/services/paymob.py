"""
Shim — the canonical location is now `billing.services.paymob`.

Kept so the four current callers (subscription_views,
parts_marketplace_views, customer_diagnostics_views ×2) keep working
without edits. See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from billing.services.paymob import *  # noqa: F401, F403
