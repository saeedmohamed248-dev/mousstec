"""Admin endpoints — registered under /api/admin/ (top-level).

Kept in a separate urlconf from the tenant-facing /api/ecu/ routes so
super-admin reverse-proxying / IP allowlists can target them precisely.
"""
from django.urls import path

from . import admin_views

app_name = "bmw_ecu_admin"

urlpatterns = [
    path("entitlements/gift", admin_views.grant_gift, name="grant_gift"),
    path("entitlements/gift/<int:gift_pk>/revoke", admin_views.revoke_gift,
         name="revoke_gift"),
]
