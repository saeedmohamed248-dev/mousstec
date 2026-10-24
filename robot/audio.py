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
import hashlib
import logging
import os
import re
import struct
from typing import Optional

logger = logging.getLogger(__name__)

STT_PROVIDER = os.getenv("ROBOT_STT_PROVIDER", "auto").lower()
TTS_PROVIDER = os.getenv("ROBOT_TTS_PROVIDER", "auto").lower()


# ---------------------------------------------------------------------------
# G.711 µ-law — what the robot sends and plays over a slow line
# ---------------------------------------------------------------------------
# One byte a sample instead of two: the question going up and the answer
# coming down are half the size, at the quality the bridge's DAC path already
# played replies at. Bit-for-bit the firmware's muEncode/muDecode
# (robot/tests/test_firmware_codec.py compiles those and compares).

ULAW = "ulaw"
PCM = "pcm"
_WAV_PCM, _WAV_ULAW = 1, 7


def _ulaw_decode_table():
    table = []
    for u in range(256):
        u = ~u & 0xFF
        exp, mant = (u >> 4) & 7, u & 0x0F
        v = (((mant << 3) + 0x84) << exp) - 0x84
        table.append(-v if u & 0x80 else v)
    return table


_ULAW_DECODE = _ulaw_decode_table()


def ulaw_encode(pcm: bytes) -> bytes:
    """16-bit little-endian PCM → µ-law bytes."""
    import numpy as np

    s = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.int32)
    sign = np.where(s < 0, 0x80, 0)
    v = np.minimum(np.abs(s), 32635) + 0x84
    exp = np.frexp(v.astype(np.float64))[1] - 8          # highest bit − 7, exact
    mant = (v >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant) & 0xFF).astype(np.uint8).tobytes()


def ulaw_decode(data: bytes) -> bytes:
    """µ-law bytes → 16-bit little-endian PCM."""
    import numpy as np

    table = np.array(_ULAW_DECODE, dtype="<i2")
    return table[np.frombuffer(data, dtype=np.uint8)].tobytes()


def make_wav(samples: bytes, rate: int = 16000, codec: str = PCM) -> bytes:
    """A mono WAV around `samples` (16-bit PCM, or µ-law bytes)."""
    tag, bits = (_WAV_ULAW, 8) if codec == ULAW else (_WAV_PCM, 16)
    align = bits // 8
    return (b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, tag, 1, rate, rate * align, align, bits)
            + b"data" + struct.pack("<I", len(samples)) + samples)


def wav_parts(data: bytes):
    """(format tag, sample rate, sample bytes) of a WAV, or None."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt, pos = None, 12
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if chunk == b"data" and (size == 0 or pos + 8 + size > len(data)):
            size = len(data) - pos - 8      # ffmpeg writing to a pipe can't fill sizes in
        body = data[pos + 8:pos + 8 + size]
        if chunk == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk == b"data" and fmt:
            return fmt[0], fmt[2], body
        pos += 8 + size + (size & 1)
    return None


def as_pcm_wav(data: bytes) -> bytes:
    """A µ-law WAV from the robot as the 16-bit PCM WAV speech-to-text takes;
    anything else unchanged."""
    parts = wav_parts(data)
    if parts and parts[0] == _WAV_ULAW:
        return make_wav(ulaw_decode(parts[2]), parts[1], PCM)
    return data


def _peak_normalized(pcm: bytes, target: float = 0.85, max_gain: float = 4.0) -> bytes:
    """Loudest sample at `target` of full scale (gain capped). The bridge used
    to do this after downloading the whole reply; done here, it can start
    playing the first bytes that arrive."""
    import numpy as np

    s = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    peak = float(np.abs(s).max()) if s.size else 0.0
    if peak < 1:
        return pcm
    gain = min(max_gain, target * 32767 / peak)
    return np.clip(s * gain, -32768, 32767).astype("<i2").tobytes()


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------

class SttUnavailable(Exception):
    """Speech-to-text couldn't run: no key, or Gemini unreachable / erroring."""


