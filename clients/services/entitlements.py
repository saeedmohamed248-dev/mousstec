"""
Shim — the canonical location is now `tenancy.services.entitlements`.

This file is kept so existing imports
(`from clients.services.entitlements import require_feature` etc.) keep
working untouched while we migrate callers in follow-up commits.
See docs/ARCHITECTURE.md ADR-001 / Wave 2 Phase 2A for rationale.
"""
from tenancy.services.entitlements import *  # noqa: F401, F403
from tenancy.services.entitlements import EntitlementService, require_feature  # noqa: F401
