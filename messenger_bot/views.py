"""
Messenger webhook endpoint.

GET  → Meta subscription handshake (verify token echo).
POST → entry-point for inbound messages. We respond 200 to Meta immediately
       and process each message inside a thread so the webhook never blocks
       past Meta's 20-second timeout, no matter how slow Gemini is.
"""
from __future__ import annotations

import hmac
import json
import logging
import threading

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import ConversationLog
from .services.facebook_api import FacebookSendError, send_text_message
from .services.gemini_service import generate_support_reply

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class MessengerWebhookView(View):
    """Thread-safe Class-Based View at /api/webhooks/messenger/."""

    http_method_names = ["get", "post"]

    # -------- GET: Meta verification handshake --------
    def get(self, request, *args, **kwargs):
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        expected = getattr(settings, "MESSENGER_VERIFY_TOKEN", "")

        if mode == "subscribe" and token and expected and hmac.compare_digest(token, expected):
            logger.info("messenger_bot: webhook verified by Meta")
            return HttpResponse(challenge, content_type="text/plain")

        logger.warning(
            "messenger_bot: verification rejected (mode=%s, token_match=%s)",
            mode, bool(token and expected and hmac.compare_digest(token, expected)),
        )
        return HttpResponse("Verification failed", status=403)

    # -------- POST: inbound messages --------
    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return HttpResponseBadRequest("Invalid JSON")

        if payload.get("object") != "page":
            # Meta sends other object types (e.g. instagram) — ack but ignore.
            return JsonResponse({"status": "ignored"})

        for entry in payload.get("entry", []) or []:
            for event in entry.get("messaging", []) or []:
                sender = (event.get("sender") or {}).get("id")
                message = event.get("message") or {}
                text = message.get("text")
                # Skip echoes, delivery receipts, and non-text payloads.
                if not sender or not text or message.get("is_echo"):
                    continue
                self._dispatch(sender, text)

        # Ack Meta immediately — actual reply happens in a worker thread.
        return JsonResponse({"status": "ok"})

    # -------- worker --------
    def _dispatch(self, sender_id: str, user_text: str) -> None:
        thread = threading.Thread(
            target=_handle_message_safely,
            args=(sender_id, user_text),
            daemon=True,
            name=f"messenger-reply-{sender_id[:8]}",
        )
        thread.start()


def _handle_message_safely(sender_id: str, user_text: str) -> None:
    bot_reply = ""
    error_str = ""
    try:
        bot_reply = generate_support_reply(user_text)
        send_text_message(sender_id, bot_reply)
    except FacebookSendError as exc:
        error_str = f"FB send failed: {exc}"
        logger.error("messenger_bot: %s", error_str)
    except Exception as exc:  # belt-and-braces — never crash the worker thread
        error_str = f"Unhandled: {exc!r}"
        logger.exception("messenger_bot: unhandled error processing %s", sender_id)
    finally:
        try:
            ConversationLog.objects.create(
                sender_id=sender_id,
                user_message=user_text,
                bot_response=bot_reply,
                error=error_str,
            )
        except Exception:
            logger.exception("messenger_bot: failed to write ConversationLog")
