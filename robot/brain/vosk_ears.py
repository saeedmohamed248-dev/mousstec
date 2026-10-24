"""Ears that need no AVX2: Vosk (Kaldi, Apache 2.0), on this computer.

Whisper's encoder reads 30 s of sound for every sentence, however short: on
the shop's Celeron G3900 (no AVX2) that is 13.6 s for one second of speech,
whichever model or number type. Kaldi reads only what was said, and runs on
a Raspberry Pi: about a second here. It gets fewer words right than Whisper
small — `run_windows.bat bench --models vosk,small` says how many, on the
shop's own voice. No internet and no Google once the model is downloaded.

    [ears]
    model = vosk        ; Arabic, broadcast-trained (~320 MB)
    model = vosk-big    ; bigger and slower, more words right (~1.3 GB)
"""

import json
import time
import zipfile
from pathlib import Path

import numpy as np

from .ears import clean

MODELS = {"vosk": "vosk-model-ar-mgb2-0.4", "vosk-big": "vosk-model-ar-0.22-linto-1.1.0"}
URL = "https://alphacephei.com/vosk/models/{name}.zip"
RATE = 16000


def is_vosk(model: str) -> bool:
    return (model or "").strip().lower().startswith("vosk")


class VoskEars:
    """The same face as ears.Ears: load(), ready, hear(), timed(), warmup."""

    def __init__(self, model: str = "vosk", *, home):
        self.model_name = (model or "vosk").strip().lower()
        self.folder = Path(home) / "models" / MODELS.get(self.model_name, self.model_name)
        self.error = ""
        self.warmup = 0.0
        self._model = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self, log=print) -> bool:
        try:
            import vosk
        except ImportError as exc:
            self.error = f"vosk isn't installed ({exc})"
            return False
        except OSError as exc:                   # its DLL won't load
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        try:
            if not (self.folder / "conf").is_dir():
                self._download(log)
            vosk.SetLogLevel(-1)
            self._model = vosk.Model(str(self.folder))
        except Exception as exc:                 # no download, a broken zip, no RAM…
            self.error = f"{type(exc).__name__}: {exc}"
            self._model = None
            return False
        t0 = time.monotonic()
        self.hear(np.zeros(RATE // 2, dtype=np.int16))
        self.warmup = time.monotonic() - t0
        return True

    def _download(self, log) -> None:
        """The model's zip (it holds its own folder) into home/models, once."""
        import requests

        self.folder.parent.mkdir(parents=True, exist_ok=True)
        part = self.folder.parent / f"{self.folder.name}.zip.part"
        url = URL.format(name=self.folder.name)
        log(f"[EARS] downloading {url} — once")
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total, got, said = int(resp.headers.get("Content-Length") or 0), 0, 0
            with open(part, "wb") as out:
                for chunk in resp.iter_content(1 << 20):
                    out.write(chunk)
                    got += len(chunk)
                    if total and got * 10 // total > said:
                        said = got * 10 // total
                        log(f"[EARS]   {said * 10}%")
        with zipfile.ZipFile(part) as zipped:
            zipped.extractall(self.folder.parent)
        part.unlink()
        if not (self.folder / "conf").is_dir():
            raise RuntimeError(f"the zip had no {self.folder.name}/ in it")

    def release(self, why: str) -> None:
        self._model = None
        self.error = why

    def hear(self, samples: np.ndarray, prompt: str = "") -> str:
        """int16 16 kHz samples → what was said ('' for nothing understood).
        `prompt` is Whisper's hint: Kaldi has no place for it."""
        if self._model is None:
            return ""
        import vosk

        rec = vosk.KaldiRecognizer(self._model, RATE)
        rec.AcceptWaveform(np.asarray(samples, dtype="<i2").tobytes())
        return clean(json.loads(rec.FinalResult() or "{}").get("text", ""))

    def timed(self, samples: np.ndarray, prompt: str = ""):
        t0 = time.monotonic()
        text = self.hear(samples, prompt)
        return text, time.monotonic() - t0
