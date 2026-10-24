"""«يا موس» heard by a model made for it — the way «Alexa» and «Hey Siri» are.

Whisper and Vosk write what they think was said; Vosk has no «موس» in its
words at all («يمس», «انظر إلى كابوس»…), and Whisper takes 16 s a sentence
on the shop's Celeron. This only answers one question — was the name said? —
in a fraction of a second, on any CPU, numpy only, no internet, no Google.

Two parts, as in openWakeWord (github.com/dscripka/openWakeWord, Apache 2.0;
see models/NOTICE):

  1. Fixed, shipped with the brain (models/wake_features.npz): sound →
     log-mel frames → one 96-number «fingerprint» of each 0.76 s of speech,
     from a network trained on a great deal of real speech. Run here in
     plain numpy (no onnxruntime: the Celeron has no AVX at all).
  2. Learned on the shop's own computer (home/wake_model.npz, `brain wake`):
     a small logistic model over 16 fingerprints (≈2 s) — «يا موس» in the
     voices that will call it, against the same voices saying anything else
     and the shop's own noise.
"""

from pathlib import Path
from typing import Optional

import numpy as np

FEATURES = Path(__file__).resolve().parent / "models" / "wake_features.npz"
HOP, NFFT = 160, 512                        # mel frames: every 10 ms, 32 ms long
WINDOW, STEP = 76, 8                        # a fingerprint: 76 mel frames, every 80 ms
SPAN = 16                                   # fingerprints the detector looks at (≈2 s)
CLIP = (SPAN - 1) * STEP * HOP + (WINDOW - 1) * HOP + NFFT   # samples in one look: 31712


