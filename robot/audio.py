"""
robot/audio.py — speech-to-text (mic) and text-to-speech (amp) for the robot.

The INMP441 mic streams PCM/audio up; the MAX98357A amp plays synthesized
speech. Both are behind pluggable providers so the system runs without heavy
deps and upgrades by installing a library or setting an API key.

  * transcribe(audio_bytes, language) → text ("" if unavailable)
  * synthesize(text, language)        → audio bytes (mp3) or None

Providers (env `ROBOT_STT_PROVIDER` / `ROBOT_TTS_PROVIDER`):
  STT: "gemini" (the ERP Gemini key, over the Gemini REST API) | "none"
  TTS: "edge" (Microsoft neural voices, an Egyptian one for Arabic; no key)
       | "gtts" (Google Translate's voice, Arabic without the Egyptian accent)
       | "none". "auto" tries edge, then gtts.

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

def transcribe(audio_bytes: bytes, language: str = "ar", *, name: str = "موس") -> str:
    """Transcribe spoken audio to text. Returns '' when no provider is available.

    `name` is the robot's wake name: told to the model, it spells it the way
    the wake-name check expects instead of a near miss («موز», "Moose").
    """
    if not audio_bytes:
        return ""
    provider = STT_PROVIDER
    if provider in ("auto", "gemini") and _gemini_key():
        text = _gemini_transcribe(audio_bytes, language, name or "موس")
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
    "shop and speaks Egyptian Arabic or English, often mixing both: write "
    "Arabic speech in Arabic script and English speech in English letters. "
    "The robot listening is called «{name}»: people call it with «يا {name}» "
    "(\"Hey …\" in English). Whenever its name is said, write it exactly as "
    "«{name}», in Arabic letters."
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


def gemini_generate(parts: list, *, what: str = "STT") -> str:
    """Text Gemini answers for `parts` (a prompt, audio, …), or '' when it
    can't. Tries each of `_stt_models()` in turn.

    Plain REST like the rest of the ERP's Gemini calls: the robot used the
    google-generativeai SDK, which isn't in the image, so every clip came
    back empty and the robot never answered anyone.
    """
    import requests

    key = _gemini_key()
    if not key:
        return ""
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        # Thinking models spend output tokens before the text: leave room.
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }
    for model in _stt_models():
        try:
            resp = requests.post(_GEMINI_URL.format(model=model), params={"key": key},
                                 json=payload, timeout=_STT_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("robot %s: Gemini unreachable (%s): %s", what, model, exc)
            return ""
        if resp.status_code != 200:
            logger.warning("robot %s: %s answered %s: %s", what, model, resp.status_code,
                           resp.text[:200])
            continue
        try:
            reply_parts = resp.json()["candidates"][0]["content"]["parts"]
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning("robot %s: unexpected reply from %s: %s", what, model, resp.text[:200])
            continue
        return " ".join(p.get("text", "") for p in reply_parts if not p.get("thought")).strip()
    return ""


def _gemini_transcribe(audio_bytes: bytes, language: str, name: str = "موس") -> str:
    """Transcribe a WAV clip with Gemini (it understands audio inline)."""
    return gemini_generate([
        {"text": _STT_PROMPT.format(name=name)},
        {"inline_data": {"mime_type": "audio/wav",
                         "data": base64.b64encode(audio_bytes).decode("ascii")}},
    ])


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------

def synthesize(text: str, language: str = "ar") -> Optional[bytes]:
    """Synthesize speech audio (mp3 bytes) for the amp, or None if unavailable."""
    if not text:
        return None
    provider = TTS_PROVIDER
    if provider in ("auto", "edge"):
        audio = _edge_synthesize(text)
        if audio:
            return audio
    if provider in ("auto", "gtts"):
        audio = _gtts_synthesize(text, language)
        if audio:
            return audio
    return None


# Egyptian Arabic and English neural voices (Shakir / Salma are the Egyptian
# ones; `edge-tts --list-voices` lists the rest).
_EDGE_VOICES = {"ar": ("ROBOT_TTS_VOICE_AR", "ar-EG-ShakirNeural"),
                "en": ("ROBOT_TTS_VOICE_EN", "en-US-GuyNeural")}
_EDGE_TIMEOUT = 12          # then gTTS gets its turn; the robot waits 30 s in all


def _edge_synthesize(text: str) -> Optional[bytes]:
    """Edge's neural voice for the text's language as mp3, or None.

    Run on a worker thread with its own event loop and a hard deadline: the
    library's own stream_sync() waits forever when the connection fails, and
    the robot must fall back to gTTS rather than hang.
    """
    try:
        import edge_tts
    except ImportError:
        return None
    import asyncio
    import concurrent.futures
    from .language import detect

    env, default = _EDGE_VOICES[detect(text)]
    voice = os.getenv(env, "").strip() or default

    async def collect() -> bytes:
        audio = bytearray()
        speech = edge_tts.Communicate(text, voice, connect_timeout=5, receive_timeout=10)
        async for chunk in speech.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return bytes(audio)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(asyncio.run, collect()).result(timeout=_EDGE_TIMEOUT) or None
    except Exception as exc:
        logger.warning("robot TTS: edge voice %s failed, using gTTS: %s", voice, exc)
        return None
    finally:
        pool.shutdown(wait=False)


def _gtts_synthesize(text: str, language: str) -> Optional[bytes]:
    """gTTS mp3 bytes (Arabic/English), or None if the lib isn't installed."""
    try:
        import io
        from gtts import gTTS
        from .language import detect
        lang = detect(text)        # mostly English reads in English, one Arabic name or not
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
