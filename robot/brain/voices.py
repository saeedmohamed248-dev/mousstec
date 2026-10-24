"""Many Arabic voices for `brain wake`: «يا موس» learned the way the big
companies learn their wake words — from many people, not one shop.

Microsoft's neural voices (edge-tts — the same that speaks the robot's
answers; no Google): 32 Arabic ones, men and women, Egypt to Morocco to
the Gulf, each line at its own speed and pitch. Made ONCE on this computer
(it needs the internet then) and kept in home/wake/voices/; after that the
robot needs none of it — this is what it learns from, never what it runs on.
"""

import io
import random
import re
from pathlib import Path

import numpy as np

from . import wavs

# What each voice says. said: the name; names/near/talk: what isn't it;
# the rest: the commands kws.COMMANDS knows by their sound.
LINES = {
    "said": ["يا موس", "يا مُوس"],
    "names": ["يا محمد", "يا أحمد", "يا مصطفى", "يا محمود", "يا علي", "يا حسن"],
    "near": ["فانوس", "كابوس", "موز", "ماوس", "فلوس", "دبوس"],
    "talk": ["عندك فلتر زيت", "بكام الطرمبة دي", "الحساب كام", "صباح الخير",
             "العربية فيها صوت", "غير الزيت والفلتر", "تعالى شوف الزبون", "الكهربا قاطعة"],
    "quran": ["شغل القرآن"],
    "calm": ["شغل مزيكا هادية"],
    "stop": ["وقف الصوت", "اقفل الصوت"],
    "louder": ["علي الصوت"],
    "quieter": ["وطي الصوت", "قلل الصوت"],
}

# Arabic voices as of edge-tts 7 — asked for afresh each time, this is only
# what's used if the list can't be had.
ARABIC = [f"ar-{c}-{n}Neural" for c, n in (
    ("AE", "Fatima"), ("AE", "Hamdan"), ("BH", "Ali"), ("BH", "Laila"), ("DZ", "Amina"), ("DZ", "Ismael"),
    ("EG", "Salma"), ("EG", "Shakir"), ("IQ", "Bassel"), ("IQ", "Rana"), ("JO", "Sana"), ("JO", "Taim"),
    ("KW", "Fahed"), ("KW", "Noura"), ("LB", "Layla"), ("LB", "Rami"), ("LY", "Iman"), ("LY", "Omar"),
    ("MA", "Jamal"), ("MA", "Mouna"), ("OM", "Abdullah"), ("OM", "Aysha"), ("QA", "Amal"), ("QA", "Moaz"),
    ("SA", "Hamed"), ("SA", "Zariyah"), ("SY", "Amany"), ("SY", "Laith"), ("TN", "Hedi"), ("TN", "Reem"),
    ("YE", "Maryam"), ("YE", "Saleh"))]
RATES = ["-15%", "-5%", "+5%", "+15%"]
PITCHES = ["-10Hz", "-5Hz", "+0Hz", "+5Hz", "+10Hz"]
WORKERS = 4
LEAD_IN, TAIL = 0.3, 0.15                     # s: what the VAD keeps around a sentence


def folder(home) -> Path:
    return Path(home) / "wake" / "voices"


def _file(home, kind, voice, i) -> Path:
    return folder(home) / kind / f"{voice}__{i}.wav"


def as_said(samples, rate) -> np.ndarray:
    """A voice's clip → 16 kHz int16 as the VAD would have cut it: its own
    silence trimmed, 0.3 s before and 0.15 s after of a quiet room."""
    x = wavs.resample(np.asarray(samples, np.float32), rate, wavs.RATE) if rate != wavs.RATE \
        else np.asarray(samples, np.float32)
    loud = np.where(np.abs(x) > 0.02 * max(1.0, float(np.abs(x).max())))[0]
    if len(loud):
        x = x[max(0, loud[0] - 160): loud[-1] + 160]
    rng = np.random.default_rng(len(x))
    lead, tail = (rng.standard_normal(int(t * wavs.RATE)) * 40 for t in (LEAD_IN, TAIL))
    return np.clip(np.concatenate([lead, x, tail]), -32768, 32767).astype(np.int16)


def decode(mp3: bytes) -> np.ndarray:
    """The voice's mp3 → 16 kHz int16 (soundfile: libsndfile reads mp3; no ffmpeg on the shop's PC)."""
    import soundfile
    data, rate = soundfile.read(io.BytesIO(mp3), dtype="int16", always_2d=True)
    return as_said(data.mean(axis=1), rate)


def _voices(log) -> list:
    import asyncio

    import edge_tts
    try:
        found = asyncio.run(edge_tts.list_voices())
        names = sorted(v["ShortName"] for v in found if str(v.get("Locale", "")).startswith("ar-"))
        if names:
            return names
    except Exception as exc:                       # the list is a nicety: the known ones will do
        log(f"[VOICES] the voice list didn't come ({type(exc).__name__}): using the {len(ARABIC)} known")
    return list(ARABIC)


def _synthesize_all(jobs, log) -> int:
    """jobs: [(path, text, voice, rate, pitch)] → files written; a voice that
    fails is left out (and asked again next time)."""
    import asyncio

    import edge_tts

    done = [0]

    async def one(sem, path, text, voice, rate, pitch):
        async with sem:
            for attempt in range(3):
                try:
                    audio = bytearray()
                    speech = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                                  connect_timeout=10, receive_timeout=20)
                    async for chunk in speech.stream():
                        if chunk["type"] == "audio":
                            audio += chunk["data"]
                    if audio:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(wavs.pcm_wav(decode(bytes(audio))))
                        done[0] += 1
                        if done[0] % max(1, len(jobs) // 10) == 0:
                            log(f"[VOICES]   {done[0]} of {len(jobs)}")
                        return
                except Exception as exc:
                    if attempt == 2:
                        log(f"[VOICES]   {voice} «{text}»: {type(exc).__name__} — left out")
                await asyncio.sleep(1 + attempt * 2)

    async def every():
        sem = asyncio.Semaphore(WORKERS)
        await asyncio.gather(*(one(sem, *job) for job in jobs))

    asyncio.run(every())
    return done[0]


def make(home, log=print, lines=None) -> dict:
    """{kind: [(voice, clip)]} — made the first time (internet), then from disk."""
    lines = lines or LINES
    try:
        import edge_tts  # noqa: F401
        import soundfile  # noqa: F401
        online = True
    except (ImportError, OSError) as exc:
        log(f"[VOICES] can't make voices here ({type(exc).__name__}: {exc}) — learning from the shop's "
            "own recordings only")
        online = False
    voices = _voices(log) if online else sorted({p.name.split("__")[0] for p in folder(home).glob("*/*.wav")})
    jobs = []
    for voice in voices:
        for kind, texts in lines.items():
            for i, text in enumerate(texts):
                path = _file(home, kind, voice, i)
                if not path.exists():
                    pick = random.Random(f"{voice}|{text}")
                    jobs.append((path, text, voice, pick.choice(RATES), pick.choice(PITCHES)))
    if jobs and online:
        log(f"[VOICES] making {len(jobs)} clips in {len(voices)} Arabic voices — once, a few minutes…")
        _synthesize_all(jobs, log)
    out = {kind: [] for kind in lines}
    for kind in lines:
        for path in sorted((folder(home) / kind).glob("*.wav")):
            parsed = wavs.parse(path.read_bytes())
            if parsed is not None and parsed[1] == wavs.RATE and len(parsed[2]):
                out[kind].append((re.sub(r"__\d+$", "", path.stem), parsed[2]))
    return out
