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


def _forward(p: dict, flat: np.ndarray) -> np.ndarray:
    """The small network: (N, SPAN·96) → (N, classes) probabilities."""
    h = np.maximum(((flat - p["mean"]) / p["scale"]) @ p["w1"] + p["b1"], 0.0)
    z = h @ p["w2"] + p["b2"]
    z = np.exp(z - z.max(axis=1, keepdims=True))
    return z / z.sum(axis=1, keepdims=True)


class _Model:
    def __init__(self, params: dict, features: Optional[Features] = None):
        self.p = {k: np.asarray(v, np.float32) for k, v in params.items()}
        self.threshold = float(self.p.pop("threshold", 0.5))
        self.features = features or Features()

    @classmethod
    def load(cls, path):
        try:
            with np.load(path) as z:
                return cls({k: z[k] for k in z.files})
        except (OSError, KeyError, ValueError):
            return None

    def save(self, path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, threshold=np.float32(self.threshold), **self.p)

    def _looks(self, samples, upto=None, lead: float = 1.0):
        """(flattened looks, where each ends in s) of a sentence: `lead` s of
        silence in front (a name said first thing still has a whole look),
        a quiet mic brought up first, as it was for learning."""
        from .wavs import louder
        x = np.asarray(samples, np.int16)
        if upto is not None:
            x = x[: int(upto * RATE)]
        flat = windows(self.features.fingerprints(np.concatenate([np.zeros(int(lead * RATE), np.int16),
                                                                   louder(x)])))
        return flat, _window_ends(len(flat) + SPAN - 1) - lead


class Detector(_Model):
    """Was «يا موس» said, at the start of this sentence? A small network
    (SPAN·96 → 32 → 2) over 16 fingerprints."""

    def probs(self, flat: np.ndarray) -> np.ndarray:
        return _forward(self.p, flat)[:, 1]

    def score(self, samples: np.ndarray, lead: float = 1.0):
        """(best probability, seconds into `samples` where the name ended),
        over the looks that end in the sentence's first START_BY seconds."""
        flat, ends = self._looks(samples, START_BY + 0.5, lead)
        if not len(flat):
            return 0.0, 0.0
        p = np.where(ends <= START_BY, self.probs(flat), 0.0)
        best = int(np.argmax(p))
        return float(p[best]), max(0.0, float(ends[best]))

    def heard(self, samples: np.ndarray):
        """(said the name?, probability, seconds where it ended)."""
        p, end = self.score(samples)
        return p >= self.threshold, p, end


# The commands it knows by sound, and how each is written for the server
# (robot/assistant.media_command reads these back; who may is still the
# server's call — the face and the role).
COMMANDS = {
    "quran": "شغل القرآن",
    "calm": "شغل مزيكا هادية",
    "stop": "وقف الصوت",                    # a bare «اسكت» may be to anyone: the server wants what to stop
    "louder": "علي الصوت",
    "quieter": "وطي الصوت",
}
KINDS = list(COMMANDS)                      # class 1.. in this order; 0 is «none of them»
# A command is listened for only when the words have what it's about (Vosk got
# «الصوت» and mangled «وطّي»), a word that's only ever a command, or nothing at
# all: «يا موس صباح الخير» is never turned into one — nor «شغل العربية» (the
# car) into «شغل القرآن»: «شغّل», «وقف» alone are everyday words.
HINTS = ("صوت", "قران", "قرءان", "قرآن", "مزيكا", "موسيقي", "موسيقا", "اغاني", "هادي", "وطي")


def sounds_like_a_command(words: str) -> bool:
    """Empty, or a piece of a command in it (already normalized words)."""
    return not words.strip() or any(h in words for h in HINTS)


class Commands(_Model):
    """Which command was said after the name, by its sound — Vosk writes
    Egyptian commands wrong («وطّي» isn't one of its words either)."""

    def heard(self, samples: np.ndarray, after: float = 0.0):
        """(kind or None, probability) over the looks that end at least
        0.3 s after `after` (where the name ended)."""
        flat, ends = self._looks(samples, 8.0)
        keep = ends >= after + 0.3
        if not keep.any():
            return None, 0.0
        probs = _forward(self.p, flat[keep])[:, 1:]
        k = np.unravel_index(int(np.argmax(probs)), probs.shape)[1]
        best = float(probs[:, k].max())
        return (KINDS[k] if best >= self.threshold else None), best


# ---- learning it on the shop's computer (`brain wake`)

