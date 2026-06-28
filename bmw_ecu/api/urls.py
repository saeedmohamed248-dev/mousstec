from django.urls import path

from . import smart_views, subscribe, views

app_name = "bmw_ecu_api"

urlpatterns = [
    path("execute", views.execute, name="execute"),
    path("wizard/step", views.wizard_step, name="wizard_step"),

    # ── Smart Auto-Detect (UniversalSmartOrchestrator, persistent session)
    path("smart/step", smart_views.smart_step, name="smart_step"),

    # ── Storefront-facing endpoints (tenant subdomain, any logged-in user)
    path("storefront/packages/", subscribe.list_active_packages,
         name="storefront_packages"),
    path("subscribe/", subscribe.subscribe, name="subscribe"),
]
