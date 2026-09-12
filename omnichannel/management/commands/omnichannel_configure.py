"""
One-shot Omnichannel setup for a tenant — reads every secret from the
environment so nothing sensitive ever lands in source control or shell history
files (pass them inline on the command that runs the container).

Usage (run on the server, secrets provided inline):

    FIXIT_META_TOKEN=EAA... \
    FIXIT_APP_SECRET=abc123 \
    FIXIT_VERIFY_TOKEN=Fixit_Webhook_2026 \
    FIXIT_WA_PHONE_ID=123456789 \
    FIXIT_WABA_ID=123456789 \
    FIXIT_FB_PAGE_ID=123456789 \
    FIXIT_IG_ID=123456789 \
    docker compose exec -T web python manage.py omnichannel_configure \
        --schema fixit_02e0 --lifetime --business-name "FixIt"

Only the variables you provide are written; omit one to leave that field
unchanged. Re-running is safe (idempotent upsert).
"""
from __future__ import annotations

import os
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

ENV_MAP = {
    # env var            -> (attribute, is_secret)
    "FIXIT_META_TOKEN": ("meta_access_token", True),
    "FIXIT_APP_SECRET": ("app_secret", True),
    "FIXIT_VERIFY_TOKEN": ("webhook_verify_token", False),
    "FIXIT_WA_PHONE_ID": ("whatsapp_phone_number_id", False),
    "FIXIT_WABA_ID": ("whatsapp_business_account_id", False),
    "FIXIT_FB_PAGE_ID": ("facebook_page_id", False),
    "FIXIT_IG_ID": ("instagram_account_id", False),
    "FIXIT_LLM_PROVIDER": ("llm_provider", False),   # platform | openai | gemini
    "FIXIT_LLM_KEY": ("llm_api_key", True),
    "FIXIT_LLM_MODEL": ("llm_model", False),
}


class Command(BaseCommand):
    help = "Configure the Omnichannel add-on for a tenant (Meta creds + channels + subscription)."

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True, help="Tenant schema name, e.g. fixit_02e0")
        parser.add_argument("--business-name", default="", help="Display name shown to customers")
        parser.add_argument("--lifetime", action="store_true", help="Grant a lifetime subscription")
        parser.add_argument("--months", type=int, default=0, help="Grant N months of subscription")
        parser.add_argument(
            "--no-enable", action="store_true",
            help="Do not flip channel/AI enable flags (only write provided fields)",
        )

    def handle(self, *args, **opts):
        from clients.models import Client
        from omnichannel.models import TenantChannelConfig

        schema = opts["schema"]
        client = Client.objects.filter(schema_name=schema).first()
        if not client:
            raise CommandError(f"مفيش تينانت بالـ schema '{schema}'")

        cfg, created = TenantChannelConfig.objects.get_or_create(tenant=client)

        # ── write only the fields whose env var is present ────────────────
        written = []
        for env_var, (attr, is_secret) in ENV_MAP.items():
            val = os.environ.get(env_var)
            if val:
                setattr(cfg, attr, val)  # property setters encrypt secrets
                written.append(attr if not is_secret else f"{attr} (encrypted)")

        if opts["business_name"]:
            cfg.business_display_name = opts["business_name"]
        elif not cfg.business_display_name:
            cfg.business_display_name = "FixIt"

        if not opts["no_enable"]:
            cfg.whatsapp_enabled = True
            cfg.messenger_enabled = True
            cfg.instagram_enabled = True
            cfg.web_widget_enabled = True
            cfg.ai_enabled = True

        cfg.save()
        widget_key = cfg.ensure_web_widget_key()

        # ── subscription ─────────────────────────────────────────────────
        if opts["lifetime"]:
            cfg.grant_subscription(None)
        elif opts["months"] > 0:
            cfg.grant_subscription(timedelta(days=30 * opts["months"]))

        # ── secret-free report ───────────────────────────────────────────
        w = self.stdout.write
        style = self.style
        w(style.SUCCESS(f"\n✅ إعدادات الأتمتة {'اتعملت' if created else 'اتحدّثت'} لتينانت: {schema}"))
        w(f"   الاسم التجاري      : {cfg.business_display_name}")
        w(f"   الحقول اللي اتكتبت : {', '.join(written) or '(مفيش — كله زي ما هو)'}")
        w(f"   واتساب Phone ID    : {cfg.whatsapp_phone_number_id or '-'}")
        w(f"   Facebook Page ID   : {cfg.facebook_page_id or '-'}")
        w(f"   Instagram Acc ID   : {cfg.instagram_account_id or '-'}")
        w(f"   مزوّد الـ AI        : {cfg.llm_provider}")
        w(f"   حالة الاشتراك      : {cfg.subscription_state}")
        w(f"   الأتمتة شغّالة؟     : {'أيوة ✅' if cfg.is_operational else 'لأ ❌ (راجع الاشتراك/التوكن)'}")
        w("")
        w(style.HTTP_INFO("   رابط الـ Webhook لتطبيق Meta:"))
        w("   https://mousstec.com/api/webhooks/omnichannel/")
        w(style.HTTP_INFO("   كود شات الموقع (حطّه في صفحات fixitauto.parts):"))
        w(f'   <script src="https://mousstec.com/omnichannel/widget/{widget_key}.js" defer></script>')
        w("")
