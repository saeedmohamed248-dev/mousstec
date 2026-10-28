"""Cut the mic's endless stream into utterances (who spoke, from when to when).

The same idea as the bridge firmware's VAD: listening starts at a few times
the room's own level (learned while nobody talks), ends after a pause, and
keeps a little from just before the start — the quiet «يا» of «يا موس». On
a computer there's no 1.8 s memory limit: a sentence can run 12 s.
"""

from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .wavs import RATE, rms

FRAME = 512                                  # 32 ms at 16 kHz
FRAME_MS = FRAME * 1000 // RATE
KEEP_TAIL_MS = 150                           # of the pause that ended it


@dataclass
class Utterance:
    samples: np.ndarray                      # int16, 16 kHz
    speech_ms: int                           # without the lead-in and the pause
    quietest: int                            # frame levels while it lasted
    loudest: int
    cut: bool                                # hit the length limit, not a pause

    @property
    def never_quiet(self) -> bool:
        """Words have gaps: not one quiet frame means noise (a fan, a loose
        wire, a radio), not someone talking."""
        return self.quietest > self.loudest * 0.5 and self.speech_ms > 1500


class Segmenter:
    def __init__(self, *, min_start: int = 150, max_start: int = 2500,
                 end_silence_ms: int = 600, preroll_ms: int = 300,
                 min_speech_ms: int = 300, max_ms: int = 12000):
        self.min_start, self.max_start = min_start, max_start
        self.end_silence_ms, self.min_speech_ms, self.max_ms = end_silence_ms, min_speech_ms, max_ms
        self.noise = min_start / 4.0
        self._pre = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._rec = None

    def start_level(self) -> int:
        return int(min(max(self.noise * 3.5, self.min_start), self.max_start))

    def stop_level(self) -> int:
        return self.start_level() * 55 // 100

    def _track(self, level: int) -> None:
        """The room's own level: follows quiet frames down fast and anything
        louder up slowly, on every frame — also while recording. Learned only
        between sentences, a shop louder than the first guess never let a
        sentence end, so the floor was never learned at all."""
        if level < self.noise:
            self.noise = 0.8 * self.noise + 0.2 * level
        else:
            self.noise = 0.995 * self.noise + 0.005 * level

    @property
    def recording(self):
        """The frames of the sentence in progress (from its lead-in on), or
        None between sentences — for ears that hear while it's said."""
        return self._rec

    @property
    def gapped(self) -> bool:
        """The sentence in progress has had a quiet frame: the gaps words have —
        a fan's or a radio's steady sound never does (Utterance.never_quiet)."""
        return self._rec is not None and self._quietest <= self._loudest * 0.5

    @property
    def said_ms(self) -> int:
        """How much has been said of the sentence in progress (its lead-in and
        the pause it's in now left out), or 0 between sentences."""
        if self._rec is None:
            return 0
        return max(0, (len(self._rec) - self._lead) * FRAME_MS - self._silent)

    def reset(self) -> None:
        """Forget what's in progress (the robot just spoke: that was its own voice)."""
        self._rec = None
        self._pre.clear()

    def feed(self, frame: np.ndarray) -> Optional[Utterance]:
        """One FRAME of int16 samples in; a finished Utterance out, now and then."""
        level = rms(frame)
        start = self.start_level()
        self._track(level)
        if self._rec is None:
            if level > start:
                self._lead = len(self._pre)
                self._rec = list(self._pre) + [frame]
                self._pre.clear()
                self._silent, self._quietest, self._loudest = 0, level, level
                return None
            self._pre.append(frame)
            return None

        self._rec.append(frame)
        self._quietest = min(self._quietest, level)
        self._loudest = max(self._loudest, level)
        self._silent = self._silent + FRAME_MS if level < self.stop_level() else 0
        total_ms = len(self._rec) * FRAME_MS
        paused = self._silent >= self.end_silence_ms
        if not paused and total_ms < self.max_ms:
            return None

        frames, lead, silent = self._rec, self._lead, self._silent
        self._rec = None
        if not paused:                   # never paused: that floor is the room
            self.noise = max(self.noise, float(self._quietest))
        speech_ms = total_ms - silent - lead * FRAME_MS
        if speech_ms < self.min_speech_ms:
            return None
        drop = max(0, (silent - KEEP_TAIL_MS) // FRAME_MS)
        if drop:
            frames = frames[:-drop]
        return Utterance(samples=np.concatenate(frames).astype(np.int16), speech_ms=speech_ms,
                         quietest=self._quietest, loudest=self._loudest, cut=not paused)
