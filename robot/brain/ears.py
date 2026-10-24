"""The robot's ears: Whisper on this computer, no internet, no Gemini.

faster-whisper (MIT licence, fine to ship in a product) runs Whisper with
8-bit weights on the CPU. Two things make it hear a parts shop well:

* a hint before every clip: the robot's name and the names of the parts in
  stock, so «فلتر زيت كورولا» is written the way the stock lists it;
* dropping what Whisper invents on noise — it was trained on subtitled
  videos, so a cough can come out as «ترجمة نانسي قنقر».
"""

import inspect
import os
import re
import time

import numpy as np

# Subtitle credits and sign-offs Whisper writes over noise or silence.
HALLUCINATIONS = (
    "ترجمة نانسي قنقر", "نانسي قنقر", "اشتركوا في القناة", "اشترك في القناة",
    "شكرا للمشاهدة", "شكراً للمشاهدة", "شكرا على المشاهدة", "شكراً على المشاهدة",
    "thanks for watching", "thank you for watching", "subtitles by", "please subscribe",
)
_PUNCT = re.compile(r"[\s\.\,\!\?،؟؛\-\"'«»…:;()\[\]]+")
PROMPT_CHARS = 400                   # Whisper reads at most ~224 tokens of hint
OFF = ("off", "server", "none")      # [ears] model = off: the server listens (a slow computer)
# What a Windows PC without the C++ runtime needs before ctranslate2.dll loads.
VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def clean(text: str) -> str:
    """The transcript, or '' when it's empty, punctuation or a known invention."""
    text = " ".join((text or "").split())
    bare = _PUNCT.sub(" ", text).strip().lower()
    if not bare or not any(ch.isalpha() for ch in bare):
        return ""
    for phrase in HALLUCINATIONS:
        if phrase.lower() in bare and len(bare) <= len(phrase) + 12:
            return ""
    return text


def hints(catalog: dict, name: str = "موس", max_chars: int = PROMPT_CHARS) -> str:
    """The hint read before every clip: the wake name, then part names
    (what's in stock first, the first words of each, no repeats)."""
    text = f"يا {name}، عندك فلتر زيت؟ بكام؟"
    parts = sorted(catalog.get("parts") or [], key=lambda p: -(p.get("stock") or 0))
    labels, seen = [], set()
    for part in parts:
        label = " ".join(str(part.get("name") or "").split()[:4])
        key = label.lower()
        if not label or key in seen or not any(ch.isalpha() for ch in label):
            continue
        if len(text) + 1 + len("، ".join(labels + [label])) > max_chars:
            break
        seen.add(key)
        labels.append(label)
    return text + (" " + "، ".join(labels) if labels else "")


class Ears:
    """One Whisper model, loaded once and kept warm."""

    def __init__(self, model: str = "small", *, device: str = "auto", compute_type: str = "int8",
                 language: str = "ar", beam_size: int = 1):
        self.model_name, self.device, self.compute_type = model, device, compute_type
        self.language = None if language in ("", "auto") else language
        self.beam_size = max(1, int(beam_size))
        self.error = ""
        self.warmup = 0.0                # seconds the first (silent) clip took: how fast this computer hears
        self._model = None
        self._hotwords = False

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        """Load (and, the first time, download) the model. False + `error` if it can't."""
        if self.model_name.strip().lower() in OFF:
            self.error = "turned off in brain.ini ([ears] model = off)"
            return False
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            self.error = f"faster-whisper isn't installed ({exc})"
            return False
        except OSError as exc:
            # 🐛 [FIX]: a DLL that won't load (no Visual C++ runtime on Windows) is an
            # OSError, not an ImportError: it ended the robot at boot instead of
            # letting the server listen.
            self.error = f"{type(exc).__name__}: {exc}"
            if os.name == "nt":
                self.error += f" — install the Microsoft Visual C++ runtime (x64): {VC_REDIST}"
            return False
        try:
            self._model = WhisperModel(self.model_name, device=self.device,
                                       compute_type=self.compute_type,
                                       cpu_threads=os.cpu_count() or 4)
        except Exception as exc:                 # no download, unknown model, no RAM…
            self.error = f"{type(exc).__name__}: {exc}"
            self._model = None
            return False
        self._hotwords = "hotwords" in inspect.signature(self._model.transcribe).parameters
        t0 = time.monotonic()
        self.hear(np.zeros(8000, dtype=np.int16))  # the first clip is slow: not a customer's
        self.warmup = time.monotonic() - t0
        return True

    def hear(self, samples: np.ndarray, prompt: str = "") -> str:
        """int16 16 kHz samples → what was said ('' for nothing understood)."""
        if self._model is None:
            return ""
        audio = np.asarray(samples, dtype=np.float32) / 32768.0
        kwargs = dict(language=self.language, beam_size=self.beam_size,
                      condition_on_previous_text=False, without_timestamps=True, vad_filter=False)
        if prompt:
            # Once: each is kept to half of Whisper's 448-token window, so the
            # hint given as both left no room for what was actually said.
            kwargs["hotwords" if self._hotwords else "initial_prompt"] = prompt
        segments, _info = self._model.transcribe(audio, **kwargs)
        said = []
        for seg in segments:                     # a generator: decoding happens here
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                continue                         # Whisper itself thinks nobody spoke
            said.append(seg.text)
        return clean(" ".join(said))

    def timed(self, samples: np.ndarray, prompt: str = ""):
        """(text, seconds) — for the log and the bench."""
        t0 = time.monotonic()
        text = self.hear(samples, prompt)
        return text, time.monotonic() - t0
