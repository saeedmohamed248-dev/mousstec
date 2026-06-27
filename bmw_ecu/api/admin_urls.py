"""Admin endpoints — registered under /api/admin/ (top-level).

Kept in a separate urlconf from the tenant-facing /api/ecu/ routes so
super-admin reverse-proxying / IP allowlists can target them precisely.
"""
from django.urls import path

from . import admin_packages, admin_views

app_name = "bmw_ecu_admin"

urlpatterns = [
    # ── Legacy gift endpoints ────────────────────────────────────────
    path("entitlements/gift", admin_views.grant_gift, name="grant_gift"),
    path("entitlements/gift/<int:gift_pk>/revoke", admin_views.revoke_gift,
         name="revoke_gift"),

    # ── Granular SaaS — Feature catalog (read-only) ──────────────────
    path("features/", admin_packages.list_features, name="features_list"),

    # ── Granular SaaS — SubscriptionPackage CRUD ─────────────────────
    path("packages/", admin_packages.packages_collection,
         name="packages_collection"),
    path("packages/<int:pk>/", admin_packages.package_detail,
         name="package_detail"),

    # ── Granular SaaS — Tenant grants ────────────────────────────────
    path("grants/", admin_packages.grants_collection,
         name="grants_collection"),
    path("grants/<int:pk>/revoke/", admin_packages.revoke_grant,
         name="revoke_grant"),
]
