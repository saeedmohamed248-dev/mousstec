"""Ops-only admin for the Omnichannel add-on.

Secrets are never exposed — only a boolean "is set?" hint. Toggling
`is_subscription_active` here is how Mouss Tec ops enable/disable the paid add-on
for a tenant after billing.
"""
from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import ChannelMessageLog, TenantChannelConfig


@admin.register(TenantChannelConfig)
class TenantChannelConfigAdmin(admin.ModelAdmin):
    list_display = (
        "tenant", "is_subscription_active", "ai_enabled",
        "whatsapp_phone_number_id", "facebook_page_id",
        "token_set", "llm_provider", "updated_at",
    )
    list_filter = ("is_subscription_active", "ai_enabled", "llm_provider",
                   "whatsapp_enabled", "messenger_enabled")
    search_fields = ("tenant__name", "tenant__schema_name",
                     "whatsapp_phone_number_id", "facebook_page_id",
                     "whatsapp_business_account_id")
    autocomplete_fields = ("tenant",)
    readonly_fields = ("created_at", "updated_at", "token_set")
    exclude = ("_meta_access_token", "_app_secret", "_llm_api_key")

    @admin.display(boolean=True, description=_("Access token مضبوط؟"))
    def token_set(self, obj):
        return bool(obj._meta_access_token)


@admin.register(ChannelMessageLog)
class ChannelMessageLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tenant", "channel", "sender_id", "status")
    list_filter = ("channel", "status", "created_at")
    search_fields = ("tenant__name", "sender_id", "inbound_text", "outbound_text")
    readonly_fields = (
        "tenant", "channel", "sender_id", "inbound_text", "outbound_text",
        "status", "error", "meta_message_id", "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False
