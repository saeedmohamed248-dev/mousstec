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
TRIES = 8                                        # a download that keeps breaking: this many goes


def is_vosk(model: str) -> bool:
    return (model or "").strip().lower().startswith("vosk")


class VoskEars:
    """The same face as ears.Ears: load(), ready, hear(), timed(), warmup."""

    def __init__(self, model: str = "vosk", *, home):
        self.model_name = (model or "vosk").strip().lower()
        self.folder = Path(home) / "models" / MODELS.get(self.model_name, self.model_name)
        self.downloads = Path.home() / "Downloads"   # where a browser puts the zip, if fetched by hand
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
        """The model's zip (it holds its own folder) into home/models, once.
        A zip already downloaded by hand (the browser's Downloads, or
        home/models) is taken as it is."""
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        name = f"{self.folder.name}.zip"
        zipped = next((z for z in (self.folder.parent / name, self.downloads / name) if z.is_file()), None)
        if zipped is not None:
            log(f"[EARS] unpacking {zipped}")
        else:
            zipped = self._fetch(log)
        try:
            with zipfile.ZipFile(zipped) as zf:
                zf.extractall(self.folder.parent)
        except zipfile.BadZipFile:
            if zipped.suffix == ".part":
                zipped.unlink()                  # broken: the next start downloads it afresh
            raise
        if zipped.suffix == ".part":
            zipped.unlink()
        if not (self.folder / "conf").is_dir():
            raise RuntimeError(f"the zip had no {self.folder.name}/ in it")

    def _fetch(self, log) -> Path:
        """Download it to <name>.zip.part. 🐛 [FIX]: a dropped connection
        (ChunkedEncodingError at 10% on the shop's line) lost everything and
        the robot started with no ears: now it picks up where it broke, with
        an HTTP Range, a few times over — and next start too."""
        import requests

        part = self.folder.parent / f"{self.folder.name}.zip.part"
        url = URL.format(name=self.folder.name)
        log(f"[EARS] downloading {url} — once")
        broke = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
        for attempt in range(1, TRIES + 1):
            have = part.stat().st_size if part.exists() else 0
            try:
                if self._fetch_from(requests, url, part, have, log):
                    return part
            except broke as exc:
                if attempt == TRIES:
                    raise
                got = part.stat().st_size if part.exists() else 0
                log(f"[EARS]   the connection broke at {got >> 20} MB ({type(exc).__name__}) — "
                    f"going on from there ({attempt + 1}/{TRIES})")
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"{url} never came whole")

    @staticmethod
    def _fetch_from(requests, url, part, have, log) -> bool:
        """One try, from byte `have` on. True once the whole file is there."""
        headers = {"Range": f"bytes={have}-"} if have else {}
        with requests.get(url, stream=True, timeout=60, headers=headers) as resp:
            if have and resp.status_code == 416:            # nothing left to send: it's all here
                return True
            resp.raise_for_status()
            if resp.status_code != 206:                     # the server ignored the Range: from 0
                have = 0
            size = int(resp.headers.get("Content-Length") or 0)
            whole = (resp.headers.get("Content-Range") or "").rpartition("/")[2]   # bytes 5-9/10
            total = int(whole) if whole.isdigit() else have + size
            got, said = have, (have * 10 // total if total else 0)
            with open(part, "ab" if have else "wb") as out:
                for chunk in resp.iter_content(1 << 20):
                    out.write(chunk)
                    got += len(chunk)
                    if total and got * 10 // total > said:
                        said = got * 10 // total
                        log(f"[EARS]   {said * 10}%")
        return not total or got >= total

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
