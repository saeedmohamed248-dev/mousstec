"""
Outbound Meta Graph API client — BYOK edition.

Unlike messenger_bot (which uses one platform-wide page token from settings),
here every call is authenticated with the *tenant's own* access token, so all
WhatsApp conversation charges are billed to the tenant by Meta directly.

Supports both:
  • WhatsApp Cloud API   → POST /{phone_number_id}/messages
  • Messenger Send API   → POST /me/messages?access_token=...

Design notes:
  - Bounded exponential-backoff retry on transient errors (5xx, 429, network).
  - Hard per-request timeout so a stuck Meta endpoint can't park a Celery worker.
  - Signature verification helper (verify_signature) for inbound webhooks.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger("mouss_tec_core")

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.7  # seconds
_TIMEOUT = 10        # seconds


class MetaSendError(Exception):
    pass


def _graph_base() -> str:
    version = getattr(settings, "META_GRAPH_VERSION", "v19.0")
    return f"https://graph.facebook.com/{version}"


# ── Page access token resolution ──────────────────────────────────────
# In-process cache: {(page_id, base_token_fingerprint): page_token}. Page tokens
# derived from a System User token never expire, so a process-lifetime cache is
# safe and saves a Graph round-trip on every Messenger/IG send + comment reply.
_page_token_cache: dict[tuple[str, str], str] = {}


def resolve_page_token(base_token: str, page_id: str) -> str:
    """Return a Page access token for `page_id`, deriving it from `base_token`.

    Facebook's "new Pages experience" requires a *Page* access token for the
    Send API (/me/messages) and for public comment replies (/{comment_id}/comments).
    The token we store per tenant is often a User or System-User token (e.g. one
    generated from Business Manager → System Users), which can *read* the page but
    is rejected on those write calls with:

        "A Page access token is required for this call for the new Pages experience."

    Calling GET /{page_id}?fields=access_token with the base token returns the
    page-scoped token. If the base token is already a page token this still works
    (it returns the same page token), so the call is safe regardless of token type.

    Never raises: on any failure we fall back to the base token so behaviour is no
    worse than before this helper existed.
    """
    if not base_token or not page_id:
        return base_token
    key = (str(page_id), base_token[:16])
    cached = _page_token_cache.get(key)
    if cached:
        return cached
    try:
        resp = requests.get(
            f"{_graph_base()}/{page_id}",
            params={"fields": "access_token", "access_token": base_token},
            timeout=_TIMEOUT,
        )
        if resp.status_code < 400:
            page_token = (resp.json() or {}).get("access_token") or ""
            if page_token:
                _page_token_cache[key] = page_token
                return page_token
        else:
            logger.warning(
                "omnichannel: could not derive page token for %s (status=%d): %s",
                page_id, resp.status_code, resp.text[:200],
            )
    except (requests.RequestException, ValueError) as exc:
        logger.warning("omnichannel: page token derivation failed for %s: %s", page_id, exc)
    # Fall back to the base token — Messenger/comment call may still succeed if the
    # stored token happens to be a page token already.
    return base_token


# ── Inbound signature verification ────────────────────────────────────
def verify_signature(app_secret: str, raw_body: bytes, header_value: str) -> bool:
    """Constant-time check of Meta's X-Hub-Signature-256 header.

    Meta signs the raw request body with HMAC-SHA256 keyed on the app secret.
    Returns True only on an exact match. If no app_secret is configured we
    cannot verify — the caller decides whether to allow that (dev) or reject it.
    """
    if not app_secret or not header_value:
        return False
    if not header_value.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = header_value.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# ── Outbound sends ────────────────────────────────────────────────────
def send_whatsapp_text(*, access_token: str, phone_number_id: str,
                       recipient_id: str, text: str) -> dict:
    """Send a WhatsApp text message via the Cloud API."""
    if not access_token:
        raise MetaSendError("WhatsApp access token is not configured")
    if not phone_number_id:
        raise MetaSendError("WhatsApp phone_number_id is not configured")

    body = _clean_body(text, limit=4096)  # WhatsApp text body cap
    url = f"{_graph_base()}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    return _post_with_retry(url, payload, headers=headers, recipient=recipient_id)


def send_messenger_text(*, access_token: str, recipient_id: str, text: str) -> dict:
    """Send a Messenger text message via the Send API."""
    if not access_token:
        raise MetaSendError("Messenger access token is not configured")

    body = _clean_body(text, limit=2000)  # Messenger text body cap
    url = f"{_graph_base()}/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": body},
    }
    return _post_with_retry(
        url, payload, params={"access_token": access_token}, recipient=recipient_id
    )


def reply_to_comment(*, access_token: str, comment_id: str, message: str) -> dict:
    """Post a public reply to a Facebook/Instagram comment.

    Graph API: POST /{comment_id}/comments — authenticated with the tenant's page
    token (needs pages_manage_engagement for FB, instagram_manage_comments for IG).
    """
    if not access_token:
        raise MetaSendError("access token is not configured")
    if not comment_id:
        raise MetaSendError("comment_id is required")
    body = _clean_body(message, limit=8000)
    url = f"{_graph_base()}/{comment_id}/comments"
    return _post_with_retry(
        url, {"message": body}, params={"access_token": access_token}, recipient=comment_id
    )


# ── internals ─────────────────────────────────────────────────────────
def _clean_body(text: str, *, limit: int) -> str:
    body = (text or "").strip()
    if not body:
        raise MetaSendError("Refusing to send an empty message")
    if len(body) > limit:
        body = body[: limit - 3] + "..."
    return body


def _post_with_retry(url, payload, *, headers=None, params=None, recipient="") -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url, json=payload, headers=headers, params=params, timeout=_TIMEOUT
            )
            if resp.status_code < 400:
                logger.info(
                    "omnichannel: sent to %s (attempt=%d, status=%d)",
                    recipient, attempt, resp.status_code,
                )
                try:
                    return resp.json()
                except ValueError:
                    return {"status": "ok"}

            # 4xx other than 429 → permanent client error, don't retry.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                logger.error(
                    "omnichannel: Meta rejected send to %s (status=%d): %s",
                    recipient, resp.status_code, resp.text[:500],
                )
                raise MetaSendError(
                    f"Meta Graph API returned {resp.status_code}: {resp.text[:200]}"
                )

            logger.warning(
                "omnichannel: transient Meta error %d (attempt=%d): %s",
                resp.status_code, attempt, resp.text[:200],
            )
            last_exc = MetaSendError(f"status={resp.status_code}")
        except requests.RequestException as exc:
            logger.warning(
                "omnichannel: network error sending to %s (attempt=%d): %s",
                recipient, attempt, exc,
            )
            last_exc = exc

        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))

    raise MetaSendError(f"Failed after {_MAX_ATTEMPTS} attempts: {last_exc}")
