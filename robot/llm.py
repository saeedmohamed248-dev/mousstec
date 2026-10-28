"""
robot/llm.py — the robot's thinking when Gemini doesn't answer.

Gemini understands what's said (robot/understand.py) and answers it (the repair
coach, a discussion: services.ai_reply). When it's busy («Gemini ✗ timeout»),
every sentence waited on it twice — 3–5 s for nothing each time — and the robot
said «مفهمتش قصدك» in the end (from the shop's log, brain-0.9.12).

  • `gemini_down()`: Gemini didn't answer a moment ago — not asked again for
    DOWN_S seconds; it's tried again after.
  • `backup()`: another model, asked directly with a voice's short timeout
    (never the ERP gateway's 45 s): OpenAI first when the shop has a key
    (OPENAI_API_KEY — ROBOT_OPENAI_MODEL, gpt-4o-mini by default: cheap, quick,
    good Egyptian Arabic), then Together AI, the ERP's own text AI
    (TOGETHER_API_KEY — ROBOT_BACKUP_MODEL, TOGETHER_LLM_MODEL by default).

The keys live in the server's .env only. Neither key: the rules alone, as
before, and said honestly.
"""

from __future__ import annotations

import logging
import os
import re

from django.core.cache import cache

logger = logging.getLogger("mouss_tec_core")

DOWN_S = 60                        # Gemini not asked again this long after it didn't answer
SLOW_S = 2.5                       # …in this long: out of time, not a quick «no» (a key, a bad request)
BACKUP_TIMEOUT_S = 6.0             # the customer stands waiting
_URL = "https://api.together.xyz/v1/chat/completions"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_DOWN_KEY = "robot:gemini:down"


def gemini_down() -> bool:
    try:
        return bool(cache.get(_DOWN_KEY))
    except Exception:                       # the cache down: Gemini's tried, as before
        return False


def note_gemini(answered: bool, took: float) -> None:
    """Gemini answered: asked again at once. It ran out of time (`took` seconds): not asked again
    for DOWN_S seconds — a quick «no» isn't an outage."""
    try:
        if answered:
            cache.delete(_DOWN_KEY)
        elif took >= SLOW_S:
            cache.set(_DOWN_KEY, 1, DOWN_S)
    except Exception:
        pass


def _key() -> str:
    from django.conf import settings

    return str(getattr(settings, "TOGETHER_API_KEY", "") or "").strip()


def _openai_key() -> str:
    from django.conf import settings

    return str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()


def _providers() -> list:
    """(url, key, model) of each backup the shop has, in the order they're asked."""
    from django.conf import settings

    found = []
    if _openai_key():
        found.append((_OPENAI_URL, _openai_key(), os.getenv("ROBOT_OPENAI_MODEL", "").strip() or "gpt-4o-mini"))
    if _key():
        found.append((_URL, _key(), os.getenv("ROBOT_BACKUP_MODEL", "").strip()
                      or str(getattr(settings, "TOGETHER_LLM_MODEL", "") or "meta-llama/Llama-3.3-70B-Instruct-Turbo")))
    return found


def has_backup() -> bool:
    return bool(_providers())


def backup(system: str, text: str, *, history=(), json_out: bool = False, timeout: float = BACKUP_TIMEOUT_S,
           max_tokens: int = 500) -> str:
    """The backup model's answer to `text` (`system` its instructions, `history` [said, replied]
    pairs), or '' — no key, out of time, an error."""
    import time

    if not (text or "").strip():
        return ""
    messages = [{"role": "system", "content": system}]
    for said, replied in list(history or [])[-4:]:
        messages += [{"role": "user", "content": str(said)[:200]}, {"role": "assistant", "content": str(replied)[:300]}]
    messages.append({"role": "user", "content": text[:4000]})
    started = time.monotonic()
    for url, key, model in _providers():
        left = timeout - (time.monotonic() - started)
        if left < 1:
            break                           # the customer's waited enough
        said = _ask(url, key, model, messages, json_out=json_out, timeout=left, max_tokens=max_tokens)
        if said:
            return re.sub(r"^```(?:json)?\s*|\s*```$", "", said.strip()) if json_out else said.strip()
    return ""


def _ask(url: str, key: str, model: str, messages: list, *, json_out: bool, timeout: float, max_tokens: int) -> str:
    """One OpenAI-style chat model's answer, or '' (unreachable, an error, out of time)."""
    import requests

    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
    if json_out:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("robot backup model %s: unreachable: %s", model, type(exc).__name__)
        return ""
    if resp.status_code != 200:                 # (the key never logged: only the code and the reason)
        logger.warning("robot backup model %s answered %s: %s", model, resp.status_code, resp.text[:200])
        return ""
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