class Features:
    """openWakeWord's melspectrogram + embedding models, in numpy."""

    def __init__(self, path=FEATURES):
        with np.load(path) as z:
            self.real, self.imag, self.mel_w = z["stft_real"], z["stft_imag"], z["mel_w"]
            self.plan = [tuple(int(v) for v in row) for row in z["plan"]]
            self.convs = {k: (z[f"w{k}"], z[f"b{k}"]) for k in range(sum(p[0] == 0 for p in self.plan))}

    def melspec(self, samples: np.ndarray) -> np.ndarray:
        """int16 16 kHz → (frames, 32), as openWakeWord feeds its embedding
        model (int16 values as floats; 10·log10; 80 dB below the loudest;
        then /10 + 2)."""
        x = np.asarray(samples, dtype=np.float32)
        if len(x) < NFFT:
            x = np.concatenate([x, np.zeros(NFFT - len(x), np.float32)])
        n = (len(x) - NFFT) // HOP + 1
        frames = np.lib.stride_tricks.as_strided(
            x, shape=(n, NFFT), strides=(x.strides[0] * HOP, x.strides[0]))
        power = (frames @ self.real) ** 2 + (frames @ self.imag) ** 2
        db = 10.0 * np.log10(np.maximum(power @ self.mel_w, 1e-10))
        db = np.maximum(db, db.max() - 80.0)
        return (db / 10.0 + 2.0).astype(np.float32)

    def embed(self, mel: np.ndarray) -> np.ndarray:
        """(frames, 32) → (fingerprints, 96): one per 76 frames, every 8."""
        starts = range(0, len(mel) - WINDOW + 1, STEP)
        if not len(starts):
            return np.zeros((0, 96), np.float32)
        x = np.stack([mel[s:s + WINDOW] for s in starts])[:, :, :, None]   # (N, 76, 32, 1)
        for op, a, b, c, d, e in self.plan:
            if op == 0:                                  # conv: a = which, b..e = pads t, l, b, r
                x = _conv(x, *self.convs[a], (b, c, d, e))
            elif op == 1:
                x = np.where(x > 0, x, 0.2 * x)          # LeakyReLU(0.2)
            elif op == 2:
                x = np.maximum(x, -0.4)
            else:                                        # max pool a × b, stride a × b
                n, h, w, ch = x.shape
                h, w = h // a * a, w // b * b
                x = x[:, :h, :w, :].reshape(n, h // a, a, w // b, b, ch).max(axis=(2, 4))
        return x.reshape(len(x), -1).astype(np.float32)    # (N, 96)

    def fingerprints(self, samples: np.ndarray) -> np.ndarray:
        return self.embed(self.melspec(samples))


def _conv(x: np.ndarray, w: np.ndarray, bias: np.ndarray, pads) -> np.ndarray:
    """2-D convolution, stride 1, channels last: (n, h, w, c) → (n, ho, wo, o);
    pads = (top, left, bottom, right). One matrix product (im2col): the
    Celeron's BLAS does the rest."""
    top, left, bottom, right = pads
    if any(pads):
        x = np.pad(x, ((0, 0), (top, bottom), (left, right), (0, 0)))
    n, h, wd, c = x.shape
    o, _, kh, kw = w.shape
    ho, wo = h - kh + 1, wd - kw + 1
    cols = np.concatenate([x[:, i:i + ho, j:j + wo, :] for i in range(kh) for j in range(kw)], axis=3)
    kernel = w.transpose(2, 3, 1, 0).reshape(kh * kw * c, o)      # rows in the same (i, j, c) order
    return (cols.reshape(-1, kh * kw * c) @ kernel + bias).reshape(n, ho, wo, o)


def windows(prints: np.ndarray) -> np.ndarray:
    """Every SPAN consecutive fingerprints, flattened: (N, SPAN·96)."""
    if len(prints) < SPAN:
        return np.zeros((0, SPAN * prints.shape[1] if prints.ndim == 2 else SPAN * 96), np.float32)
    return np.stack([prints[i:i + SPAN].ravel() for i in range(len(prints) - SPAN + 1)])


START_BY = 1.8                              # s: the name is said first («يا موس، عندك …»)


class Detector:
    """The learned part: was «يا موس» said, at the start of this sentence?
    A small network (SPAN·96 → 32 → 1) over 16 fingerprints."""

    def __init__(self, params: dict, features: Optional[Features] = None):
        self.p = {k: np.asarray(v, np.float32) for k, v in params.items()}
        self.threshold = float(self.p.pop("threshold", 0.5))
        self.features = features or Features()

    @classmethod
    def load(cls, path) -> Optional["Detector"]:
        try:
            with np.load(path) as z:
                return cls({k: z[k] for k in z.files})
        except (OSError, KeyError, ValueError):
            return None

    def save(self, path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, threshold=np.float32(self.threshold), **self.p)

    def probs(self, flat: np.ndarray) -> np.ndarray:
        p = self.p
        h = np.maximum(((flat - p["mean"]) / p["scale"]) @ p["w1"] + p["b1"], 0.0)
        return 1.0 / (1.0 + np.exp(-np.clip(h @ p["w2"] + p["b2"], -30, 30)))

    def score(self, samples: np.ndarray, lead: float = 1.0):
        """(best probability, seconds into `samples` where the name ended),
        over the looks that end in the sentence's first START_BY seconds.
        The sentence gets `lead` s of silence in front, so a name said first
        thing still has a whole look of its own; a quiet mic is brought up
        first, as it was for learning."""
        from .wavs import louder
        x = louder(np.asarray(samples, np.int16)[: int((START_BY + 0.5) * 16000)])
        pad = np.zeros(int(lead * 16000), np.int16)
        flat = windows(self.features.fingerprints(np.concatenate([pad, x])))
        if not len(flat):
            return 0.0, 0.0
        ends = _window_ends(len(flat) + SPAN - 1) - lead
        p = np.where(ends <= START_BY, self.probs(flat), 0.0)
        best = int(np.argmax(p))
        return float(p[best]), max(0.0, float(ends[best]))

    def heard(self, samples: np.ndarray):
        """(said the name?, probability, seconds where it ended)."""
        p, end = self.score(samples)
        return p >= self.threshold, p, end


# ---- learning it on the shop's computer (`brain wake`)

RATE = 16000
LEAD = 1.0                                  # s of silence put before each sentence (as score() does)
TAIL = 0.15                                 # s of pause the VAD keeps after a sentence
AUGMENT = 12                                # variations of each «يا موس» said


def _lead(samples, lead=LEAD):
    return np.concatenate([np.zeros(int(lead * RATE), np.int16), np.asarray(samples, np.int16)])


def _window_ends(n_prints: int) -> np.ndarray:
    """Where each look (SPAN fingerprints) ends, in seconds of its input."""
    k = np.arange(max(0, n_prints - SPAN + 1))
    return ((k + SPAN - 1) * STEP * HOP + (WINDOW - 1) * HOP + NFFT) / RATE


def _vary(said, talk, room, rng):
    """One «يا موس» as it might come another time: faster or slower, louder
    or softer, the shop's noise under it, and more words right after."""
    from .wavs import louder, resample
    x = np.asarray(said, np.float32)
    speed = rng.choice([0.9, 0.95, 1.0, 1.05, 1.1])
    if speed != 1.0:
        x = resample(x, RATE, int(RATE / speed))
    x = x * float(np.exp(rng.uniform(np.log(0.5), np.log(2.0))))
    end = len(x) - int(TAIL * RATE / speed)           # where the name ended
    after = int(rng.uniform(0, 0.5) * RATE)
    if after and len(talk) > after and rng.random() < 0.7:
        start = rng.integers(0, len(talk) - after)
        x = np.concatenate([x[:end], np.asarray(talk[start:start + after], np.float32)])
    if len(room) > len(x) and rng.random() < 0.8:
        start = rng.integers(0, len(room) - len(x))
        noise = np.asarray(room[start:start + len(x)], np.float32)
        x = x + noise * float(rng.uniform(0.3, 1.5))
    x = np.clip(x, -32768, 32767).astype(np.int16)
    return louder(x), end / RATE


def _chunks(audio, rng, n, shortest=1.0, longest=3.0):
    """`n` random stretches of `audio`, as sentences the VAD would cut."""
    audio = np.asarray(audio, np.int16)
    out = []
    for _ in range(n):
        size = int(rng.uniform(shortest, longest) * RATE)
        if len(audio) <= size:
            out.append(audio)
            continue
        start = rng.integers(0, len(audio) - size)
        out.append(audio[start:start + size])
    return out


def _sets(features, said, talk, room, rng, augment=None, calls=()):
    """(positive looks, negative looks, which said each positive came from).
    `calls`: «يا محمد», «يا أحمد»… — others called by name, the closest
    thing to «يا موس» there is: varied as much as the name itself."""
    from .wavs import louder
    augment = AUGMENT if augment is None else augment
    pos, neg, owner = [], [], []
    for one in calls:
        for _ in range(augment // 2):
            x, _end = _vary(one, talk, room, rng)
            neg.extend(windows(features.fingerprints(_lead(x))))
    for i, one in enumerate(said):
        for _ in range(augment):
            x, end = _vary(one, talk, room, rng)
            flat = windows(features.fingerprints(_lead(x)))
            ends = _window_ends(len(flat) + SPAN - 1) - LEAD
            near = (ends >= end - 0.10) & (ends <= end + 0.30)
            pos.extend(flat[near])
            owner.extend([i] * int(near.sum()))
            neg.extend(flat[ends <= end - 0.45])       # only «يا»: not the name yet
    for x in _chunks(talk, rng, 16) + _chunks(room, rng, 8):
        neg.extend(windows(features.fingerprints(_lead(louder(x)))))
    dim = SPAN * 96
    return (np.asarray(pos, np.float32).reshape(-1, dim), np.asarray(neg, np.float32).reshape(-1, dim),
            np.asarray(owner))


def _fit(pos, neg, hidden=32, steps=500, l2=1e-3, seed=0) -> dict:
    """SPAN·96 → 32 (ReLU) → 1, balanced, plain numpy, Adam. A straight line
    (logistic regression) took any «…وس» for the name; this holds up."""
    rng = np.random.default_rng(seed)
    x = np.concatenate([pos, neg])
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))]).astype(np.float32)
    mean, scale = x.mean(axis=0), x.std(axis=0) + 1e-3
    z = ((x - mean) / scale).astype(np.float32)
    weight = np.where(y == 1, 0.5 / max(len(pos), 1), 0.5 / max(len(neg), 1)).astype(np.float32)
    params = {"w1": (rng.standard_normal((z.shape[1], hidden)) / np.sqrt(z.shape[1])).astype(np.float32),
              "b1": np.zeros(hidden, np.float32),
              "w2": (rng.standard_normal(hidden) / np.sqrt(hidden)).astype(np.float32),
              "b2": np.zeros((), np.float32)}
    m = {k: np.zeros_like(v) for k, v in params.items()}
    v = {k: np.zeros_like(v) for k, v in params.items()}
    for t in range(1, steps + 1):
        h = np.maximum(z @ params["w1"] + params["b1"], 0.0)
        prob = 1.0 / (1.0 + np.exp(-np.clip(h @ params["w2"] + params["b2"], -30, 30)))
        err = (prob - y) * weight
        dh = np.outer(err, params["w2"]) * (h > 0)
        grads = {"w1": z.T @ dh + l2 * params["w1"], "b1": dh.sum(axis=0),
                 "w2": h.T @ err + l2 * params["w2"], "b2": np.asarray(err.sum(), np.float32)}
        for k, g in grads.items():
            m[k] = 0.9 * m[k] + 0.1 * g
            v[k] = 0.999 * v[k] + 0.001 * g * g
            params[k] = params[k] - 0.003 * (m[k] / (1 - 0.9 ** t)) / (np.sqrt(v[k] / (1 - 0.999 ** t)) + 1e-8)
    params.update(mean=mean.astype(np.float32), scale=scale.astype(np.float32))
    return params


