"""The USB (or Bluetooth) mic and the speakers, through PortAudio (sounddevice).

Each device is opened at its own rate — a Bluetooth mic or a USB headset
often can't do 16 kHz itself — and converted here, so any mic works.
"""

import queue
from typing import Iterator

import numpy as np

from . import wavs
from .vad import FRAME


def _sd():
    import sounddevice
    return sounddevice


def pick(spec: str, kind: str):
    """'' → the system default; '3' → device 3; 'USB' → the first whose name has it."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    sd = _sd()
    for index, dev in enumerate(sd.query_devices()):
        if spec.lower() in dev["name"].lower() and dev[f"max_{kind}_channels"] > 0:
            return index
    raise SystemExit(f"no {kind} device named like «{spec}» — run: python -m brain devices")


def devices() -> str:
    return str(_sd().query_devices())


class Mic:
    def __init__(self, device: str = ""):
        sd = _sd()
        self.device = pick(device, "input")
        info = sd.query_devices(self.device, "input")
        self.name = info["name"]
        self.rate = int(info["default_samplerate"])
        self._q = queue.Queue(maxsize=600)          # ~20 s: the brain was busy answering
        self._stream = sd.InputStream(device=self.device, channels=1, samplerate=self.rate,
                                      dtype="int16", blocksize=int(self.rate * 0.032),
                                      callback=self._incoming)
        self._carry = np.zeros(0, dtype=np.float32)

    def _incoming(self, indata, frames, time_info, status):
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def __enter__(self):
        self._stream.start()
        return self

    def __exit__(self, *exc):
        self._stream.stop()
        self._stream.close()

    def drain(self) -> None:
        """Drop what was heard meanwhile (the robot's own voice, mostly)."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._carry = np.zeros(0, dtype=np.float32)

    def frames(self) -> Iterator[np.ndarray]:
        """Endless 32 ms int16 frames at 16 kHz."""
        while True:
            block = wavs.resample(self._q.get(), self.rate, wavs.RATE)
            self._carry = np.concatenate([self._carry, block])
            while len(self._carry) >= FRAME:
                frame, self._carry = self._carry[:FRAME], self._carry[FRAME:]
                yield np.clip(frame, -32768, 32767).astype(np.int16)


class Speaker:
    def __init__(self, device: str = "", volume: float = 1.0):
        self.device = pick(device, "output")
        self.volume = volume

    def play(self, samples: np.ndarray, rate: int) -> None:
        sd = _sd()
        x = np.asarray(samples, dtype=np.float32) / 32768.0 * self.volume
        try:
            sd.play(x, rate, device=self.device, blocking=True)
        except sd.PortAudioError:                    # the device won't do 16 kHz
            native = int(sd.query_devices(self.device, "output")["default_samplerate"])
            sd.play(wavs.resample(x, rate, native), native, device=self.device, blocking=True)

    def stop(self) -> None:
        """Cut short what's playing (the hold-on phrase, its answer here)."""
        try:
            _sd().stop()
        except Exception:
            pass

    def play_wav(self, data: bytes) -> float:
        """Play a WAV; its length in seconds (0 when it isn't one)."""
        parsed = wavs.parse(data or b"")
        if not parsed:
            return 0.0
        _tag, rate, samples = parsed
        self.play(samples, rate)
        return len(samples) / rate