RATE = 16000
LEAD = 1.0                                  # s of silence put before each sentence (as score() does)
TAIL = 0.15                                 # s of pause the VAD keeps after a sentence
LEAD_IN = 0.3                               # s the VAD keeps from before it
AUGMENT = 8                                 # variations of each «يا موس» said in the shop
GOOD_ENOUGH = 0.7                           # of the held-out «يا موس» found — and 1% false wakes at most
HIGHEST_BAR = 0.8                           # «يا + another name» is turned down by the words (loop.py)


def _lead(samples, lead=LEAD):
    return np.concatenate([np.zeros(int(lead * RATE), np.int16), np.asarray(samples, np.int16)])


def _window_ends(n_prints: int) -> np.ndarray:
    """Where each look (SPAN fingerprints) ends, in seconds of its input."""
    k = np.arange(max(0, n_prints - SPAN + 1))
    return ((k + SPAN - 1) * STEP * HOP + (WINDOW - 1) * HOP + NFFT) / RATE


def _vary(said, talk, room, rng):
    """One clip as it might come another time: faster or slower, louder or
    softer, the shop's noise under it, and more words right after."""
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


def _joined(said, command, rng):
    """«يا موس» then the command, as one sentence: the pause between them
    0–0.25 s, not the VAD's whole tail and lead-in (0.45 s) — the command
    comes right after the name when it's said."""
    said = np.asarray(said, np.int16)[: max(0, len(said) - int(TAIL * RATE))]
    command = np.asarray(command, np.int16)[int(LEAD_IN * RATE):]
    gap = (rng.standard_normal(int(rng.uniform(0.0, 0.25) * RATE)) * 40).astype(np.int16)
    return np.concatenate([said, gap, command])


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


class PrintCache:
    """Fingerprints of clips that are learned from as they are (the voices
    made once, the shop's talk): kept on disk, so the next `wake` doesn't
    spend the Celeron's minutes on them again."""

    def __init__(self, path=None):
        self.path, self.store, self.dirty = path, {}, False
        if path is not None and Path(path).exists():
            try:
                with np.load(path) as z:
                    self.store = {k: z[k] for k in z.files}
            except (OSError, ValueError):
                self.store = {}

    def prints(self, features: Features, clip) -> np.ndarray:
        import hashlib
        from .wavs import louder
        clip = np.asarray(clip, np.int16)
        key = hashlib.sha1(clip.tobytes()).hexdigest()
        if key not in self.store:
            self.store[key] = features.fingerprints(_lead(louder(clip))).astype(np.float16)
            self.dirty = True
        return self.store[key].astype(np.float32)

    def save(self) -> None:
        if self.path is not None and self.dirty:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            np.savez(self.path, **self.store)
            self.dirty = False


class _Sets:
    """Looks (flattened windows) by label: 0 none, 1 the name, then the commands."""

    def __init__(self):
        self.wake = {0: [], 1: []}
        self.cmd = {k: [] for k in range(len(KINDS) + 1)}

    def add(self, flat, ends, end=None, wake=0, cmd=0, thin=1):
        """wake: 1 — the name ends at `end`; 0 — none of it is the name;
        None — not for the name's model at all (a command said after the
        name). cmd: k — command k ends at `end`; 0 — no command in it.
        Looks ending near `end` are the word; the earlier ones only part of
        it, so not it."""
        near = ((ends >= end - 0.10) & (ends <= end + 0.30)) if end is not None else np.zeros(len(flat), bool)
        early = (ends <= end - 0.45) if end is not None else np.ones(len(flat), bool)
        if wake == 1:
            self.wake[1].extend(flat[near])
            self.wake[0].extend(flat[early])
        elif wake == 0:
            self.wake[0].extend(flat[::thin])
        if cmd:
            self.cmd[cmd].extend(flat[near])
            self.cmd[0].extend(flat[early])
        elif cmd == 0:
            self.cmd[0].extend(flat[::max(thin, 2)])

    @staticmethod
    def arrays(groups: dict):
        dim = SPAN * 96
        return [np.asarray(groups[k], np.float32).reshape(-1, dim) for k in sorted(groups)]