def transcribe(audio_bytes: bytes, language: str = "ar", *, name: str = "موس",
               strict: bool = False) -> str:
    """Transcribe spoken audio to text. Returns '' when no provider is available.

    `name` is the robot's wake name: told to the model, it spells it the way
    the wake-name check expects instead of a near miss («موز», "Moose").
    `strict` raises SttUnavailable instead of returning '' when speech-to-text
    itself failed, so a clip recorded offline is kept and retried rather than
    taken for silence.
    """
    if not audio_bytes:
        return ""
    provider = STT_PROVIDER
    if provider in ("auto", "gemini") and _gemini_key():
        return _gemini_transcribe(audio_bytes, language, name or "موس", strict=strict) or ""
    # 🐛 [FIX]: من غير مفتاح كان الروبوت بيطرش في صمت — ولا سطر في اللوج يقول ليه،
    # فـ `logs web | grep "robot STT"` كان بيطلع فاضي زي ما يكون كل حاجة تمام.
    logger.warning("robot STT: no speech-to-text provider (GEMINI_API_KEY is empty, "
                   "ROBOT_STT_PROVIDER=%s): the robot can't hear", provider)
    if strict:
        raise SttUnavailable("no speech-to-text provider configured")
    return ""


def _gemini_key() -> str:
    try:
        from django.conf import settings
        return str(getattr(settings, "GEMINI_API_KEY", "") or "").strip()
    except Exception:
        return ""


_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_STT_TIMEOUT = 8           # per model; a hung one used to hold the answer 15 s
_STT_BUDGET = 20           # all models together: the robot waits 30 s for /voice/
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


# Models that refused thinking off (a "thinking only" model answers 400):
# asked without it from then on.
_THINKING_REQUIRED: set = set()

# Model health, per process. In the shop's logs every sentence went
# gemini-3.6-flash → 429/503, gemini-flash-latest → 503 or no answer for 15 s,
# and only then to the model that worked: seconds before every answer. So the
# model that answered last is asked first, and one that just failed rests
# (it's still tried, last, if nothing else is left).
_REST_SECONDS = {404: 6 * 3600, 429: 600, "timeout": 300}
_REST_DEFAULT = 120                  # 5xx: overloaded, try again soon
_model_rest: dict = {}               # model → time.monotonic() it may be tried again
_last_ok = {"model": ""}


def _forget_model_health():
    _model_rest.clear()
    _last_ok["model"] = ""


def _rest(model: str, reason, body: str = "") -> None:
    import re
    import time

    seconds = _REST_SECONDS.get(reason, _REST_DEFAULT)
    if reason == 429:                # Google says when the quota frees up
        m = re.search(r'"retryDelay"\s*:\s*"(\d+)(?:\.\d+)?s"', body or "")
        if m:
            seconds = min(max(int(m.group(1)), 30), 3600)
    _model_rest[model] = time.monotonic() + seconds


def _models_in_order() -> list:
    import time

    models = _stt_models()
    if _last_ok["model"] in models:
        models.remove(_last_ok["model"])
        models.insert(0, _last_ok["model"])
    now = time.monotonic()
    awake = [m for m in models if _model_rest.get(m, 0) <= now]
    return awake + [m for m in models if m not in awake]


