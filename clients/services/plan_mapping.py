"""
Shim — the canonical location is now `tenancy.services.plan_mapping`.

Kept so existing
`from clients.services.plan_mapping import LEGACY_TO_PLAN_SLUG, resolve_plan_slug`
imports keep working. See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from tenancy.services.plan_mapping import *  # noqa: F401, F403
