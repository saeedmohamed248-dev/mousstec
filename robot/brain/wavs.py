"""WAV in and out, G.711 µ-law, and sample-rate conversion (numpy only).

The server answers with 16 kHz mono WAV (16-bit PCM, or µ-law when asked);
mics and speakers run at whatever rate their driver likes (44.1/48 kHz).
"""

import struct

import numpy as np

PCM, ULAW = 1, 7                     # WAV format tags
RATE = 16000                         # what Whisper and the server use


def ulaw_decode(data: bytes) -> np.ndarray:
    """µ-law bytes → int16 samples (the same table as the firmware and the server)."""
    u = (~np.frombuffer(data, dtype=np.uint8)).astype(np.int32) & 0xFF
    exp = (u >> 4) & 7
    mant = u & 0x0F
    v = (((mant << 3) + 0x84) << exp) - 0x84
    return np.where(u & 0x80, -v, v).astype(np.int16)


def parse(data: bytes):
    """(format tag, sample rate, int16 mono samples) of a WAV, or None.

    Walks the chunks (a `LIST` before `data` is common) and takes a `data`
    size of 0 or past the end as "the rest" — ffmpeg writing to a pipe can't
    go back and fill it in.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt, pos = None, 12
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if chunk == b"data" and (size == 0 or pos + 8 + size > len(data)):
            size = len(data) - pos - 8
        body = data[pos + 8:pos + 8 + size]
        if chunk == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk == b"data" and fmt:
            tag, channels, rate, _, _, bits = fmt
            if tag == ULAW:
                samples = ulaw_decode(body)
            elif tag == PCM and bits == 16:
                samples = np.frombuffer(body[:len(body) // 2 * 2], dtype="<i2").astype(np.int16)
            else:
                return None
            if channels > 1:
                samples = samples[:len(samples) // channels * channels]
                samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
            return tag, rate, samples
        pos += 8 + size + (size & 1)
    return None


def pcm_wav(samples: np.ndarray, rate: int = RATE) -> bytes:
    """A mono 16-bit PCM WAV around int16 samples."""
    body = np.asarray(samples, dtype="<i2").tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, PCM, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(body)) + body)


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """`x` from `src` Hz to `dst` Hz, as float32.

    48 → 16 kHz (the usual USB mic) averages each 3 samples, which also
    filters what 16 kHz can't hold; other ratios smooth over the step, then
    interpolate. Plenty for speech-to-text and for a voice on a speaker.
    """
    x = np.asarray(x, dtype=np.float32)
    if src == dst or not len(x):
        return x
    if src > dst and src % dst == 0:
        k = src // dst
        return x[:len(x) // k * k].reshape(-1, k).mean(axis=1)
    if src > dst:
        width = max(1, round(src / dst))
        x = np.convolve(x, np.ones(width, dtype=np.float32) / width, mode="same")
    n = max(1, round(len(x) * dst / src))
    return np.interp(np.arange(n) * (src / dst), np.arange(len(x)), x).astype(np.float32)


def rms(frame: np.ndarray) -> int:
    """Level of a frame around its own mean (a mic's DC offset isn't sound)."""
    if not len(frame):
        return 0
    return int(np.std(np.asarray(frame, dtype=np.float64)))