def gemini_generate(parts: list, *, what: str = "STT", strict: bool = False,
                    tools: Optional[list] = None, sources: Optional[list] = None) -> str:
    """Text Gemini answers for `parts` (a prompt, audio, …), or '' when it
    can't (`strict`: raise SttUnavailable instead). Tries each of
    `_stt_models()` in turn.

    `tools=[{"google_search": {}}]` lets it look things up on the internet;
    the pages it read are added to `sources` ([{"title", "uri"}]).

    Plain REST like the rest of the ERP's Gemini calls: the robot used the
    google-generativeai SDK, which isn't in the image, so every clip came
    back empty and the robot never answered anyone.

    Thinking is switched off: writing down what was said, translating one
    reply or a two-line answer gains nothing from it, and it cost seconds
    before every answer the robot gave.
    """
    import requests

    key = _gemini_key()
    if not key:
        if strict:
            raise SttUnavailable("no Gemini key")
        return ""
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        # Room for a model that thinks anyway (it spends tokens before the text).
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }
    if tools:
        payload["tools"] = tools
    fast = {**payload, "generationConfig": {**payload["generationConfig"],
                                            "thinkingConfig": {"thinkingBudget": 0}}}
    import time
    started = time.monotonic()
    failed = []                          # «gemini-x 429» — what to tell when all of them fail
    for model in _models_in_order():
        if time.monotonic() - started > _STT_BUDGET:
            break
        url = _GEMINI_URL.format(model=model)
        try:
            body = payload if model in _THINKING_REQUIRED else fast
            resp = requests.post(url, params={"key": key}, json=body, timeout=_STT_TIMEOUT)
            if resp.status_code == 400 and body is fast and "thinking" in resp.text.lower():
                _THINKING_REQUIRED.add(model)
                resp = requests.post(url, params={"key": key}, json=payload, timeout=_STT_TIMEOUT)
        except requests.ConnectionError as exc:
            # No route to Google at all: the next model won't fare better.
            logger.warning("robot %s: Gemini unreachable (%s): %s", what, model, exc)
            if strict:
                raise SttUnavailable(str(exc)) from exc
            return ""
        except requests.Timeout as exc:
            # 🐛 [FIX]: a model that doesn't answer in time is overloaded, not
            # the internet: it used to end the whole try (the robot "heard
            # nothing") although the next model was answering fine.
            logger.warning("robot %s: %s didn't answer in time, trying the next: %s", what, model, exc)
            _rest(model, "timeout")
            failed.append(f"{model} timeout")
            continue
        except requests.RequestException as exc:
            logger.warning("robot %s: Gemini unreachable (%s): %s", what, model, exc)
            if strict:
                raise SttUnavailable(str(exc)) from exc
            return ""
        if resp.status_code != 200:
            logger.warning("robot %s: %s answered %s: %s", what, model, resp.status_code,
                           resp.text[:200])
            if resp.status_code in (404, 429) or resp.status_code >= 500:
                _rest(model, resp.status_code if resp.status_code in (404, 429) else "5xx", resp.text)
            failed.append(f"{model} {resp.status_code}")
            continue
        try:
            candidate = resp.json()["candidates"][0]
            reply_parts = candidate["content"]["parts"]
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning("robot %s: unexpected reply from %s: %s", what, model, resp.text[:200])
            continue
        _last_ok["model"] = model
        _model_rest.pop(model, None)
        if sources is not None:
            sources.extend(_grounding_sources(candidate))
        return " ".join(p.get("text", "") for p in reply_parts if not p.get("thought")).strip()
    if strict:
        raise SttUnavailable("every Gemini model failed: " + (", ".join(failed) or "out of time"))
    return ""


def _grounding_sources(candidate) -> list:
    """The web pages a Google-Search-grounded answer was built from."""
    meta = (candidate or {}).get("groundingMetadata") or {}
    out = []
    for chunk in meta.get("groundingChunks") or []:
        web = (chunk or {}).get("web") or {}
        title, uri = str(web.get("title") or "").strip(), str(web.get("uri") or "").strip()
        if (title or uri) and all(o["uri"] != uri or o["title"] != title for o in out):
            out.append({"title": title, "uri": uri})
    return out


def _gemini_transcribe(audio_bytes: bytes, language: str, name: str = "موس",
                       strict: bool = False) -> str:
    """Transcribe a WAV clip with Gemini (it understands audio inline).

    The robot sends µ-law to halve the upload; Gemini gets 16-bit PCM."""
    audio_bytes = as_pcm_wav(audio_bytes)
    return gemini_generate([
        {"text": _STT_PROMPT.format(name=name)},
        {"inline_data": {"mime_type": "audio/wav",
                         "data": base64.b64encode(audio_bytes).decode("ascii")}},
    ], strict=strict)


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------

def synthesize(text: str, language: str = "ar") -> Optional[bytes]:
    """Synthesize speech audio (mp3 bytes) for the amp, or None if unavailable."""
    return _synthesize_with(text, language)[0]


def _synthesize_with(text: str, language: str = "ar"):
    """(mp3 bytes or None, the provider that made them)."""
    if not text:
        return None, ""
    from .language import detect, egyptian_numbers, speakable
    if detect(text) == "ar":
        text = egyptian_numbers(speakable(text))
    provider = TTS_PROVIDER
    if provider in ("auto", "edge"):
        audio = _edge_synthesize(text)
        if audio:
            return audio, "edge"
    if provider in ("auto", "gtts"):
        audio = _gtts_synthesize(text, language)
        if audio:
            return audio, "gtts"
    return None, ""


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


# Replies the robot says again and again («أيوه، تحت أمرك.», the price of a
# part people keep asking about) are kept, so the second time costs no TTS.
# Only Edge's Egyptian voice is kept: a gTTS stand-in for an Edge outage
# would otherwise outlive the outage.
_TTS_CACHE_SECONDS = 3 * 24 * 3600
_TTS_CACHE_VERSION = 2          # 2: part names said aloud cleaned (language.speakable)


