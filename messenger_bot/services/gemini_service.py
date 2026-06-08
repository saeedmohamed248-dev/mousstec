"""
Gemini generation wrapper for the Messenger bot. Mirrors the REST-call
style already used elsewhere in this codebase (see erp_core/ai/advisor_agent.py)
so we don't introduce a new SDK dependency.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings

from .vector_store import get_store

logger = logging.getLogger(__name__)

_GENERATE_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

_SYSTEM_PROMPT = """You are the official AI support assistant for Mousstec — a SaaS ERP platform
serving auto repair shops and print shops. Reply in the same language the user wrote in
(Arabic or English). Be professional, concise, and friendly.

Rules:
- Ground every factual claim in the CONTEXT below. If the context doesn't cover the
  question, say so honestly and offer to route the user to a human agent.
- Never invent pricing, features, or release dates.
- Keep replies under ~120 words unless the user explicitly asks for more detail.
- Format prices and plan names exactly as they appear in the context.
"""


def generate_support_reply(user_message: str) -> str:
    """Run RAG: pull context from the local vector store, then ask Gemini."""
    store = get_store()
    try:
        context = store.context_for(user_message, top_k=4)
    except Exception as exc:
        logger.exception("messenger_bot: vector lookup failed: %s", exc)
        context = ""

    return _call_gemini(user_message, context)


def _call_gemini(user_message: str, context: str) -> str:
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        logger.error("messenger_bot: GEMINI_API_KEY missing")
        return _fallback_reply()

    model = getattr(settings, "MESSENGER_GEMINI_MODEL", None) or getattr(
        settings, "GEMINI_REFINER_MODEL", "gemini-2.0-flash"
    )
    url = _GENERATE_URL_TEMPLATE.format(model=model)

    context_block = context if context else "(no matching knowledge-base entries)"
    prompt = (
        f"{_SYSTEM_PROMPT}\n\n"
        f"CONTEXT:\n{context_block}\n\n"
        f"USER MESSAGE:\n{user_message}\n\n"
        f"ASSISTANT REPLY:"
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 512,
            "topP": 0.9,
        },
    }

    try:
        resp = requests.post(
            url,
            params={"key": api_key},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        text = _extract_text(data)
        if text:
            return text.strip()
        logger.warning("messenger_bot: empty Gemini response: %s", data)
    except requests.RequestException as exc:
        logger.exception("messenger_bot: Gemini call failed: %s", exc)
    return _fallback_reply()


def _extract_text(data: dict) -> Optional[str]:
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


def _fallback_reply() -> str:
    return (
        "Sorry — I'm having trouble reaching our knowledge base right now. "
        "A Mousstec support agent will follow up with you shortly."
    )
