"""
End-to-end self-test for the Omnichannel AI Automation add-on.

Exercises the whole pipeline for one tenant without needing a real customer
message: activate subscription → verify routing → read the tenant catalogue →
generate an AI reply → (optionally) send it through Meta.

Usage (inside the web container):

    python manage.py omnichannel_selftest --tenant <schema_or_id>
    python manage.py omnichannel_selftest --tenant fixit --message "عايز سعر فلتر زيت"
    python manage.py omnichannel_selftest --tenant fixit --send   # actually call Meta

By default it runs in DRY-RUN for the final Meta send (prints what it would send)
so you can validate everything before wiring real WhatsApp credentials.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

from clients.models import Client
from omnichannel.models import ChannelMessageLog, TenantChannelConfig
from omnichannel.services import meta_api
from omnichannel.services.inventory_context import build_catalog_context
from omnichannel.services.llm import generate_reply
from omnichannel.services.routing import (
    CHANNEL_MESSENGER,
    CHANNEL_WHATSAPP,
    InboundMessage,
    resolve_config,
)


def _line(msg=""):
    return msg


class Command(BaseCommand):
    help = "Run an end-to-end self-test of the Omnichannel add-on for one tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True,
                            help="Tenant schema_name or numeric id.")
        parser.add_argument("--message", default="السلام عليكم، عايز أعرف الأسعار المتوفرة عندكم",
                            help="Simulated customer message.")
        parser.add_argument("--channel", default="whatsapp",
                            choices=["whatsapp", "messenger"])
        parser.add_argument("--sender", default="201000000000",
                            help="Simulated customer id (WA number / PSID).")
        parser.add_argument("--send", action="store_true",
                            help="Actually send the reply via Meta (needs a real token).")
        parser.add_argument("--no-activate", action="store_true",
                            help="Do not auto-activate the subscription.")

    # ──────────────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        tenant = self._resolve_tenant(opts["tenant"])
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Omnichannel self-test → {tenant.name} ({tenant.schema_name}) ===\n"))

        config, created = TenantChannelConfig.objects.get_or_create(tenant=tenant)
        self.stdout.write(f"1) Config: {'created new' if created else 'found existing'}")

        # 2) Subscription
        if not opts["no_activate"] and not config.subscription_is_valid:
            config.grant_subscription(timedelta(days=30))
            self.stdout.write(self.style.SUCCESS("2) Subscription: ACTIVATED for 30 days ✅"))
        else:
            self.stdout.write(f"2) Subscription: state={config.subscription_state} "
                              f"(valid={config.subscription_is_valid})")

        channel = opts["channel"]

        # 3) Ensure a route key so routing can resolve this tenant.
        if channel == CHANNEL_WHATSAPP:
            if not config.whatsapp_phone_number_id:
                config.whatsapp_phone_number_id = f"TEST_PN_{tenant.id}"
                config.whatsapp_enabled = True
                config.save(update_fields=["whatsapp_phone_number_id", "whatsapp_enabled"])
                self.stdout.write("   (set a TEST phone_number_id for routing)")
            route_key = config.whatsapp_phone_number_id
        else:
            if not config.facebook_page_id:
                config.facebook_page_id = f"TEST_PAGE_{tenant.id}"
                config.messenger_enabled = True
                config.save(update_fields=["facebook_page_id", "messenger_enabled"])
                self.stdout.write("   (set a TEST page_id for routing)")
            route_key = config.facebook_page_id

        # 4) Routing
        msg = InboundMessage(
            channel=channel, route_key=route_key,
            sender_id=opts["sender"], text=opts["message"], message_id="selftest-1",
        )
        resolved = resolve_config(msg)
        if resolved and resolved.pk == config.pk:
            self.stdout.write(self.style.SUCCESS(
                f"3) Routing: resolved '{route_key}' → {tenant.schema_name} ✅"))
        else:
            self.stdout.write(self.style.ERROR(
                f"3) Routing: FAILED to resolve '{route_key}' — check enabled flags"))
            return

        # 5) Operational gate
        self.stdout.write(f"4) Operational: {config.is_operational} "
                          f"(ai_enabled={config.ai_enabled}, token_set={bool(config.meta_access_token)})")

        # 6) Catalogue snapshot (inside tenant schema)
        currency = ""
        try:
            currency = tenant.effective_currency
        except Exception:
            pass
        with schema_context(tenant.schema_name):
            catalog = build_catalog_context(opts["message"], currency=currency)
        preview = (catalog[:300] + "…") if len(catalog) > 300 else (catalog or "(فارغ)")
        self.stdout.write(f"5) Catalogue: {len(catalog)} chars\n----\n{preview}\n----")

        # 7) LLM reply
        self.stdout.write("6) Generating AI reply…")
        reply = generate_reply(config, opts["message"], catalog)
        if reply:
            self.stdout.write(self.style.SUCCESS("   AI reply:\n" + reply))
        else:
            reply = config.fallback_message
            self.stdout.write(self.style.WARNING(
                "   LLM returned nothing (check GEMINI_API_KEY / BYO key) — using fallback:\n" + reply))

        # 8) Meta send (dry-run unless --send)
        token = config.meta_access_token
        if opts["send"]:
            if not token:
                self.stdout.write(self.style.ERROR(
                    "7) Send: SKIPPED — no Meta access token configured for this tenant."))
            else:
                try:
                    if channel == CHANNEL_WHATSAPP:
                        meta_api.send_whatsapp_text(
                            access_token=token,
                            phone_number_id=config.whatsapp_phone_number_id,
                            recipient_id=opts["sender"], text=reply)
                    else:
                        meta_api.send_messenger_text(
                            access_token=token, recipient_id=opts["sender"], text=reply)
                    self.stdout.write(self.style.SUCCESS("7) Send: delivered via Meta ✅"))
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f"7) Send: FAILED — {exc}"))
        else:
            self.stdout.write("7) Send: DRY-RUN (pass --send with a real token to deliver)")

        # 9) Log
        ChannelMessageLog.objects.create(
            tenant=tenant, channel=channel, sender_id=opts["sender"],
            inbound_text=opts["message"], outbound_text=reply,
            status=ChannelMessageLog.Status.REPLIED, meta_message_id="selftest",
        )
        self.stdout.write(self.style.SUCCESS(
            "\n=== DONE — pipeline exercised end-to-end. Check /omnichannel/settings/ for the logged conversation. ===\n"))

    # ──────────────────────────────────────────────────────────────────
    def _resolve_tenant(self, ref: str) -> Client:
        qs = Client.objects.exclude(schema_name="public")
        if ref.isdigit():
            t = qs.filter(pk=int(ref)).first()
        else:
            t = qs.filter(schema_name=ref).first()
        if not t:
            available = ", ".join(qs.values_list("schema_name", flat=True)[:20])
            raise CommandError(f"Tenant '{ref}' not found. Available: {available}")
        return t
