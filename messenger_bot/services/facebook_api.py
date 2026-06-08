"""
Outbound Facebook Graph API client — Send API for Messenger.

Includes:
- Bounded exponential-backoff retry on transient errors (5xx, 429, network).
- Hard timeout so a stuck FB endpoint can't park a worker.
- Structured logging — every send is observable in production logs.
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_GRAPH_URL = "https://graph.facebook.com/v19.0/me/messages"
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.7  # seconds


class FacebookSendError(Exception):
    pass


def send_text_message(recipient_id: str, text: str) -> dict:
    """Send a plain-text message back to a Messenger user. Returns the FB JSON response."""
    token = getattr(settings, "FB_PAGE_ACCESS_TOKEN", "")
    if not token:
        raise FacebookSendError("FB_PAGE_ACCESS_TOKEN is not configured")

    # Messenger hard-caps message body at 2000 chars — truncate defensively.
    body = (text or "").strip()
    if not body:
        raise FacebookSendError("Refusing to send empty message")
    if len(body) > 1900:
        body = body[:1897] + "..."

    payload = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": body},
    }

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                _GRAPH_URL,
                params={"access_token": token},
                json=payload,
                timeout=10,
            )
            if resp.status_code < 400:
                logger.info(
                    "messenger_bot: sent to %s (attempt=%d, status=%d)",
                    recipient_id, attempt, resp.status_code,
                )
                return resp.json()

            # 4xx (other than 429) is a permanent client error — don't retry.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                logger.error(
                    "messenger_bot: FB rejected send to %s (status=%d): %s",
                    recipient_id, resp.status_code, resp.text[:500],
                )
                raise FacebookSendError(
                    f"FB Graph API returned {resp.status_code}: {resp.text[:200]}"
                )

            logger.warning(
                "messenger_bot: transient FB error %d (attempt=%d): %s",
                resp.status_code, attempt, resp.text[:200],
            )
            last_exc = FacebookSendError(f"status={resp.status_code}")
        except requests.RequestException as exc:
            logger.warning(
                "messenger_bot: network error sending to %s (attempt=%d): %s",
                recipient_id, attempt, exc,
            )
            last_exc = exc

        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))

    raise FacebookSendError(f"Failed after {_MAX_ATTEMPTS} attempts: {last_exc}")
