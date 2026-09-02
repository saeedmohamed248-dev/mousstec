"""
Central Omnichannel webhook (Deliverable 2).

A single public endpoint receives EVERY subscribed tenant's WhatsApp + Messenger
traffic (Meta calls the platform domain, not tenant subdomains). We:

  GET  → answer Meta's subscription handshake (hub.challenge) when hub.verify_token
         matches the platform token *or* any tenant's configured verify token.
  POST → parse the payload, route each message to its owning tenant, verify the
         per-tenant HMAC signature, and hand off to Celery. We always ack Meta
         with 200 within a few milliseconds so we never hit its ~20s timeout.
"""
from __future__ import annotations

import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .services import meta_api
from .services.routing import extract_inbound_messages, resolve_config

logger = logging.getLogger("mouss_tec_core")


@method_decorator(csrf_exempt, name="dispatch")
class OmnichannelWebhookView(View):
    http_method_names = ["get", "post"]

    # ── GET: Meta verification handshake ──────────────────────────────
    def get(self, request, *args, **kwargs):
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token") or ""
        challenge = request.GET.get("hub.challenge", "")

        if mode == "subscribe" and token and self._verify_token_valid(token):
            logger.info("omnichannel: webhook verified by Meta")
            return HttpResponse(challenge, content_type="text/plain")

        logger.warning("omnichannel: webhook verification rejected (mode=%s)", mode)
        return HttpResponse("Verification failed", status=403)

    def _verify_token_valid(self, token: str) -> bool:
        platform = getattr(settings, "OMNICHANNEL_VERIFY_TOKEN", "")
        if platform and hmac.compare_digest(token, platform):
            return True
        # Fall back to a per-tenant token match.
        from .models import TenantChannelConfig
        return TenantChannelConfig.objects.filter(webhook_verify_token=token).exists()

    # ── POST: inbound messages ────────────────────────────────────────
    def post(self, request, *args, **kwargs):
        raw_body = request.body
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return HttpResponseBadRequest("Invalid JSON")

        signature = request.headers.get("X-Hub-Signature-256", "")

        for message in extract_inbound_messages(payload):
            try:
                self._route_and_dispatch(message, raw_body, signature)
            except Exception:  # one bad message must not drop the whole batch
                logger.exception("omnichannel: failed to dispatch a message")

        # Ack immediately — real work happens in Celery.
        return JsonResponse({"status": "ok"})

    def _route_and_dispatch(self, message, raw_body: bytes, signature: str) -> None:
        config = resolve_config(message)
        if config is None:
            logger.info(
                "omnichannel: no tenant for %s route_key=%s (unconfigured/disabled)",
                message.channel, message.route_key,
            )
            return

        # Per-tenant signature verification. If the tenant has set an app secret
        # we REQUIRE a valid signature; if not, we allow (dev) but warn.
        app_secret = config.app_secret
        if app_secret:
            if not meta_api.verify_signature(app_secret, raw_body, signature):
                logger.warning(
                    "omnichannel: bad signature for tenant=%s — dropping message",
                    config.tenant.schema_name,
                )
                return
        else:
            logger.warning(
                "omnichannel: tenant=%s has no app secret — skipping signature check",
                config.tenant.schema_name,
            )

        if not config.is_operational:
            logger.info(
                "omnichannel: tenant=%s not operational — ignoring inbound",
                config.tenant.schema_name,
            )
            return

        from .tasks import process_inbound_message
        process_inbound_message.delay(
            config_id=config.pk,
            channel=message.channel,
            sender_id=message.sender_id,
            text=message.text,
            message_id=message.message_id,
            sender_name=message.sender_name,
        )
