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


LOUD_PEAK = 20000                    # where a sentence's peaks are brought up to
MOST_GAIN = 20.0                     # no more than this: the room's hiss stays a hiss


def gain_for(samples: np.ndarray, peak: int = LOUD_PEAK, most: float = MOST_GAIN) -> float:
    """How much louder `samples` should be for the ears (1.0: as they are).
    The peak is taken without its loudest 0.1%: a click mustn't set the gain."""
    s = np.asarray(samples, dtype=np.int16)
    if not len(s):
        return 1.0
    top = float(np.percentile(np.abs(s.astype(np.int32)), 99.9))
    if top < 1:
        return 1.0                                   # silence: nothing to bring up
    gain = min(most, peak / top)
    return gain if gain > 1.05 else 1.0              # loud enough already: left as it is


def louder(samples: np.ndarray, peak: int = LOUD_PEAK, most: float = MOST_GAIN) -> np.ndarray:
    """A quiet mic's sentence brought up to speaking level, before the ears.

    🐛 [FIX]: the shop's USB mic gave frames of 253 at most (a voice near a
    mic is 1000+) and Vosk and Whisper both heard nothing at all in it."""
    s = np.asarray(samples, dtype=np.int16)
    gain = gain_for(s, peak, most)
    if gain == 1.0:
        return s
    return np.clip(np.rint(s.astype(np.float32) * gain), -32768, 32767).astype(np.int16)


def ulaw_encode(samples: np.ndarray) -> bytes:
    """int16 samples → µ-law bytes (bit-for-bit the server's and the firmware's)."""
    s = np.asarray(samples, dtype=np.int16).astype(np.int32)
    sign = np.where(s < 0, 0x80, 0)
    v = np.minimum(np.abs(s), 32635) + 0x84
    exp = np.frexp(v.astype(np.float64))[1] - 8          # highest bit − 7, exact
    mant = (v >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant) & 0xFF).astype(np.uint8).tobytes()


def ulaw_wav(samples: np.ndarray, rate: int = RATE) -> bytes:
    """A mono µ-law WAV: half the bytes of 16-bit PCM over the shop's internet
    (the server turns it back into PCM for speech-to-text)."""
    body = ulaw_encode(samples)
    return (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, ULAW, 1, rate, rate, 1, 8)
            + b"data" + struct.pack("<I", len(body)) + body)


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
