"""
Shim — the canonical location is now `tenancy.middleware.quota`.

Kept so any caller that still imports `from clients.middleware import
TenantQuotaMiddleware` keeps working. The MIDDLEWARE entry in
erp_core/settings.py has already been updated to the new path.
See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from tenancy.middleware.quota import TenantQuotaMiddleware  # noqa: F401
