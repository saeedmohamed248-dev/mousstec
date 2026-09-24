"""
robot/audio.py — speech-to-text (mic) and text-to-speech (amp) for the robot.

The INMP441 mic streams PCM/audio up; the MAX98357A amp plays synthesized
speech. Both are behind pluggable providers so the system runs without heavy
deps and upgrades by installing a library or setting an API key.

  * transcribe(audio_bytes, language) → text ("" if unavailable)
  * synthesize(text, language)        → audio bytes (mp3) or None

Providers (env `ROBOT_STT_PROVIDER` / `ROBOT_TTS_PROVIDER`):
  STT: "gemini" (uses the ERP Gemini key via google-generativeai) | "none"
  TTS: "gtts" (offline-ish, needs internet) | "none"

If a provider isn't available the functions degrade gracefully: `/voice/` still
works when the caller posts a ready `transcript`, and `/speak/` returns 204.
"""

from __future__ import annotations

import os
from typing import Optional

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


def _gemini_transcribe(audio_bytes: bytes, language: str) -> str:
    """Transcribe via google-generativeai (Gemini understands audio inline)."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=_gemini_key())
        model = genai.GenerativeModel(
            os.getenv("ROBOT_STT_MODEL", "gemini-2.0-flash")
        )
        prompt = (
            "Transcribe this audio verbatim. Reply with ONLY the transcription "
            "text, no quotes, no commentary. The speaker may use Arabic or "
            "English."
        )
        resp = model.generate_content([
            prompt,
            {"mime_type": "audio/wav", "data": audio_bytes},
        ])
        return (getattr(resp, "text", "") or "").strip()
    except Exception:
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