def _tts_cache_key(text: str, rate: int, codec: str) -> str:
    voices = "|".join(os.getenv(v, "") for v in ("ROBOT_TTS_VOICE_AR", "ROBOT_TTS_VOICE_EN"))
    raw = f"{_TTS_CACHE_VERSION}|{TTS_PROVIDER}|{voices}|{rate}|{codec}|{text}"
    return "robot:tts:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _pieces(text: str, longest: int = 60) -> list:
    """The reply cut where a voice pauses anyway: at the end of each sentence,
    and a long one (a list of parts) at its commas — so the pieces can be
    voiced at the same time."""
    out = []
    for sentence in re.split(r"(?<=[.!?؟])\s+", " ".join(text.split())):
        if len(sentence) <= longest:
            out.append(sentence)
            continue
        part = ""
        for clause in re.split(r"(?<=[،,])\s+", sentence):
            if part and len(part) + 1 + len(clause) > longest:
                out.append(part)
                part = clause
            else:
                part = f"{part} {clause}".strip()
        out.append(part)
    return [piece for piece in out if piece]


def _voiced_pcm(text: str, language: str, rate: int):
    """(16-bit PCM samples at `rate`, the provider) for one piece, or (None, "").
    No Django in here: it runs on worker threads."""
    mp3, provider = _synthesize_with(text, language)
    if not mp3:
        return None, ""
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, ""
    try:
        proc = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-i", "pipe:0",
             "-ac", "1", "-ar", str(rate), "-sample_fmt", "s16", "-f", "wav", "pipe:1"],
            input=mp3, capture_output=True, timeout=20, check=True,
        )
    except Exception:
        return None, ""
    parts = wav_parts(proc.stdout or b"")
    return (parts[2], provider) if parts else (None, "")


_PIECE = "piece"            # a piece's bare samples, never mistaken for a whole reply's WAV


def _keepable(provider: str) -> bool:
    # Only Edge's Egyptian voice is kept: a gTTS stand-in for an Edge outage
    # would otherwise outlive the outage.
    return provider == "edge" or TTS_PROVIDER == "gtts"


def synthesize_wav(text: str, language: str = "ar", *, rate: int = 16000,
                   codec: str = PCM, providers: Optional[list] = None) -> Optional[bytes]:
    """Speech as a mono WAV at `rate` Hz — what the ESP32 amp plays.

    `codec`: 16-bit PCM, or µ-law (half the bytes to download, loudness
    already evened out so the robot can play it as it arrives).
    The MAX98357A takes raw I2S PCM and the ESP32 has no MP3 decoder to spare,
    so the server converts the voice's MP3 with ffmpeg (installed in the image).
    `providers` (a list) gets which voice said it: "edge", "gtts" or "cache".
    Returns None when TTS or ffmpeg isn't available.

    🐛 [FIX]: a reply listing three parts took 5.5 s to voice, one long piece
    after another. It's voiced a sentence (or a few list items) at a time, the
    pieces at once, each kept on its own — «تقصد أنهي؟» is voiced once ever.
    """
    if not text:
        return None
    from django.core.cache import cache

    key = _tts_cache_key(text, rate, codec)
    cached = cache.get(key)
    if cached:
        if providers is not None:
            providers.append("cache")
        return cached
    pieces = _pieces(text)
    # The cache is read and written here, never on the workers (its keys
    # follow the request's tenant).
    found = {i: cache.get(_tts_cache_key(piece, rate, _PIECE)) for i, piece in enumerate(pieces)}
    todo = [i for i, pcm in found.items() if not pcm]
    voiced = {}
    if len(todo) == 1:
        voiced[todo[0]] = _voiced_pcm(pieces[todo[0]], language, rate)
    elif todo:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            jobs = {i: pool.submit(_voiced_pcm, pieces[i], language, rate) for i in todo}
            voiced = {i: job.result() for i, job in jobs.items()}
    used = ["cache"] * (len(pieces) - len(todo))
    for i, (pcm, provider) in voiced.items():
        if pcm is None:
            return None
        used.append(provider)
        found[i] = pcm
        if _keepable(provider):
            cache.set(_tts_cache_key(pieces[i], rate, _PIECE), pcm, _TTS_CACHE_SECONDS)
    if providers is not None:
        providers.extend(sorted(set(used)))
    pcm = b"".join(found[i] for i in range(len(pieces)))
    if codec == ULAW:
        wav = make_wav(ulaw_encode(_peak_normalized(pcm)), rate, ULAW)
    else:
        wav = make_wav(pcm, rate, PCM)
    if all(_keepable(p) or p == "cache" for p in used):
        cache.set(key, wav, _TTS_CACHE_SECONDS)
    return wav
