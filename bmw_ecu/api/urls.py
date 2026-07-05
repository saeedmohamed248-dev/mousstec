from django.urls import path

from . import (
    cable_views, diagnose_views, key_views, smart_views, subscribe,
    swap_views, views,
)

app_name = "bmw_ecu_api"

urlpatterns = [
    path("execute", views.execute, name="execute"),
    path("wizard/step", views.wizard_step, name="wizard_step"),

    # ── Smart Auto-Detect (UniversalSmartOrchestrator, persistent session)
    path("smart/step", smart_views.smart_step, name="smart_step"),
    # Friendly alias matching the product spec name; same handler.
    path("smart-detect/", smart_views.smart_step, name="smart_detect"),

    # ── Used-DME swap (DmeSwapOrchestrator, persistent session, BSL fallback)
    path("swap/step", swap_views.swap_step, name="swap_step"),

    # ── Key programming (BenchOrchestrator, persistent session, used-key path)
    path("key/step", key_views.key_step, name="key_step"),

    # ── Engine-swap Auto-Diagnose (single-shot ISN mismatch + FA engine)
    path("diagnose/swap", diagnose_views.swap_diagnose, name="swap_diagnose"),

    # ── Cable connectivity check (CANable/D-CAN Tester-Present probe)
    path("cable/ping", cable_views.cable_ping, name="cable_ping"),

    # ── Storefront-facing endpoints (tenant subdomain, any logged-in user)
    path("storefront/packages/", subscribe.list_active_packages,
         name="storefront_packages"),
    path("subscribe/", subscribe.subscribe, name="subscribe"),
]
