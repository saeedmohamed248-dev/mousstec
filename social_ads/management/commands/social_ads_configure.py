"""
One-shot Social Studio (social_ads) setup for a tenant.

Reads every secret from the environment so nothing sensitive lands in source
control or shell history (pass them inline on the command that runs the
container). SAFETY: paid ads stay OFF unless you explicitly pass --enable-ads
together with a positive --monthly-budget, and autopilot defaults to SUGGEST
(the bot prepares drafts for you to approve) rather than FULL auto-publish.

Usage:

    SA_PAGE_TOKEN=EAA... SA_APP_SECRET=abc \
    SA_FB_PAGE_ID=100064057084274 \
    docker compose exec -T \
      -e SA_PAGE_TOKEN -e SA_APP_SECRET -e SA_FB_PAGE_ID \
      web python manage.py social_ads_configure --schema fixit_02e0 \
        --lifetime --business-name "FixIt" --website https://fixitauto.parts \
        --autopilot suggest
"""
from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

ENV_MAP = {
    "SA_PAGE_TOKEN": ("page_access_token", True),
    "SA_APP_SECRET": ("app_secret", True),
    "SA_FB_PAGE_ID": ("facebook_page_id", False),
    "SA_IG_ID": ("instagram_account_id", False),
    "SA_AD_ACCOUNT_ID": ("ad_account_id", False),
    "SA_LLM_PROVIDER": ("llm_provider", False),
    "SA_LLM_KEY": ("llm_api_key", True),
    "SA_LLM_MODEL": ("llm_model", False),
}


def _money(val) -> Decimal:
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError):
        return Decimal("0.00")


class Command(BaseCommand):
    help = "Configure the Social Studio (social_ads) add-on for a tenant."

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True)
        parser.add_argument("--business-name", default="")
        parser.add_argument("--industry", default="")
        parser.add_argument("--products", default="")
        parser.add_argument("--audience", default="")
        parser.add_argument("--website", default="")
        parser.add_argument("--phone", default="")
        parser.add_argument("--autopilot", choices=["off", "suggest", "full"], default="suggest")
        parser.add_argument("--posts-per-week", type=int, default=0)
        parser.add_argument("--monthly-budget", default="")
        parser.add_argument("--daily-budget", default="")
        parser.add_argument(
            "--enable-ads", action="store_true",
            help="Turn ON paid campaigns (requires a positive --monthly-budget and an ad account).",
        )
        parser.add_argument("--lifetime", action="store_true")
        parser.add_argument("--months", type=int, default=0)

    def handle(self, *args, **o):
        from clients.models import Client
        from social_ads.models import SocialAdsConfig

        schema = o["schema"]
        client = Client.objects.filter(schema_name=schema).first()
        if not client:
            raise CommandError(f"مفيش تينانت بالـ schema '{schema}'")

        cfg, created = SocialAdsConfig.objects.get_or_create(tenant=client)

        written = []
        for env_var, (attr, is_secret) in ENV_MAP.items():
            val = os.environ.get(env_var)
            if val:
                setattr(cfg, attr, val)
                written.append(attr if not is_secret else f"{attr} (encrypted)")

        # Brand profile
        if o["business_name"]:
            cfg.business_display_name = o["business_name"]
        elif not cfg.business_display_name:
            cfg.business_display_name = "FixIt"
        if o["industry"]:
            cfg.industry = o["industry"]
        if o["products"]:
            cfg.products_services = o["products"]
        if o["audience"]:
            cfg.target_audience = o["audience"]
        if o["website"]:
            cfg.website_url = o["website"]
        if o["phone"]:
            cfg.contact_phone = o["phone"]

        # Autopilot + cadence
        cfg.autopilot_mode = o["autopilot"]
        if o["posts_per_week"]:
            cfg.posts_per_week = max(1, min(21, o["posts_per_week"]))

        # Budgets
        if o["monthly_budget"]:
            cfg.monthly_ad_budget = _money(o["monthly_budget"])
        if o["daily_budget"]:
            cfg.max_daily_ad_budget = _money(o["daily_budget"])

        # 🛡️ Paid ads only when explicitly enabled AND a budget + ad account exist.
        if o["enable_ads"]:
            if cfg.monthly_ad_budget <= 0:
                raise CommandError("لتفعيل الإعلانات المدفوعة لازم --monthly-budget أكبر من صفر.")
            if not cfg.ad_account_id:
                raise CommandError("لتفعيل الإعلانات لازم SA_AD_ACCOUNT_ID (act_...).")
            cfg.ads_enabled = True
        else:
            cfg.ads_enabled = False  # آمن افتراضياً — مفيش صرف تلقائي

        cfg.save()

        if o["lifetime"]:
            cfg.grant_subscription(None)
        elif o["months"] > 0:
            cfg.grant_subscription(timedelta(days=30 * o["months"]))

        w = self.stdout.write
        s = self.style
        w(s.SUCCESS(f"\n✅ استوديو التسويق {'اتعمل' if created else 'اتحدّث'} لتينانت: {schema}"))
        w(f"   الاسم التجاري   : {cfg.business_display_name}")
        w(f"   الحقول المكتوبة : {', '.join(written) or '(مفيش)'}")
        w(f"   Facebook Page   : {cfg.facebook_page_id or '-'}")
        w(f"   Instagram       : {cfg.instagram_account_id or '-'}")
        w(f"   Ad Account      : {cfg.ad_account_id or '-'}")
        w(f"   وضع الطيار الآلي : {cfg.get_autopilot_mode_display()}")
        w(f"   بوستات/أسبوع    : {cfg.posts_per_week}")
        w(f"   إعلانات مدفوعة  : {'مفعّلة ✅' if cfg.ads_enabled else 'موقوفة (آمن) ⏸️'}")
        w(f"   سقف شهري/يومي   : {cfg.monthly_ad_budget} / {cfg.max_daily_ad_budget}")
        w(f"   الاشتراك        : {cfg.subscription_state}")
        w(f"   جاهز للنشر؟     : {'أيوة ✅' if cfg.is_operational else 'لأ ❌ (راجع التوكن/الصفحة/الاشتراك)'}")
        w(f"   جاهز للإعلانات؟ : {'أيوة ✅' if cfg.can_run_ads() else 'لأ (محتاج ad account + تفعيل)'}")
        w("")