def _fit(groups, hidden=32, steps=500, l2=1e-3, seed=0) -> dict:
    """SPAN·96 → 32 (ReLU) → classes (softmax), each class weighed alike,
    plain numpy, Adam. A straight line (logistic regression) took any «…وس»
    for the name; this holds up."""
    rng = np.random.default_rng(seed)
    groups = [g for g in groups]
    x = np.concatenate(groups)
    y = np.concatenate([np.full(len(g), k) for k, g in enumerate(groups)])
    k_classes = len(groups)
    mean, scale = x.mean(axis=0), x.std(axis=0) + 1e-3
    z = ((x - mean) / scale).astype(np.float32)
    weight = np.array([1.0 / (k_classes * max(len(g), 1)) for g in groups], np.float32)[y]
    onehot = np.eye(k_classes, dtype=np.float32)[y]
    params = {"w1": (rng.standard_normal((z.shape[1], hidden)) / np.sqrt(z.shape[1])).astype(np.float32),
              "b1": np.zeros(hidden, np.float32),
              "w2": (rng.standard_normal((hidden, k_classes)) / np.sqrt(hidden)).astype(np.float32),
              "b2": np.zeros(k_classes, np.float32)}
    m = {k: np.zeros_like(v) for k, v in params.items()}
    v = {k: np.zeros_like(v) for k, v in params.items()}
    for t in range(1, steps + 1):
        h = np.maximum(z @ params["w1"] + params["b1"], 0.0)
        logits = h @ params["w2"] + params["b2"]
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        prob = e / e.sum(axis=1, keepdims=True)
        err = (prob - onehot) * weight[:, None]
        dh = (err @ params["w2"].T) * (h > 0)
        grads = {"w1": z.T @ dh + l2 * params["w1"], "b1": dh.sum(axis=0),
                 "w2": h.T @ err + l2 * params["w2"], "b2": err.sum(axis=0)}
        for k, g in grads.items():
            m[k] = 0.9 * m[k] + 0.1 * g
            v[k] = 0.999 * v[k] + 0.001 * g * g
            params[k] = params[k] - 0.003 * (m[k] / (1 - 0.9 ** t)) / (np.sqrt(v[k] / (1 - 0.999 ** t)) + 1e-8)
    params.update(mean=mean.astype(np.float32), scale=scale.astype(np.float32))
    return params


def _held(items, every=3):
    """(learned from, held out): every `every`-th held out."""
    return ([x for i, x in enumerate(items) if i % every != 1], [x for i, x in enumerate(items) if i % every == 1])


