"""Ops-only admin for the Social Studio add-on.

Secrets are never exposed — only a boolean "is set?" hint. Toggling the
subscription here is how Mouss Tec ops grant/extend/revoke the paid add-on after
billing (mirrors the omnichannel admin).
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    AdCampaign,
    PerformanceSnapshot,
    SocialAdsConfig,
    SocialPost,
    StrategyMemory,
)


@admin.register(SocialAdsConfig)
class SocialAdsConfigAdmin(admin.ModelAdmin):
    list_display = (
        "tenant", "subscription_state", "autopilot_mode", "ads_enabled",
        "facebook_page_id", "instagram_account_id", "token_set",
        "llm_provider", "subscription_expires_at", "updated_at",
    )
    list_filter = ("is_subscription_active", "autopilot_mode", "ads_enabled",
                   "llm_provider", "facebook_enabled", "instagram_enabled")
    search_fields = ("tenant__name", "tenant__schema_name",
                     "facebook_page_id", "instagram_account_id", "ad_account_id")
    autocomplete_fields = ("tenant",)
    readonly_fields = ("created_at", "updated_at", "token_set",
                       "subscription_state", "subscription_started_at")
    exclude = ("_page_access_token", "_app_secret", "_llm_api_key")
    actions = ("activate_one_month", "extend_one_month", "make_lifetime",
               "revoke_subscription_action")

    @admin.display(boolean=True, description=_("Page token مضبوط؟"))
    def token_set(self, obj):
        return bool(obj._page_access_token)

    @admin.display(description=_("حالة الاشتراك"))
    def subscription_state(self, obj):
        return obj.subscription_state

    @admin.action(description=_("تفعيل شهر (30 يوماً)"))
    def activate_one_month(self, request, queryset):
        for obj in queryset:
            obj.grant_subscription(timedelta(days=30), by_user=request.user)
        self.message_user(request, _("تم تفعيل الاشتراك للعناصر المختارة."))

    @admin.action(description=_("تمديد شهر إضافي"))
    def extend_one_month(self, request, queryset):
        for obj in queryset:
            obj.grant_subscription(timedelta(days=30), by_user=request.user)
        self.message_user(request, _("تم التمديد."))

    @admin.action(description=_("منح مدى الحياة"))
    def make_lifetime(self, request, queryset):
        for obj in queryset:
            obj.grant_subscription(None, by_user=request.user)
        self.message_user(request, _("تم المنح مدى الحياة."))

    @admin.action(description=_("إلغاء الاشتراك"))
    def revoke_subscription_action(self, request, queryset):
        for obj in queryset:
            obj.revoke_subscription(by_user=request.user)
        self.message_user(request, _("تم الإلغاء."))


@admin.register(SocialPost)
class SocialPostAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "status", "platform", "strategy_angle",
                    "scheduled_at", "published_at", "engagement_rate", "reach")
    list_filter = ("status", "platform", "source", "strategy_angle")
    search_fields = ("tenant__name", "caption", "fb_post_id", "ig_media_id")
    readonly_fields = ("created_at", "updated_at", "published_at", "insights_synced_at",
                       "fb_post_id", "ig_media_id", "engagement_rate")
    date_hierarchy = "created_at"


@admin.register(AdCampaign)
class AdCampaignAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "name", "status", "objective", "daily_budget",
                    "spend", "results", "cost_per_result", "created_at")
    list_filter = ("status", "objective", "source")
    search_fields = ("tenant__name", "name", "meta_campaign_id")
    readonly_fields = ("created_at", "updated_at", "meta_campaign_id", "meta_adset_id",
                       "meta_ad_id", "spend", "ctr", "insights_synced_at")


@admin.register(StrategyMemory)
class StrategyMemoryAdmin(admin.ModelAdmin):
    list_display = ("tenant", "posts_analyzed", "avg_engagement_rate", "last_learned_at")
    search_fields = ("tenant__name",)
    readonly_fields = ("created_at", "updated_at", "last_learned_at")


@admin.register(PerformanceSnapshot)
class PerformanceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "kind", "reach", "impressions", "engagement_rate",
                    "spend", "captured_at")
    list_filter = ("kind",)
    search_fields = ("tenant__name",)
    date_hierarchy = "captured_at"
