from django.urls import path

from . import subscribe, views

app_name = "bmw_ecu_api"

urlpatterns = [
    path("execute", views.execute, name="execute"),
    path("wizard/step", views.wizard_step, name="wizard_step"),

    # ── Storefront-facing endpoints (tenant subdomain, any logged-in user)
    path("storefront/packages/", subscribe.list_active_packages,
         name="storefront_packages"),
    path("subscribe/", subscribe.subscribe, name="subscribe"),
]