def learn(own: dict, voices: Optional[dict] = None, *, log=print, seed=0, cache: Optional[PrintCache] = None,
          features: Optional[Features] = None):
    """The name's detector and the commands', from what was recorded in the
    shop (`own`) and what many voices said (`voices`, voices.py):

      said     «يا موس»                  names   «يا محمد», «يا أحمد»…
      near     «فانوس», «كابوس», «فلوس»…  talk    anything else
      room     (own only) the shop, nobody talking to the robot
      quran, calm, stop, louder, quieter     the commands (COMMANDS)

    `own` holds clips (talk and room: one long recording each); `voices`
    holds (voice, clip) pairs. A third of the shop's recordings and every
    fourth voice are held out, and looked for by models that never heard
    them: `report` says how many «يا موس» were found and how often anything
    else would have woken it («يا + another name» isn't counted: Vosk
    writes those right, and loop.py turns them down by the words).
    Returns (Detector, Commands or None, report)."""
    features = features or Features()
    cache = cache or PrintCache()
    rng = np.random.default_rng(seed)
    voices = voices or {}
    talk = np.asarray(own.get("talk", np.zeros(0)), np.int16)
    room = np.asarray(own.get("room", np.zeros(0)), np.int16)
    cut = len(talk) * 2 // 3
    every_voice = sorted({v for items in voices.values() for v, _ in items})
    held_voices = set(every_voice[1::4])
    log(f"[WAKE] learning: {len(own.get('said', []))} × «يا موس» from the shop, {len(every_voice)} voices "
        f"({len(held_voices)} kept back for the test) — a few minutes, longer the first time…")

    def from_voices(kind, test):
        return [c for v, c in voices.get(kind, []) if (v in held_voices) == test]

    def build(test: bool, sets: Optional[_Sets] = None) -> _Sets:
        """The learning sets — of the held-back part (`test`) or the rest."""
        sets = sets or _Sets()
        own_said, own_said_t = _held(own.get("said", []))
        own_calls, own_calls_t = _held(list(own.get("names", [])) + list(own.get("near", [])))
        part = slice(cut, None) if test else slice(None, cut)
        for one in (own_said_t if test else own_said):
            for _ in range(AUGMENT):
                x, end = _vary(one, talk[part], room, rng)
                flat = windows(features.fingerprints(_lead(x)))
                sets.add(flat, _window_ends(len(flat) + SPAN - 1) - LEAD, end, wake=1)
        for one in (own_calls_t if test else own_calls):
            for _ in range(AUGMENT // 2):                   # 4
                x, _end = _vary(one, talk[part], room, rng)
                flat = windows(features.fingerprints(_lead(x)))
                sets.add(flat, _window_ends(len(flat) + SPAN - 1) - LEAD, None, wake=0)
        for x in _chunks(talk[part], rng, 8) + _chunks(room, rng, 4):
            flat = windows(cache.prints(features, x))
            sets.add(flat, _window_ends(len(flat) + SPAN - 1) - LEAD, None, wake=0)
        for one in from_voices("said", test):
            for x, end in [(one, (len(one) / RATE) - TAIL), _vary(one, talk, room, rng)]:
                flat = windows(cache.prints(features, x) if x is one else features.fingerprints(_lead(x)))
                sets.add(flat, _window_ends(len(flat) + SPAN - 1) - LEAD, end, wake=1)
        for kind in ("names", "near", "talk"):
            for one in from_voices(kind, test):
                flat = windows(cache.prints(features, one))
                sets.add(flat, _window_ends(len(flat) + SPAN - 1) - LEAD, None, wake=0, thin=2)
        said_by_voice = {}
        for v, c in voices.get("said", []):
            said_by_voice.setdefault(v, c)
        for k, kind in enumerate(KINDS, start=1):
            for v, one in voices.get(kind, []):
                if (v in held_voices) != test:
                    continue
                said = said_by_voice.get(v)
                named = [_joined(said, one, rng) for _ in range(2)] if said is not None else []
                for x, wake in [(one, 0)] + [(n, None) for n in named]:   # alone, and after «يا موس»
                    flat = windows(cache.prints(features, x))
                    ends = _window_ends(len(flat) + SPAN - 1) - LEAD
                    sets.add(flat, ends, len(x) / RATE - TAIL, wake=wake, cmd=k)
        return sets

    train = build(test=False)
    wake_trial = Detector(_fit(_Sets.arrays(train.wake)), features)
    # The test: what it never heard.
    own_said_t = _held(own.get("said", []))[1]
    found = [wake_trial.score(x)[0] for x in own_said_t + from_voices("said", True)]
    others_clips = (_held(list(own.get("near", [])))[1] + _chunks(talk[cut:], rng, 12)
                    + from_voices("near", True) + from_voices("talk", True)
                    + [c for kind in KINDS for c in from_voices(kind, True)])
    others = [wake_trial.score(x)[0] for x in others_clips]
    threshold = float(min(HIGHEST_BAR, max(0.5, max(others, default=0.0) + 0.05)))
    hit = int(sum(p >= threshold for p in found))
    false = int(sum(p >= threshold for p in others))
    report = {"held_out": len(found), "found": hit, "others": len(others), "false": false,
              "threshold": round(threshold, 2), "voices": len(every_voice),
              "usable": bool(found) and hit >= GOOD_ENOUGH * len(found) and false <= 0.01 * len(others)}
    has_commands = any(voices.get(kind) for kind in KINDS)
    cmd_threshold = 0.7
    if has_commands:
        cmd_trial = Commands(_fit(_Sets.arrays(train.cmd)), features)
        cmd_trial.threshold = 0.0                          # every best guess, to set the bar
        quiet = [cmd_trial.heard(x)[1] for x in from_voices("talk", True) + from_voices("names", True)
                 + from_voices("near", True) + from_voices("said", True)]
        cmd_threshold = float(min(0.9, max(0.6, max(quiet, default=0.0) + 0.05)))
        cmd_trial.threshold = cmd_threshold
        right = total = 0
        for kind in KINDS:
            for one in from_voices(kind, True):
                total += 1
                right += cmd_trial.heard(one)[0] == kind
        report.update(commands_held_out=total, commands_right=right,
                      commands_false=int(sum(p >= cmd_threshold for p in quiet)), commands_bar=round(cmd_threshold, 2))
    log("[WAKE] now with all of it…")
    build(test=True, sets=train)
    cache.save()
    final = Detector(_fit(_Sets.arrays(train.wake)), features)
    final.threshold = threshold
    commands = None
    if has_commands:
        commands = Commands(_fit(_Sets.arrays(train.cmd)), features)
        commands.threshold = cmd_threshold
    return final, commands, report
