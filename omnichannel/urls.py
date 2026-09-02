"""
URL wiring for the Omnichannel add-on.

Everything is mounted from the project's single urlconf (ROOT == PUBLIC), so both
the public webhook and the tenant dashboard screens resolve here. URL *names* are
project-global (no app_name namespace) so templates/redirects can reverse them
directly: `omnichannel_webhook`, `omnichannel_settings`, `omnichannel_guide`.
"""
from django.urls import path

from .dashboard_views import (
    onboarding_guide,
    overview,
    pay_with_card,
    paymob_callback,
    public_page,
    settings_screen,
    subscribe,
)
from .views import OmnichannelWebhookView

urlpatterns = [
    # Public webhook — this is the URL tenants paste into their Meta app.
    path("api/webhooks/omnichannel/", OmnichannelWebhookView.as_view(), name="omnichannel_webhook"),

    # Public marketing page (no login) — shown on the main site.
    path("omnichannel-ai/", public_page, name="omnichannel_public"),

    # Tenant dashboard.
    path("omnichannel/", overview, name="omnichannel_overview"),
    path("omnichannel/subscribe/", subscribe, name="omnichannel_subscribe"),          # wallet debit
    path("omnichannel/pay/", pay_with_card, name="omnichannel_pay"),                  # Paymob card
    path("omnichannel/paymob-callback/", paymob_callback, name="omnichannel_paymob_callback"),
    path("omnichannel/settings/", settings_screen, name="omnichannel_settings"),
    path("omnichannel/guide/", onboarding_guide, name="omnichannel_guide"),
]
