"""Tenant-facing settings form for the Omnichannel add-on.

Secrets are handled specially: the form never renders the stored plaintext back
(only a "•••• saved" hint via the template), and a blank secret field means
"keep the existing value" rather than "erase it".
"""
from __future__ import annotations

from django import forms

from clients.services.meta_fields import (
    clean_app_secret, clean_meta_token, clean_numeric_id, guard_id_widgets, secret_widget,
)
from django.utils.translation import gettext_lazy as _

from .models import TenantChannelConfig

_SECRET_PLACEHOLDER = "••••••••"


class TenantChannelConfigForm(forms.ModelForm):
    meta_access_token = forms.CharField(
        required=False, widget=secret_widget(),
        label=_("Meta Access Token"),
        help_text=_("اتركه فارغاً للإبقاء على القيمة المحفوظة."),
    )
    app_secret = forms.CharField(
        required=False, widget=secret_widget(),
        label=_("Meta App Secret"),
        help_text=_("اتركه فارغاً للإبقاء على القيمة المحفوظة."),
    )
    llm_api_key = forms.CharField(
        required=False, widget=secret_widget(),
        label=_("مفتاح LLM الخاص بالشركة"),
        help_text=_("اتركه فارغاً للإبقاء على القيمة المحفوظة."),
    )

    class Meta:
        model = TenantChannelConfig
        fields = [
            "whatsapp_enabled", "messenger_enabled", "instagram_enabled",
            "whatsapp_phone_number_id", "whatsapp_business_account_id",
            "facebook_page_id", "instagram_account_id", "web_widget_enabled",
            "webhook_verify_token",
            "business_display_name", "tone_of_voice", "discount_policy",
            "custom_instructions", "fallback_message", "max_reply_chars",
            "notify_on_handoff", "notify_email",
            "llm_provider", "llm_model", "ai_enabled",
        ]
        widgets = {
            "discount_policy": forms.Textarea(attrs={"rows": 3}),
            "custom_instructions": forms.Textarea(attrs={"rows": 4}),
            "fallback_message": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 🐛 [FIX]: المتصفح كان بيملى إيميل/باسورد المالك في خانات Meta — clients/services/meta_fields.py
        guard_id_widgets(self, "facebook_page_id", "instagram_account_id", "whatsapp_phone_number_id", "whatsapp_business_account_id")

    def clean_meta_access_token(self):
        return clean_meta_token(self.cleaned_data.get("meta_access_token"))

    def clean_app_secret(self):
        return clean_app_secret(self.cleaned_data.get("app_secret"))

    def clean_facebook_page_id(self):
        return clean_numeric_id(self.cleaned_data.get("facebook_page_id"), "رقم صفحة فيسبوك")

    def clean_instagram_account_id(self):
        return clean_numeric_id(self.cleaned_data.get("instagram_account_id"), "رقم حساب إنستجرام")

    def clean_whatsapp_phone_number_id(self):
        return clean_numeric_id(self.cleaned_data.get("whatsapp_phone_number_id"), "Phone Number ID")

    def clean_whatsapp_business_account_id(self):
        return clean_numeric_id(self.cleaned_data.get("whatsapp_business_account_id"), "WhatsApp Business Account ID")

    def save(self, commit: bool = True):
        instance = super().save(commit=False)
        # Only overwrite a secret when the user actually typed a new one.
        token = self.cleaned_data.get("meta_access_token")
        if token:
            instance.meta_access_token = token
        secret = self.cleaned_data.get("app_secret")
        if secret:
            instance.app_secret = secret
        llm_key = self.cleaned_data.get("llm_api_key")
        if llm_key:
            instance.llm_api_key = llm_key
        if commit:
            instance.save()
        return instance
