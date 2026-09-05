"""
URL wiring for the Social Studio add-on.

Mounted from the project's single (PUBLIC) urlconf. URL names are project-global
(no app_name namespace) so templates/redirects reverse them directly, matching
the omnichannel add-on's convention.
"""
from django.urls import path

from . import studio_views, views

urlpatterns = [
    # Public marketing page (no login).
    path("social-studio-ai/", views.public_page, name="social_ads_public"),

    # Tenant dashboard.
    path("social-studio/", views.overview, name="social_ads_overview"),
    path("social-studio/subscribe/", views.subscribe, name="social_ads_subscribe"),       # wallet debit
    path("social-studio/pay/", views.pay_with_card, name="social_ads_pay"),               # Paymob card
    path("social-studio/paymob-callback/", views.paymob_callback, name="social_ads_paymob_callback"),
    path("social-studio/settings/", views.settings_screen, name="social_ads_settings"),
    path("social-studio/guide/", views.onboarding_guide, name="social_ads_guide"),

    # Studio console (subscribers only).
    path("social-studio/studio/", studio_views.studio_home, name="social_ads_studio"),
    path("social-studio/studio/generate/", studio_views.generate_now, name="social_ads_generate"),
    path("social-studio/studio/learn/", studio_views.run_learning_now, name="social_ads_learn"),
    path("social-studio/studio/post/<int:pk>/edit/", studio_views.post_edit, name="social_ads_post_edit"),
    path("social-studio/studio/post/<int:pk>/approve/", studio_views.post_approve, name="social_ads_post_approve"),
    path("social-studio/studio/post/<int:pk>/publish/", studio_views.post_publish_now, name="social_ads_post_publish"),
    path("social-studio/studio/post/<int:pk>/delete/", studio_views.post_delete, name="social_ads_post_delete"),

    # Campaigns.
    path("social-studio/campaigns/", studio_views.campaigns, name="social_ads_campaigns"),
    path("social-studio/campaigns/create/", studio_views.campaign_create, name="social_ads_campaign_create"),
    path("social-studio/campaigns/<int:pk>/launch/", studio_views.campaign_launch, name="social_ads_campaign_launch"),
    path("social-studio/campaigns/<int:pk>/pause/", studio_views.campaign_pause, name="social_ads_campaign_pause"),
]
