"""Tenant-facing settings form for the Social Studio add-on.

Secrets (page token, app secret, BYO LLM key) are write-only: the stored value
is never rendered back, and a blank field means "keep the existing value".
"""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import SocialAdsConfig, SocialPost


class SocialAdsConfigForm(forms.ModelForm):
    page_access_token = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=False),
        label=_("Page Access Token"),
        help_text=_("اتركه فارغاً للإبقاء على القيمة المحفوظة."),
    )
    app_secret = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=False),
        label=_("Meta App Secret"),
        help_text=_("اتركه فارغاً للإبقاء على القيمة المحفوظة."),
    )
    llm_api_key = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=False),
        label=_("مفتاح LLM الخاص بالشركة"),
        help_text=_("اتركه فارغاً للإبقاء على القيمة المحفوظة."),
    )

    class Meta:
        model = SocialAdsConfig
        fields = [
            # channels + credentials
            "facebook_enabled", "instagram_enabled", "ads_enabled",
            "facebook_page_id", "instagram_account_id", "ad_account_id",
            # brand profile
            "business_display_name", "industry", "products_services",
            "target_audience", "brand_tone", "brand_keywords",
            "business_goals", "call_to_action", "contact_phone", "website_url",
            "banned_words",
            # autopilot + cadence
            "autopilot_mode", "posts_per_week", "preferred_times",
            "default_language", "generate_images",
            # ad guardrails
            "monthly_ad_budget", "max_daily_ad_budget", "auto_optimize_ads",
            # AI provider
            "llm_provider", "llm_model",
            # notifications
            "notify_email", "notify_on_publish",
        ]
        widgets = {
            "products_services": forms.Textarea(attrs={"rows": 3}),
            "target_audience": forms.Textarea(attrs={"rows": 2}),
            "brand_tone": forms.TextInput(),
        }

    def clean_posts_per_week(self):
        n = self.cleaned_data.get("posts_per_week") or 5
        return max(1, min(int(n), 21))

    def save(self, commit: bool = True):
        instance = super().save(commit=False)
        if self.cleaned_data.get("page_access_token"):
            instance.page_access_token = self.cleaned_data["page_access_token"]
        if self.cleaned_data.get("app_secret"):
            instance.app_secret = self.cleaned_data["app_secret"]
        if self.cleaned_data.get("llm_api_key"):
            instance.llm_api_key = self.cleaned_data["llm_api_key"]
        if commit:
            instance.save()
        return instance


class QuickPostForm(forms.ModelForm):
    """Manual/edit form used in the studio for a single post."""

    class Meta:
        model = SocialPost
        fields = ["platform", "caption", "hashtags", "image_url", "image_prompt",
                  "strategy_angle", "scheduled_at"]
        widgets = {
            "caption": forms.Textarea(attrs={"rows": 5}),
            "hashtags": forms.TextInput(),
            "image_prompt": forms.Textarea(attrs={"rows": 2}),
            "scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }
