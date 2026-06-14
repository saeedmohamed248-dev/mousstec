"""
Tenancy app — SaaS tenant lifecycle, plans, subscriptions, and entitlements.

Phase 2A of the domain-split roadmap (see docs/ARCHITECTURE.md, Wave 2).
At this phase the models still live in `clients.models.tenancy`, but the
business logic — entitlement gating, plan mapping, billing services —
lives here. The `clients/services/` modules of the same name are kept as
thin re-export shims so existing imports keep working.

Phase 2B will move the Plan / Client / TenantSubscription / Feature
models here with `db_table` preserved and `state_operations` migrations,
once a staging dry-run has confirmed the approach is safe for the live
django-tenants schemas.
"""
from django.apps import AppConfig


class TenancyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tenancy'
    verbose_name = 'SaaS Tenancy & Entitlements'

    def ready(self):
        # Plan-based quota enforcement signals on User/Branch/Treasury creation.
        # The file used to live at clients/signals_quota.py and be imported
        # from clients/apps.py; Wave 2 Phase 2A moved both the file and the
        # wiring here so quota enforcement is owned by its proper domain.
        from . import signals  # noqa: F401
        from .signals import quota  # noqa: F401