GOOD_ENOUGH = 0.7                           # of the held-out «يا موس» found — and no false wake
HIGHEST_BAR = 0.8                           # «يا + another name» is turned down by the words (loop.py)


def learn(said, talk, room, names=(), near=(), *, log=print, seed=0, features: Optional[Features] = None):
    """The detector from what was recorded on the shop's computer:

      said   each «يا موس» (int16, as the VAD cut it)
      names  the others in the shop called by name («يا محمد»): without
             them any «يا + a name» woke it
      near   words that sound like it («فانوس», «كابوس», «موز», «ماوس»…)
      talk   the same people saying anything else
      room   the shop, nobody talking to the robot

    A third of each is held out and looked for by a detector that never saw
    it: `report` says how many «يا موس» it found, and how often the near
    words and the talk would have woken it. «يا + another name» isn't
    counted: Vosk writes those right, and loop.py turns them down by the
    words. `report["usable"]`: good enough to use. Returns (Detector, report)."""
    features = features or Features()
    rng = np.random.default_rng(seed)
    said, names, near = list(said), list(names), list(near)
    talk = np.asarray(talk, np.int16)
    log(f"[WAKE] learning from {len(said)} × «يا موس», {len(names)} other names, {len(near)} near words, "
        f"{len(talk) / RATE:.0f} s of talk, {len(room) / RATE:.0f} s of the shop — a few minutes…")
    test = lambda items: [x for i, x in enumerate(items) if i % 3 == 1]         # noqa: E731
    keep = lambda items: [x for i, x in enumerate(items) if i % 3 != 1]         # noqa: E731
    cut = len(talk) * 2 // 3
    pos, neg, _ = _sets(features, keep(said), talk[:cut], room, rng, calls=keep(names) + keep(near))
    trial = Detector(_fit(pos, neg), features)
    found = [trial.score(x)[0] for x in test(said)]
    others = [trial.score(x)[0] for x in test(near) + _chunks(talk[cut:], rng, 12)]
    threshold = float(min(HIGHEST_BAR, max(0.5, max(others, default=0.0) + 0.05)))
    log("[WAKE] now with all of it…")
    pos2, neg2, _ = _sets(features, test(said), talk[cut:], room, rng, calls=test(names) + test(near))
    final = Detector(_fit(np.concatenate([pos, pos2]), np.concatenate([neg, neg2])), features)
    final.threshold = threshold
    hit = int(sum(p >= threshold for p in found))
    false = int(sum(p >= threshold for p in others))
    report = {
        "held_out": len(found), "found": hit, "others": len(others), "false": false,
        "threshold": round(threshold, 2),
        "usable": bool(found) and hit >= GOOD_ENOUGH * len(found) and false == 0,
    }
    return final, report
