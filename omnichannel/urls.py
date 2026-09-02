"""
URL wiring for the Omnichannel add-on.

Everything is mounted from the project's single urlconf (ROOT == PUBLIC), so both
the public webhook and the tenant dashboard screens resolve here. URL *names* are
project-global (no app_name namespace) so templates/redirects can reverse them
directly: `omnichannel_webhook`, `omnichannel_settings`, `omnichannel_guide`.
"""
from django.urls import path

from .dashboard_views import onboarding_guide, settings_screen
from .views import OmnichannelWebhookView

urlpatterns = [
    # Public webhook — this is the URL tenants paste into their Meta app.
    path("api/webhooks/omnichannel/", OmnichannelWebhookView.as_view(), name="omnichannel_webhook"),

    # Tenant dashboard.
    path("omnichannel/settings/", settings_screen, name="omnichannel_settings"),
    path("omnichannel/guide/", onboarding_guide, name="omnichannel_guide"),
]
