"""Ops-only admin for the Omnichannel add-on.

Secrets are never exposed — only a boolean "is set?" hint. Toggling
`is_subscription_active` here is how Mouss Tec ops enable/disable the paid add-on
for a tenant after billing.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import ChannelMessageLog, TenantChannelConfig


@admin.register(TenantChannelConfig)
class TenantChannelConfigAdmin(admin.ModelAdmin):
    list_display = (
        "tenant", "subscription_state", "standalone_mode", "ai_enabled",
        "whatsapp_phone_number_id", "facebook_page_id",
        "token_set", "llm_provider", "subscription_expires_at", "updated_at",
    )
    list_filter = ("is_subscription_active", "standalone_mode", "ai_enabled",
                   "llm_provider", "whatsapp_enabled", "messenger_enabled")
    search_fields = ("tenant__name", "tenant__schema_name",
                     "whatsapp_phone_number_id", "facebook_page_id",
                     "whatsapp_business_account_id")
    autocomplete_fields = ("tenant",)
    readonly_fields = ("created_at", "updated_at", "token_set",
                       "subscription_state", "subscription_started_at")
    exclude = ("_meta_access_token", "_app_secret", "_llm_api_key")
    actions = ("activate_one_month", "extend_one_month",
               "make_lifetime", "revoke_subscription_action")

    @admin.display(boolean=True, description=_("Access token مضبوط؟"))
    def token_set(self, obj):
        return bool(obj._meta_access_token)

    @admin.display(description=_("حالة الاشتراك"))
    def subscription_state(self, obj):
        return obj.subscription_state

    # ── Admin actions (super-admin add/end/extend the add-on) ──────────
    @admin.action(description=_("تفعيل الاشتراك لمدة شهر"))
    def activate_one_month(self, request, queryset):
        n = 0
        for cfg in queryset:
            cfg.grant_subscription(timedelta(days=30), by_user=request.user)
            n += 1
        self.message_user(request, _("تم تفعيل الاشتراك شهراً لـ %(n)d مستأجر.") % {"n": n})

    @admin.action(description=_("تمديد شهر إضافي"))
    def extend_one_month(self, request, queryset):
        n = 0
        for cfg in queryset:
            cfg.grant_subscription(timedelta(days=30), by_user=request.user)
            n += 1
        self.message_user(request, _("تم تمديد الاشتراك شهراً لـ %(n)d مستأجر.") % {"n": n})

    @admin.action(description=_("منح مدى الحياة"))
    def make_lifetime(self, request, queryset):
        n = 0
        for cfg in queryset:
            cfg.grant_subscription(None, by_user=request.user)
            n += 1
        self.message_user(request, _("تم منح وصول مدى الحياة لـ %(n)d مستأجر.") % {"n": n})

    @admin.action(description=_("إنهاء / سحب الاشتراك"))
    def revoke_subscription_action(self, request, queryset):
        n = 0
        for cfg in queryset:
            cfg.revoke_subscription(by_user=request.user)
            n += 1
        self.message_user(request, _("تم سحب الاشتراك من %(n)d مستأجر.") % {"n": n})


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
