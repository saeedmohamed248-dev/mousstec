"""Tenant-facing settings form for the Omnichannel add-on.

Secrets are handled specially: the form never renders the stored plaintext back
(only a "•••• saved" hint via the template), and a blank secret field means
"keep the existing value" rather than "erase it".
"""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import TenantChannelConfig

_SECRET_PLACEHOLDER = "••••••••"


class TenantChannelConfigForm(forms.ModelForm):
    meta_access_token = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=False),
        label=_("Meta Access Token"),
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
        model = TenantChannelConfig
        fields = [
            "whatsapp_enabled", "messenger_enabled",
            "whatsapp_phone_number_id", "whatsapp_business_account_id",
            "facebook_page_id", "webhook_verify_token",
            "business_display_name", "tone_of_voice", "discount_policy",
            "custom_instructions", "fallback_message", "max_reply_chars",
            "llm_provider", "llm_model", "ai_enabled",
        ]
        widgets = {
            "discount_policy": forms.Textarea(attrs={"rows": 3}),
            "custom_instructions": forms.Textarea(attrs={"rows": 4}),
            "fallback_message": forms.Textarea(attrs={"rows": 2}),
        }

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
