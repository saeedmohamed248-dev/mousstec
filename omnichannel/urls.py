"""
URL wiring for the Omnichannel add-on.

Everything is mounted from the project's single urlconf (ROOT == PUBLIC), so both
the public webhook and the tenant dashboard screens resolve here. URL *names* are
project-global (no app_name namespace) so templates/redirects can reverse them
directly: `omnichannel_webhook`, `omnichannel_settings`, `omnichannel_guide`.
"""
from django.urls import path

from . import console_views
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

    # Dedicated console (subscribers only) — overview / inbox / contacts.
    path("omnichannel/console/", console_views.console_home, name="omnichannel_console"),
    path("omnichannel/console/inbox/", console_views.console_inbox, name="omnichannel_console_inbox"),
    path("omnichannel/console/contacts/", console_views.console_contacts, name="omnichannel_console_contacts"),
    path("omnichannel/console/test/", console_views.console_test, name="omnichannel_console_test"),
    path("omnichannel/console/numbers/", console_views.console_numbers, name="omnichannel_console_numbers"),
    path("omnichannel/console/numbers/buy/", console_views.console_buy_numbers, name="omnichannel_console_buy_numbers"),
    path("omnichannel/console/numbers/<int:pk>/delete/", console_views.console_number_delete, name="omnichannel_console_number_delete"),
    path("omnichannel/console/export/contacts.csv", console_views.console_export_contacts, name="omnichannel_console_export_contacts"),
    path("omnichannel/console/export/messages.csv", console_views.console_export_conversations, name="omnichannel_console_export_conversations"),
    path("omnichannel/console/c/<str:channel>/<str:sender_id>/reply/",
         console_views.console_reply, name="omnichannel_console_reply"),
    path("omnichannel/console/c/<str:channel>/<str:sender_id>/",
         console_views.console_conversation, name="omnichannel_console_conversation"),
]
