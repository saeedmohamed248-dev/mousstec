"""The robot's own voice, kept on disk so it can talk with the internet down.

While online, every clip an offline answer can need — the fixed phrases,
each part's name, each price («بسعر 450 جنيه») — is fetched once from the
server's /speak/ (the same Egyptian voice it always uses) and kept. Offline,
an answer is those clips one after the other, the way an airport or a phone
menu speaks: «أيوه،» · «فلتر زيت» · «متوفر (3 قطعة)» · «بسعر 450 جنيه».
"""

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from . import wavs

GAP_MS = 70                          # between clips: a breath, not a stutter
REFUSED_RETRY_S = 6 * 3600           # a text /speak/ wouldn't voice: asked again after this
REPLY_LONGEST = 160                  # a reply this short is kept as it was voiced (a long one is said once)
REPLIES_MOST = 300                   # the replies kept: the ones said lately


def key(text: str) -> str:
    """A text's name on disk — and, for the replies kept, on the live socket (robot/consumers.py)."""
    return hashlib.sha1(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:24]


class VoiceBank:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._refused = {}               # text → when /speak/ gave nothing for it

    def path(self, text: str) -> Path:
        return self.folder / (key(text) + ".wav")

    def has(self, text: str) -> bool:
        return self.path(text).is_file()

    def put(self, text: str, wav: bytes) -> None:
        if not wavs.parse(wav or b""):
            return                       # not audio: never keep a broken clip
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.path(text)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(wav)
        tmp.replace(path)

    def missing(self, texts: Iterable[str]) -> list:
        return [t for t in texts if t and not self.has(t)]

    def missing_now(self, texts: Iterable[str]) -> list:
        """Missing and worth asking for now (not refused lately)."""
        now = time.monotonic()
        return [t for t in self.missing(texts) if now - self._refused.get(t, -REFUSED_RETRY_S) >= REFUSED_RETRY_S]

    def compose(self, segments: list):
        """(int16 samples, rate) of the clips in a row, or None if one is missing."""
        pieces, rate = [], wavs.RATE
        for text in segments:
            path = self.path(text)
            if not path.is_file():
                return None
            parsed = wavs.parse(path.read_bytes())
            if not parsed:
                return None
            _tag, clip_rate, samples = parsed
            if clip_rate != rate:
                samples = wavs.resample(samples, clip_rate, rate).astype(np.int16)
            if pieces:
                pieces.append(np.zeros(rate * GAP_MS // 1000, dtype=np.int16))
            pieces.append(samples)
        if not pieces:
            return None
        return np.concatenate(pieces).astype(np.int16), rate

    def prefetch(self, texts: Iterable[str], fetch: Callable[[str], Optional[bytes]], *,
                 stop: threading.Event, pace: float = 1.0, budget: int = 0, log=None) -> int:
        """Fetch the clips not on disk yet, one every `pace` seconds (the shop's
        line is shared with the cashier). Stops on `stop`, after `budget`
        requests (0 = all), or at the first failure (offline: try again
        later). A text the server gave no voice for isn't asked for again for
        REFUSED_RETRY_S — it used to be re-requested every round, forever."""
        done = asked = 0
        for text in self.missing_now(texts):
            if stop.is_set() or (budget and asked >= budget):
                break
            asked += 1
            try:
                wav = fetch(text)
            except Exception as exc:     # offline, 401, a TTS hiccup
                if log:
                    log(f"[VOICE] clip fetch paused: {type(exc).__name__}")
                break
            if wav and wavs.parse(wav):
                self.put(text, wav)
                done += 1
            else:
                self._refused[text] = time.monotonic()
            stop.wait(pace)
        return done

    def coverage(self, texts: Iterable[str]) -> tuple:
        texts = [t for t in dict.fromkeys(texts) if t]
        have = sum(1 for t in texts if self.has(t))
        return have, len(texts)


class ReplyVoices(VoiceBank):
    """The server's replies as they were voiced, the ones said lately.

    🐛 [FIX]: from the shop's log — «voice lost»: the reply's words came, its voice (78–123 KB) never
    did on a bad line, and the robot was silent for 55 s («الرقم ده مش صح…», said three times that
    morning, downloaded three times). A reply said before is said from here now, and the live socket
    is told which ones are here, so it sends their words alone (robot/consumers.py)."""

    def __init__(self, folder: Path, most: int = REPLIES_MOST):
        super().__init__(folder)
        self.most = most

    @staticmethod
    def worth(text: str) -> bool:
        return bool(text) and len(text) <= REPLY_LONGEST

    def put(self, text: str, wav: bytes) -> None:
        if not self.worth(text):
            return
        super().put(text, wav)
        self._prune()

    def get(self, text: str) -> Optional[bytes]:
        """The reply's voice, or None; said lately, it's kept longer."""
        if not self.worth(text):
            return None
        path = self.path(text)
        try:
            data = path.read_bytes()
            os.utime(path)
        except OSError:
            return None
        return data if wavs.parse(data) else None

    def keys(self) -> list:
        """The replies here (key()), said latest first."""
        return [p.stem for p in self._files()[:self.most]]

    def _files(self) -> list:
        try:
            files = [(p.stat().st_mtime, p) for p in self.folder.glob("*.wav")]
        except OSError:
            return []
        return [p for _when, p in sorted(files, key=lambda item: item[0], reverse=True)]

    def _prune(self) -> None:
        for path in self._files()[self.most:]:
            try:
                path.unlink()
            except OSError:
                pass
