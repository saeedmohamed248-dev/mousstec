"""
robot/audio.py — speech-to-text (mic) and text-to-speech (amp) for the robot.

The INMP441 mic streams PCM/audio up; the MAX98357A amp plays synthesized
speech. Both are behind pluggable providers so the system runs without heavy
deps and upgrades by installing a library or setting an API key.

  * transcribe(audio_bytes, language) → text ("" if unavailable)
  * synthesize(text, language)        → audio bytes (mp3) or None

Providers (env `ROBOT_STT_PROVIDER` / `ROBOT_TTS_PROVIDER`):
  STT: "gemini" (the ERP Gemini key, over the Gemini REST API) | "none"
  TTS: "gtts" (offline-ish, needs internet) | "none"

If a provider isn't available the functions degrade gracefully: `/voice/` still
works when the caller posts a ready `transcript`, and `/speak/` returns 204.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

STT_PROVIDER = os.getenv("ROBOT_STT_PROVIDER", "auto").lower()
TTS_PROVIDER = os.getenv("ROBOT_TTS_PROVIDER", "auto").lower()


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------

def transcribe(audio_bytes: bytes, language: str = "ar") -> str:
    """Transcribe spoken audio to text. Returns '' when no provider is available."""
    if not audio_bytes:
        return ""
    provider = STT_PROVIDER
    if provider in ("auto", "gemini") and _gemini_key():
        text = _gemini_transcribe(audio_bytes, language)
        if text:
            return text
    return ""


def _gemini_key() -> str:
    try:
        from django.conf import settings
        return str(getattr(settings, "GEMINI_API_KEY", "") or "").strip()
    except Exception:
        return ""


_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_STT_TIMEOUT = 15          # the robot waits 30 s for /voice/ as a whole
_STT_PROMPT = (
    "Transcribe this audio verbatim. Reply with ONLY the transcription "
    "text, no quotes, no commentary. The speaker is in an Egyptian car-parts "
    "shop and usually speaks Egyptian Arabic, sometimes with English words."
)


def _stt_models() -> list[str]:
    """Models to try, in order. A retired model answers 404 and the next one
    is tried, so one model being withdrawn doesn't make the robot deaf: the
    old hard-coded "gemini-2.0-flash" did exactly that.
    """
    try:
        from django.conf import settings
        platform = str(getattr(settings, "OMNICHANNEL_GEMINI_MODEL", "") or "").strip()
    except Exception:
        platform = ""
    candidates = [os.getenv("ROBOT_STT_MODEL", "").strip(), platform,
                  "gemini-3.6-flash", "gemini-flash-latest", "gemini-2.5-flash"]
    models: list[str] = []
    for model in candidates:
        if model and model not in models:
            models.append(model)
    return models


def _gemini_transcribe(audio_bytes: bytes, language: str) -> str:
    """Transcribe a WAV clip with Gemini (it understands audio inline).

    Plain REST like the rest of the ERP's Gemini calls: the robot used the
    google-generativeai SDK, which isn't in the image, so every clip came
    back empty and the robot never answered anyone.
    """
    import requests

    payload = {
        "contents": [{"role": "user", "parts": [
            {"text": _STT_PROMPT},
            {"inline_data": {"mime_type": "audio/wav",
                             "data": base64.b64encode(audio_bytes).decode("ascii")}},
        ]}],
        # Thinking models spend output tokens before the text: leave room.
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }
    for model in _stt_models():
        try:
            resp = requests.post(_GEMINI_URL.format(model=model), params={"key": _gemini_key()},
                                 json=payload, timeout=_STT_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("robot STT: Gemini unreachable (%s): %s", model, exc)
            return ""
        if resp.status_code != 200:
            logger.warning("robot STT: %s answered %s: %s", model, resp.status_code, resp.text[:200])
            continue
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning("robot STT: unexpected reply from %s: %s", model, resp.text[:200])
            continue
        text = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        return text
    return ""


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------

def synthesize(text: str, language: str = "ar") -> Optional[bytes]:
    """Synthesize speech audio (mp3 bytes) for the amp, or None if unavailable."""
    if not text:
        return None
    provider = TTS_PROVIDER
    if provider in ("auto", "gtts"):
        audio = _gtts_synthesize(text, language)
        if audio:
            return audio
    return None


def _gtts_synthesize(text: str, language: str) -> Optional[bytes]:
    """gTTS mp3 bytes (Arabic/English), or None if the lib isn't installed."""
    try:
        import io
        from gtts import gTTS
        lang = "ar" if any("؀" <= c <= "ۿ" for c in text) else "en"
        buf = io.BytesIO()
        gTTS(text=text, lang=lang).write_to_fp(buf)
        return buf.getvalue()
    except Exception:
        return None


def synthesize_wav(text: str, language: str = "ar", *, rate: int = 16000) -> Optional[bytes]:
    """Speech as 16-bit mono PCM WAV at `rate` Hz — what the ESP32 amp plays.

    The MAX98357A takes raw I2S PCM and the ESP32 has no MP3 decoder to spare,
    so the server converts gTTS's MP3 with ffmpeg (installed in the image).
    Returns None when TTS or ffmpeg isn't available.
    """
    mp3 = synthesize(text, language)
    if not mp3:
        return None
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-i", "pipe:0",
             "-ac", "1", "-ar", str(rate), "-sample_fmt", "s16", "-f", "wav", "pipe:1"],
            input=mp3, capture_output=True, timeout=20, check=True,
        )
        return proc.stdout or None
    except Exception:
        return None
