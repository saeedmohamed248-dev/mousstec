"""The Quran and calm music in the shop, on the speakers the robot talks on.

Sources (brain.ini [media]), tried in order, «|» between them: a folder of
files — played in name order, round and round — then radio streams, each
taking over when the one before it is down. No files on this computer still
means the Quran plays, from the first station that answers.
mp3/m4a/ogg/streams are decoded by PyAV (installed with faster-whisper,
ffmpeg inside); WAV works without it.

Respect for the Quran: the robot never talks over it — it pauses it, says
what it has to, and goes on from where it stopped. Music is only turned down
(ducked) under its voice. The Quran and music never play together: one
replaces the other.
"""

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np

from . import wavs

KINDS = ("quran", "calm")
AUDIO_FILES = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")
RATE = 48000                         # what the output runs at
CHUNK = RATE // 10                   # 100 ms: pause/stop/duck answer within that
DUCK = 0.25                          # music under the robot's voice
STEP = 0.15                          # «علّي / وطّي الصوت»


def _is_url(source) -> bool:
    return str(source).startswith(("http://", "https://"))


def files_in(folder) -> list:
    folder = Path(str(folder or "")).expanduser()
    if not str(folder) or not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_FILES)


def decode(source) -> Iterator[np.ndarray]:
    """int16 mono chunks at RATE from a file path or a stream URL."""
    path = str(source)
    if path.lower().endswith(".wav"):
        parsed = wavs.parse(Path(path).read_bytes())
        if not parsed:
            return
        _tag, rate, samples = parsed
        samples = wavs.resample(samples, rate, RATE).astype(np.int16)
        for i in range(0, len(samples), CHUNK):
            yield samples[i:i + CHUNK]
        return
    import av                        # comes with faster-whisper
    with av.open(path) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                yield out.to_ndarray().reshape(-1).astype(np.int16)


class _SoundDeviceSink:
    def __init__(self, device=None):
        import sounddevice as sd
        self._stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="int16", device=device)
        self._stream.start()

    def write(self, chunk: np.ndarray) -> None:
        self._stream.write(chunk.reshape(-1, 1))

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


class MediaPlayer:
    def __init__(self, sources: dict, *, volume: float = 0.6, device=None,
                 sink_factory: Optional[Callable] = None, decoder: Callable = decode, log=print):
        """`sources`: {"quran": folder-or-url, "calm": …}."""
        self.sources = {k: v for k, v in (sources or {}).items() if v}
        self.volume = max(0.05, min(1.0, float(volume)))
        self._sink_factory = sink_factory or (lambda: _SoundDeviceSink(device))
        self._decode = decoder
        self.log = log
        self.kind = ""                   # what's playing ("" = nothing)
        # How many voices are talking right now (an answer, the day's words).
        # The player asks it on every chunk, so the Quran the schedule starts
        # in the middle of a reply starts held, never under the robot's voice.
        self._talking = 0
        self._stop = threading.Event()
        self._worker = None
        self._lock = threading.Lock()

    # ---- what there is to play
    def playlist(self, kind: str) -> list:
        """The first folder that has files — or else every stream, in order."""
        parts = [p.strip() for p in str(self.sources.get(kind, "")).split("|") if p.strip()]
        for part in parts:
            if not _is_url(part):
                files = files_in(part)
                if files:
                    return files
        return [p for p in parts if _is_url(p)]

    def can_play(self, kind: str) -> bool:
        return bool(self.playlist(kind))

    # ---- commands
    def play(self, kind: str) -> bool:
        """Start `kind` (replacing whatever plays). False when it has no files."""
        items = self.playlist(kind)
        if not items:
            self.log(f"[MEDIA] nothing to play for «{kind}»: set [media] {kind} in brain.ini")
            return False
        self.stop()
        with self._lock:
            self.kind = kind
            self._stop = threading.Event()
            self._worker = threading.Thread(target=self._run, args=(kind, items, self._stop), daemon=True)
            self._worker.start()
        what = "stations" if all(_is_url(i) for i in items) else "files"
        self.log(f"[MEDIA] ▶ {kind} ({len(items)} {what})")
        return True

    def stop(self) -> None:
        with self._lock:
            worker, self._worker = self._worker, None
            self._stop.set()
            was, self.kind = self.kind, ""
        if worker is not None:
            worker.join(timeout=2)
        if was:
            self.log(f"[MEDIA] ■ {was}")

    def louder(self) -> float:
        self.volume = min(1.0, round(self.volume + STEP, 2))
        return self.volume

    def quieter(self) -> float:
        self.volume = max(0.05, round(self.volume - STEP, 2))
        return self.volume

    @contextmanager
    def speaking(self):
        """While the robot talks: the Quran waits where it is, music goes down."""
        with self._lock:
            self._talking += 1
        try:
            yield
        finally:
            with self._lock:
                self._talking -= 1

    # ---- the player
    def _held(self, kind: str) -> bool:
        return kind == "quran" and self._talking > 0

    def _gain(self, kind: str) -> float:
        return self.volume * (DUCK if self._talking > 0 and kind != "quran" else 1.0)

    def _play_item(self, kind: str, item, sink, stop: threading.Event) -> bool:
        """Play one file or stream to its end; True if any sound came out."""
        played = False
        for chunk in self._decode(item):
            played = True
            while self._held(kind) and not stop.is_set():
                stop.wait(0.05)                  # held where it stopped, not skipped
            if stop.is_set():
                return played
            out = np.clip(chunk.astype(np.float32) * self._gain(kind), -32768, 32767)
            sink.write(out.astype(np.int16))
        return played

    def _run(self, kind: str, items: list, stop: threading.Event) -> None:
        sink = None
        streams = all(_is_url(i) for i in items)
        try:
            sink = self._sink_factory()
            while not stop.is_set():
                played = False
                for item in items:
                    try:
                        played |= self._play_item(kind, item, sink, stop)
                    except Exception as exc:     # a dead station, a broken file: the next one
                        self.log(f"[MEDIA] {kind}: {str(item)[:60]} failed ({type(exc).__name__}), trying the next")
                    if stop.is_set():
                        return
                if streams:
                    stop.wait(3 if played else 30)   # reconnect gently; all down: wait, try again
                elif not played:                 # no file had sound in it: don't spin
                    self.log(f"[MEDIA] {kind}: none of the files would play")
                    return
        except Exception as exc:                 # a bad file, a dead stream, no device
            self.log(f"[MEDIA] {kind} stopped: {type(exc).__name__}: {exc}")
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    pass
            with self._lock:
                if self._stop is stop and self.kind == kind:
                    self.kind = ""
