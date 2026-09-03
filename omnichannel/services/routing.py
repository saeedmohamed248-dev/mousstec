"""
Webhook payload parsing + tenant routing.

Meta sends two very different envelope shapes to the same URL:

  • WhatsApp Business Cloud API  →  {"object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": ...},
                                          "messages": [...], "contacts": [...]}}]}]}

  • Messenger (Facebook Page)    →  {"object": "page",
        "entry": [{"id": <page_id>, "messaging": [{"sender": {"id": ...},
                                                   "message": {"text": ...}}]}]}

`extract_inbound_messages` normalises both into a flat list of InboundMessage
records. `resolve_config` maps a normalised message back to the owning
TenantChannelConfig using the receiving phone_number_id / page_id (both indexed
columns in the public schema).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# Envelope object types
WHATSAPP_OBJECT = "whatsapp_business_account"
MESSENGER_OBJECT = "page"

CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_MESSENGER = "messenger"


@dataclass
class InboundMessage:
    channel: str          # "whatsapp" | "messenger"
    route_key: str        # receiving phone_number_id (WA) or page_id (Messenger)
    sender_id: str        # customer's WA number / PSID
    text: str
    message_id: str = ""
    sender_name: str = ""


def extract_inbound_messages(payload: dict) -> list[InboundMessage]:
    """Return every user-authored text message in a webhook payload.

    Non-text events (delivery/read receipts, echoes, status updates, reactions,
    stickers, etc.) are ignored — we only auto-reply to real text questions.
    Malformed fragments are skipped defensively rather than raising.
    """
    if not isinstance(payload, dict):
        return []

    obj = payload.get("object")
    if obj == WHATSAPP_OBJECT:
        return _extract_whatsapp(payload)
    if obj == MESSENGER_OBJECT:
        return _extract_messenger(payload)
    return []


def _extract_whatsapp(payload: dict) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in (entry or {}).get("changes", []) or []:
            value = (change or {}).get("value") or {}
            metadata = value.get("metadata") or {}
            route_key = metadata.get("phone_number_id") or ""
            if not route_key:
                continue

            # Build a psid → name map from contacts for nicer prompts.
            names: dict[str, str] = {}
            for contact in value.get("contacts", []) or []:
                wa_id = (contact or {}).get("wa_id")
                profile = (contact or {}).get("profile") or {}
                if wa_id:
                    names[wa_id] = profile.get("name", "") or ""

            for msg in value.get("messages", []) or []:
                if (msg or {}).get("type") != "text":
                    continue
                sender = msg.get("from") or ""
                text = ((msg.get("text") or {}).get("body") or "").strip()
                if not sender or not text:
                    continue
                out.append(
                    InboundMessage(
                        channel=CHANNEL_WHATSAPP,
                        route_key=route_key,
                        sender_id=sender,
                        text=text,
                        message_id=msg.get("id", "") or "",
                        sender_name=names.get(sender, ""),
                    )
                )
    return out


def _extract_messenger(payload: dict) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []) or []:
        route_key = (entry or {}).get("id") or ""
        if not route_key:
            continue
        for event in entry.get("messaging", []) or []:
            message = (event or {}).get("message") or {}
            # Skip echoes (messages the page itself sent) and non-text payloads.
            if message.get("is_echo"):
                continue
            sender = (event.get("sender") or {}).get("id") or ""
            text = (message.get("text") or "").strip()
            if not sender or not text:
                continue
            out.append(
                InboundMessage(
                    channel=CHANNEL_MESSENGER,
                    route_key=route_key,
                    sender_id=sender,
                    text=text,
                    message_id=message.get("mid", "") or "",
                )
            )
    return out


@dataclass
class RouteTarget:
    config: object            # TenantChannelConfig
    access_token: str         # token to reply with (number-specific or account primary)
    app_secret: str           # for signature verification
    phone_number_id: str      # WhatsApp sending id (empty for Messenger)
    page_id: str              # Messenger page id (empty for WhatsApp)


def resolve_target(message: "InboundMessage"):
    """Resolve an inbound message to its owning config + the exact credentials to
    reply with — supporting the primary number AND additional numbers.

    Returns a RouteTarget or None.
    """
    from omnichannel.models import TenantChannelConfig, TenantChannelNumber

    rk = message.route_key
    if message.channel == CHANNEL_WHATSAPP:
        cfg = (TenantChannelConfig.objects
               .filter(whatsapp_phone_number_id=rk, whatsapp_enabled=True)
               .select_related("tenant").first())
        if cfg:
            return RouteTarget(cfg, cfg.meta_access_token, cfg.app_secret, rk, "")
        num = (TenantChannelNumber.objects
               .filter(whatsapp_phone_number_id=rk, is_active=True)
               .select_related("config", "config__tenant").first())
        if num:
            cfg = num.config
            return RouteTarget(
                cfg, num.meta_access_token or cfg.meta_access_token,
                num.app_secret or cfg.app_secret, rk, "")
        return None

    if message.channel == CHANNEL_MESSENGER:
        cfg = (TenantChannelConfig.objects
               .filter(facebook_page_id=rk, messenger_enabled=True)
               .select_related("tenant").first())
        if cfg:
            return RouteTarget(cfg, cfg.meta_access_token, cfg.app_secret, "", rk)
        num = (TenantChannelNumber.objects
               .filter(facebook_page_id=rk, is_active=True)
               .select_related("config", "config__tenant").first())
        if num:
            cfg = num.config
            return RouteTarget(
                cfg, num.meta_access_token or cfg.meta_access_token,
                num.app_secret or cfg.app_secret, "", rk)
        return None

    return None


def resolve_config(message: "InboundMessage"):
    """Look up the TenantChannelConfig that owns the receiving number/page.

    Returns the config instance or None. Kept import-local so this module stays
    importable without the Django app registry (unit-testable in isolation).
    """
    from omnichannel.models import TenantChannelConfig

    if message.channel == CHANNEL_WHATSAPP:
        qs = TenantChannelConfig.objects.filter(
            whatsapp_phone_number_id=message.route_key,
            whatsapp_enabled=True,
        )
    elif message.channel == CHANNEL_MESSENGER:
        qs = TenantChannelConfig.objects.filter(
            facebook_page_id=message.route_key,
            messenger_enabled=True,
        )
    else:
        return None

    return qs.select_related("tenant").first()
