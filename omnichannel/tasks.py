"""
Celery task: process a single inbound customer message end-to-end.

Flow:
  1. Load the tenant's TenantChannelConfig (public schema).
  2. Gate on subscription + ai_enabled.
  3. Switch into the tenant schema and read the live priced catalogue.
  4. Ask the LLM (tenant BYO key, else platform Gemini) for a grounded reply.
  5. Send the reply back through the correct Meta channel using the tenant's token.
  6. Log everything to ChannelMessageLog.

The webhook view acks Meta in <1s and hands off here, so nothing in this task is
time-critical to the HTTP response. All failures are contained: a bad tenant
config or a Meta outage logs an error and (best-effort) sends the fallback
message, but never crashes the worker.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django_tenants.utils import schema_context

from .services import meta_api
from .services.inventory_context import build_catalog_context
from .services.llm import generate_reply
from .services.routing import CHANNEL_INSTAGRAM, CHANNEL_MESSENGER, CHANNEL_WHATSAPP

logger = logging.getLogger("mouss_tec_core")


@shared_task(
    name="omnichannel.process_inbound_message",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def process_inbound_message(self, config_id: int, channel: str, sender_id: str,
                            text: str, message_id: str = "", sender_name: str = "",
                            access_token: str = "", phone_number_id: str = ""):
    from .models import ChannelMessageLog, TenantChannelConfig

    try:
        config = TenantChannelConfig.objects.select_related("tenant").get(pk=config_id)
    except TenantChannelConfig.DoesNotExist:
        logger.warning("omnichannel: config %s vanished before processing", config_id)
        return

    tenant = config.tenant
    # Per-number credentials (multi-number). Fall back to the account primary so
    # messages queued before this change still work.
    send_token = access_token or config.meta_access_token
    send_phone_id = phone_number_id or config.whatsapp_phone_number_id

    def _log(status, outbound="", error=""):
        try:
            ChannelMessageLog.objects.create(
                tenant=tenant, channel=channel, sender_id=sender_id,
                contact_name=sender_name or "",
                inbound_text=text, outbound_text=outbound, status=status,
                error=error, meta_message_id=message_id,
            )
        except Exception:
            logger.exception("omnichannel: failed to write ChannelMessageLog")

    # ── Subscription / enablement gate ────────────────────────────────
    if not (config.subscription_is_valid and config.ai_enabled and send_token):
        logger.info("omnichannel: skipping — config %s not operational", config_id)
        _log(ChannelMessageLog.Status.SKIPPED, error="subscription/AI disabled")
        return

    # ── Read tenant catalogue inside the tenant schema ────────────────
    currency = ""
    catalog_context = ""
    try:
        currency = tenant.effective_currency
    except Exception:
        currency = ""
    try:
        with schema_context(tenant.schema_name):
            catalog_context = build_catalog_context(text, currency=currency)
    except Exception as exc:
        logger.warning("omnichannel: catalogue read failed for %s: %s", tenant.schema_name, exc)
        catalog_context = ""

    # ── Generate reply ────────────────────────────────────────────────
    reply = generate_reply(config, text, catalog_context)
    used_fallback = False
    if not reply:
        reply = config.fallback_message
        used_fallback = True

    if config.max_reply_chars and len(reply) > config.max_reply_chars:
        reply = reply[: config.max_reply_chars].rstrip() + "…"

    # ── Deliver via the right channel (using this number's credentials) ─
    try:
        if channel == CHANNEL_WHATSAPP:
            meta_api.send_whatsapp_text(
                access_token=send_token,
                phone_number_id=send_phone_id,
                recipient_id=sender_id,
                text=reply,
            )
        elif channel in (CHANNEL_MESSENGER, CHANNEL_INSTAGRAM):
            # Instagram Direct uses the same Send API (page token → /me/messages).
            meta_api.send_messenger_text(
                access_token=send_token, recipient_id=sender_id, text=reply,
            )
        else:
            logger.error("omnichannel: unknown channel %r", channel)
            _log(ChannelMessageLog.Status.FAILED, outbound=reply, error=f"unknown channel {channel}")
            return
    except meta_api.MetaSendError as exc:
        logger.error("omnichannel: send failed for tenant=%s: %s", tenant.schema_name, exc)
        _log(ChannelMessageLog.Status.FAILED, outbound=reply, error=str(exc))
        # Transient Meta issues are worth one bounded retry.
        raise self.retry(exc=exc)
    except Exception as exc:  # never crash the worker
        logger.exception("omnichannel: unhandled send error: %s", exc)
        _log(ChannelMessageLog.Status.FAILED, outbound=reply, error=repr(exc))
        return

    _log(
        ChannelMessageLog.Status.REPLIED,
        outbound=reply,
        error="fallback_used" if used_fallback else "",
    )

    # ── Smart handoff notification ────────────────────────────────────
    # Alert the shop only when the AI couldn't answer confidently (fallback),
    # so the owner can take over — not on every message.
    if used_fallback and config.notify_on_handoff:
        _notify_handoff(config, tenant, channel, sender_id, text)


def _notify_handoff(config, tenant, channel, sender_id, customer_text):
    """Best-effort email alert when a customer needs a human. Never raises."""
    to_email = (config.notify_email or getattr(tenant, "email", "") or "").strip()
    if not to_email:
        return
    try:
        from django.core.mail import send_mail
        subject = f"[{tenant.name}] عميل يحتاج ردّاً بشرياً — {channel}"
        body = (
            f"وصلت رسالة لم يستطع المساعد الآلي الرد عليها بثقة:\n\n"
            f"العميل: {sender_id}\nالقناة: {channel}\n\nالرسالة:\n{customer_text}\n\n"
            f"افتح لوحة التحكم للرد يدوياً: /omnichannel/console/inbox/"
        )
        send_mail(subject, body, None, [to_email], fail_silently=True)
    except Exception:
        logger.warning("omnichannel: handoff notification failed (SMTP?)", exc_info=True)
