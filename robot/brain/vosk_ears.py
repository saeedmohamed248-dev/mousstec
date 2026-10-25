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
import queue
import re
import threading
import time
import zipfile
from pathlib import Path

import numpy as np

from .ears import clean
from .wavs import LOUD_PEAK, MOST_GAIN, louder

MODELS = {"vosk": "vosk-model-ar-mgb2-0.4", "vosk-big": "vosk-model-ar-0.22-linto-1.1.0"}
URL = "https://alphacephei.com/vosk/models/{name}.zip"
RATE = 16000
TRIES = 8                                        # a download that keeps breaking: this many goes


_TASHKEEL = re.compile(r"[\u064B-\u0652\u0670]")
_LATIN, _ARABIC = re.compile(r"[A-Za-z]"), re.compile(r"[\u0600-\u06FF]")


def tidy(text: str) -> str:
    """Kaldi's own oddities out: tashkeel, and «words» of Latin and Arabic
    letters run together («إUٌٍأ», heard over a noise in the shop)."""
    words = [w for w in _TASHKEEL.sub("", text or "").split()
             if not (_LATIN.search(w) and _ARABIC.search(w))]
    return " ".join(words)


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
        self.words = []                          # [(word, start s, end s)] of the last sentence
        self.confs = []                          # how sure Kaldi was of each of them (0–1)
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

        rec = _recognizer(vosk, self._model)
        rec.AcceptWaveform(louder(samples).astype("<i2").tobytes())
        result = json.loads(rec.FinalResult() or "{}")
        self.words, self.confs = _words(result), _confs(result)
        return clean(tidy(result.get("text", "")))

    def timed(self, samples: np.ndarray, prompt: str = ""):
        t0 = time.monotonic()
        text = self.hear(samples, prompt)
        return text, time.monotonic() - t0

    def stream(self) -> "Stream":
        """Hearing while they're still talking (Kaldi reads sound as it comes)."""
        import vosk

        return Stream(_recognizer(vosk, self._model))


def _recognizer(vosk, model):
    rec = vosk.KaldiRecognizer(model, RATE)
    if hasattr(rec, "SetWords"):
        rec.SetWords(True)                       # when each word was said: kws.py needs it
    return rec


def _words(result: dict) -> list:
    """[(word, start, end)] from Kaldi's result (seconds into the sentence)."""
    out = []
    for w in result.get("result") or []:
        try:
            out.append((str(w["word"]), float(w["start"]), float(w["end"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _confs(result: dict) -> list:
    """How sure Kaldi was of each of `_words(result)` (0–1), in step with them:
    a word it wasn't sure of is a sentence a teacher hears (robot/teachers.py)."""
    out = []
    for w in result.get("result") or []:
        try:
            float(w["start"]), float(w["end"]), str(w["word"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            out.append(float(w.get("conf", 1.0)))
        except (TypeError, ValueError):
            out.append(1.0)
    return out


class Stream:
    """One sentence, heard as it's said, on a thread of its own: the words are
    ready the moment they stop. Heard afterwards, they took as long again on
    the shop's Celeron (2–3 s a sentence) — with the server and the voice on
    top of that.

    The whole sentence isn't there yet to find its loudest, so the gain
    (wavs.louder) only comes down as it gets louder: never above ×20, never
    below ×1."""

    def __init__(self, recognizer):
        self._rec = recognizer
        self._q: queue.Queue = queue.Queue()
        self._peak = LOUD_PEAK / MOST_GAIN           # the most gain: ×20
        self.error = ""
        self.words = []
        self.confs = []
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def feed(self, frame: np.ndarray) -> None:
        self._q.put(frame)

    def _work(self) -> None:
        while True:
            frame = self._q.get()
            if frame is None:
                return
            if self.error:
                continue                                 # broken: let the frames go by
            x = np.asarray(frame, dtype=np.int16)
            self._peak = max(self._peak, float(np.abs(x.astype(np.int32)).max(initial=0)))
            gain = max(1.0, LOUD_PEAK / self._peak)
            if gain > 1.0:
                x = np.clip(np.rint(x.astype(np.float32) * gain), -32768, 32767).astype(np.int16)
            try:
                self._rec.AcceptWaveform(x.astype("<i2").tobytes())
            except Exception as exc:                     # its DLL, its RAM…
                self.error = f"{type(exc).__name__}: {exc}"

    def finish(self, wait: float = 30.0):
        """(what was said, seconds it took after the last frame)."""
        t0 = time.monotonic()
        self._q.put(None)
        self._thread.join(wait)
        if self._thread.is_alive() or self.error:
            return "", time.monotonic() - t0
        try:
            result = json.loads(self._rec.FinalResult() or "{}")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            result = {}
        self.words, self.confs = _words(result), _confs(result)
        return clean(tidy(result.get("text", ""))), time.monotonic() - t0

    def drop(self) -> None:
        """Not a sentence after all (a cough, a door): forget it."""
        self._q.put(None)
