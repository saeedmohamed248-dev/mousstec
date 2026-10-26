"""robot/teachers.py — «🎓 تعلّم السمع»: when «مخ موس» isn't sure what it heard,
a teacher hears the same recording.

The robot's own ears (Vosk) are fast but trained on news Arabic: «فلترا الزي»
for «فلتر زيت». When it isn't sure (its words' confidence, or nothing in the
shop matches what it heard) it sends the recording with its words and
`check=1` to /voice/; here Azure's speech-to-text hears it (ar-EG), then
Google's if Azure can't, and the teacher's words answer the question. Each
one is kept (RobotHeard): the pair teaches the robot (RobotHeardFix,
robot/brain/heardfix.py) and the recording with the right words trains the
shop's own model later.

Gemini is the last teacher, when neither can (no key, their minutes used, down):
it hears with the names of what the branch sells in mind, like Google does.

A whole sentence is learned too (`sentences`): the same words from the robot's
ears and the same from the teacher, twice — the robot then answers it at once,
without a teacher, the net up or down.

Each teacher has a monthly budget of minutes — its free ones by default — and
is never asked past it. The keys stay on the server:
  ROBOT_AZURE_SPEECH_KEY, ROBOT_AZURE_SPEECH_REGION   (e.g. westeurope)
  ROBOT_GOOGLE_STT_KEY                                 (an API key with Speech-to-Text on)
  GEMINI_API_KEY                                       (the AI's own)
  ROBOT_AZURE_FREE_MINUTES (300), ROBOT_GOOGLE_FREE_MINUTES (60),
  ROBOT_GEMINI_TEACH_MINUTES (600: about a dollar of Flash a month)
  ROBOT_HEARD_AUDIO_DAYS (120: the recordings kept, the words for ever)
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from django.core.files.base import ContentFile
from django.db.models import F, Sum
from django.utils import timezone

from . import audio as audio_svc
from . import health, wakename
from .brain import heardfix

logger = logging.getLogger("mouss_tec_core")

LANGUAGE = "ar-EG"
TIMEOUT_S = 6
# 🐛 [FIX]: كل مدرّس كان ليه ٦ ثواني لوحده، فـ Azure واقع ثم Google واقع = ١٢ ثانية قبل ما السيرفر
# يبدأ يفكّر، و«مخ موس» بيستنى ١٥ بس. المدرّسين كلهم في ٦ ثواني: اللي بعده بياخد اللي فاضل.
TEACH_BUDGET_S = 6
TEACHER_FEWEST_S = 1.5     # less left than this: the robot's own words answer, not a hurried guess
# Google's v1 bills a request rounded up to 15 s when it doesn't say: counted so.
GOOGLE_ROUNDING_S = 15
SEEN_TO_USE = 2            # a lesson is used once it was seen this many times
MOST_SECONDS = 30          # a question, not a speech: longer isn't sent
# 🐛 [FIX]: «أي» → Azure (ar-EG) كتب «You know you.» والروبوت رد بالإنجليزي «That's me!». كلمة
# أو اتنين بحروف إنجليزي من غير ولا حرف عربي تخمين مش سماع: الجملة الإنجليزي الحقيقية أطول.
FOREIGN_FEWEST_WORDS = 4
_ARABIC = re.compile(r"[\u0600-\u06FF]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


@dataclass
class Heard:
    text: str
    teacher: str
    confidence: Optional[float] = None
    billed_seconds: int = 0


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


TEACHERS = ("azure", "google", "gemini")
_SERVICE = {"azure": "azure_stt", "google": "google_stt", "gemini": "gemini"}   # robot/health.py
_BUDGETS = {"azure": ("ROBOT_AZURE_FREE_MINUTES", "300"), "google": ("ROBOT_GOOGLE_FREE_MINUTES", "60"),
            "gemini": ("ROBOT_GEMINI_TEACH_MINUTES", "600")}
SENTENCES_FROM_DAYS = 90   # what the ears wrote lately: a new model, a new microphone hear otherwise
SENTENCES_MOST = 2000
SENTENCE_LONGEST = 120     # RobotHeardFix.heard: what «🎓 تعلّم السمع» can turn off


def free_minutes(teacher: str) -> float:
    name, default = _BUDGETS[teacher]
    try:
        return float(_env(name, default))
    except ValueError:
        return 0.0


def configured() -> list:
    """The teachers this server has keys for, in the order they're asked."""
    out = []
    if _env("ROBOT_AZURE_SPEECH_KEY") and _env("ROBOT_AZURE_SPEECH_REGION"):
        out.append("azure")
    if _env("ROBOT_GOOGLE_STT_KEY"):
        out.append("google")
    if audio_svc.gemini_key():
        out.append("gemini")
    return out


def used_seconds(teacher: str, now=None) -> int:
    """What `teacher` billed this month (this shop's robots)."""
    from .models import RobotHeard

    now = now or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (RobotHeard.objects.filter(teacher=teacher, created_at__gte=start)
             .aggregate(s=Sum("billed_seconds"))["s"])
    return int(total or 0)


def minutes_left(teacher: str) -> float:
    return max(0.0, free_minutes(teacher) - used_seconds(teacher) / 60)


def _azure(pcm_wav: bytes, seconds: float, timeout: float = TIMEOUT_S) -> Optional[Heard]:
    import requests

    region, key = _env("ROBOT_AZURE_SPEECH_REGION"), _env("ROBOT_AZURE_SPEECH_KEY")
    resp = requests.post(
        f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1",
        params={"language": LANGUAGE, "format": "detailed"},
        headers={"Ocp-Apim-Subscription-Key": key, "Accept": "application/json",
                 "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000"},
        data=pcm_wav, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    billed = math.ceil(seconds)
    if body.get("RecognitionStatus") != "Success":
        return Heard("", "azure", None, billed)
    best = (body.get("NBest") or [{}])[0]
    text = str(best.get("Display") or body.get("DisplayText") or "")
    return Heard(text, "azure", best.get("Confidence"), billed)


def _google(pcm: bytes, rate: int, seconds: float, hints: list, timeout: float = TIMEOUT_S) -> Optional[Heard]:
    import requests

    config = {"encoding": "LINEAR16", "sampleRateHertz": rate, "languageCode": LANGUAGE,
              "maxAlternatives": 1}
    if hints:                             # the shop's parts: «فلتر زيت» heard as such
        config["speechContexts"] = [{"phrases": hints[:500]}]
    resp = requests.post(
        "https://speech.googleapis.com/v1/speech:recognize", params={"key": _env("ROBOT_GOOGLE_STT_KEY")},
        json={"config": config, "audio": {"content": base64.b64encode(pcm).decode()}}, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    billed = re.match(r"(\d+)", str(body.get("totalBilledTime") or ""))
    billed = int(billed.group(1)) if billed else GOOGLE_ROUNDING_S * math.ceil(seconds / GOOGLE_ROUNDING_S)
    alternatives = [(r.get("alternatives") or [{}])[0] for r in body.get("results") or []]
    text = " ".join(str(a.get("transcript") or "").strip() for a in alternatives).strip()
    confidence = alternatives[0].get("confidence") if alternatives else None
    return Heard(text, "google", confidence, billed)


def _gemini(pcm_wav: bytes, seconds: float, hints: list, name: str, timeout: float = TIMEOUT_S) -> Optional[Heard]:
    """Gemini hears it (the last teacher), the branch's part names in mind."""
    try:
        text = audio_svc.transcribe(pcm_wav, name=name, strict=True, hints=hints, budget=timeout)
    except audio_svc.SttUnavailable as exc:
        raise RuntimeError(str(exc)) from exc
    return Heard(text, "gemini", None, math.ceil(seconds))


def usable(text: str) -> bool:
    """Whether what a teacher wrote can stand for the question: Arabic, or a
    real English sentence — not a few English words guessed at a short Arabic one."""
    if _ARABIC.search(text or ""):
        return True
    return len(_LATIN_WORD.findall(text or "")) >= FOREIGN_FEWEST_WORDS


def hints_for(device) -> list:
    """The names of what this branch has: what a shop's customers ask for.
    Only ever a help to the ears: [] rather than in their way."""
    from inventory.models import Inventory

    branch = getattr(device, "branch", None)
    if branch is None:
        return []
    try:
        names = list(Inventory.objects.filter(branch=branch, quantity__gt=0)
                     .order_by("-quantity").values_list("product__name", flat=True)[:500])
    except Exception as exc:             # the ears go on without them
        logger.warning("robot ears: no part names for the hint: %s", exc)
        return []
    return list(dict.fromkeys(n[:100] for n in names if n))


def _without_name(device, text: str) -> str:
    """The teacher's words without «يا موس» — however it wrote the name."""
    addressed, rest = wakename.strip_name(device, text)
    text = rest if addressed else re.sub(r"^\s*يا\s+\S+\s*[،,]?\s*", "", text or "").strip()
    # 🐛 [FIX]: «شكرا ليك يا موسى.» — Azure بيكتب الاسم «موسى» في آخر الجملة كمان، فالشكر مكانش
    # بيتفهم شكر وراح للذكاء الاصطناعي. «يا + الاسم» في الآخر بيتشال زي اللي في الأول.
    base = wakename.normalize(getattr(device, "wake_name", "") or "موس")
    names = set(wakename.names_for(device)) | set(wakename._OPENING_ALIASES.get(base, []))
    words = text.split()
    if (len(words) >= 2 and wakename.normalize(words[-2]) in wakename._VOCATIVES
            and wakename.normalize(words[-1]) in names):
        text = " ".join(words[:-2]).strip(" ،,.!؟?:-")
    return text


def check(device, wav: bytes, heard: str, confidence: Optional[float] = None) -> Optional[Heard]:
    """What a teacher heard in `wav` (a recording «مخ موس» wasn't sure of —
    it heard `heard`, without the name), or None: no teacher set up, the
    month's minutes used, or none could hear it. Kept with the recording, and
    what it teaches is learned."""
    from .models import RobotHeard

    pcm_wav = audio_svc.as_pcm_wav(wav)
    parts = audio_svc.wav_parts(pcm_wav)
    if not parts:
        return None
    _tag, rate, pcm = parts
    seconds = len(pcm) / 2 / (rate or 16000)
    if seconds <= 0 or seconds > MOST_SECONDS:
        return None
    hints = None
    started = time.monotonic()
    for teacher in configured():
        if minutes_left(teacher) * 60 < seconds + (GOOGLE_ROUNDING_S if teacher == "google" else 1):
            continue
        left = TEACH_BUDGET_S - (time.monotonic() - started)
        if left < TEACHER_FEWEST_S:
            break
        try:
            if teacher == "azure":
                got = _azure(pcm_wav, seconds, timeout=left)
            elif teacher == "google":
                hints = hints_for(device) if hints is None else hints
                got = _google(pcm, rate, seconds, hints, timeout=left)
            else:
                hints = hints_for(device) if hints is None else hints
                got = _gemini(pcm_wav, seconds, hints, getattr(device, "wake_name", "") or "موس", timeout=left)
        except Exception as exc:          # down, a wrong key, its quota: the next one
            logger.warning("robot teacher %s failed: %s", teacher, health.scrub(exc))   # Google's key in its URL
            if teacher != "gemini":           # Gemini notes its own (robot/audio.gemini_generate)
                health.note(_SERVICE[teacher], False, code=health.code_of(exc), detail=str(exc))
            continue
        if teacher != "gemini":
            health.note(_SERVICE[teacher], True)
        if got is None:
            continue
        got.text = _without_name(device, got.text)
        row = RobotHeard(device=device, local_text=(heard or "")[:255], local_confidence=confidence,
                         teacher=teacher, teacher_text=got.text[:255], teacher_confidence=got.confidence,
                         seconds=round(seconds, 2), billed_seconds=got.billed_seconds)
        row.audio.save(f"{device.pk}-{timezone.now():%Y%m%d%H%M%S%f}.wav", ContentFile(wav), save=False)
        row.save()
        if got.text and not usable(got.text):
            return None                   # kept on the page, but what the ears heard stands
        if got.text:
            learn(heard, got.text)
            return got
    return None


def hear(device, wav: bytes) -> str:
    """The robot's ears when its computer has none fast enough («a test clip
    took 14 s here: the server listens»): Azure hears the whole recording,
    the name too, within its minutes — '' when it can't (Gemini then).

    🐛 [FIX]: the server heard every sentence with Gemini only: on the free
    quota it answered 429 (and the retired gemini-2.5-flash 404) and the
    robot went deaf — 16–18 s and «the server can't turn speech into text»."""
    from .models import RobotHeard

    if "azure" not in configured():
        return ""
    pcm_wav = audio_svc.as_pcm_wav(wav)
    parts = audio_svc.wav_parts(pcm_wav)
    if not parts:
        return ""
    _tag, rate, pcm = parts
    seconds = len(pcm) / 2 / (rate or 16000)
    if seconds <= 0 or seconds > MOST_SECONDS or minutes_left("azure") * 60 < seconds + 1:
        return ""
    try:
        got = _azure(pcm_wav, seconds)
    except Exception as exc:              # down, a wrong key, its quota: Gemini's turn
        logger.warning("robot ears: azure failed: %s", exc)
        health.note("azure_stt", False, code=health.code_of(exc), detail=str(exc))
        return ""
    health.note("azure_stt", True)
    if got is None:
        return ""
    RobotHeard.objects.create(device=device, local_text="", teacher="azure", teacher_text=got.text[:255],
                              teacher_confidence=got.confidence, seconds=round(seconds, 2),
                              billed_seconds=got.billed_seconds)
    return got.text if usable(got.text) else ""


AUDIO_DAYS = 120           # ROBOT_HEARD_AUDIO_DAYS: the recordings kept this long (0: for ever)


def prune_audio(now=None) -> int:
    """Recordings older than ROBOT_HEARD_AUDIO_DAYS let go, their words kept
    (the lessons, the minutes, the page). 🐛 [FIX]: with [robot] teach = yes
    every question's recording was kept for ever — a busy branch fills the
    server's disk in a year or two."""
    from datetime import timedelta

    from .models import RobotHeard

    try:
        days = int(_env("ROBOT_HEARD_AUDIO_DAYS", str(AUDIO_DAYS)))
    except ValueError:
        days = AUDIO_DAYS
    if days <= 0:
        return 0
    cutoff = (now or timezone.now()) - timedelta(days=days)
    gone = 0
    for row in RobotHeard.objects.filter(created_at__lt=cutoff).exclude(audio="").iterator():
        try:
            row.audio.delete(save=False)
        except OSError as exc:            # already gone from the disk: the row still lets go of it
            logger.warning("robot heard #%s: %s", row.pk, exc)
        RobotHeard.objects.filter(pk=row.pk).update(audio="")
        gone += 1
    return gone


def learn(heard: str, said: str) -> None:
    """Count what the pair teaches (RobotHeardFix)."""
    from .models import RobotHeardFix

    for wrote, meant in heardfix.learn(heard, said):
        fix, made = RobotHeardFix.objects.get_or_create(heard=wrote[:120], means=meant[:120])
        if not made:
            RobotHeardFix.objects.filter(pk=fix.pk).update(seen=F("seen") + 1)


def fixes() -> list:
    """The lessons the robot uses ([{"heard", "means"}]): seen twice, on, and
    for each thing heard the meaning seen most."""
    from .models import RobotHeardFix

    best = {}
    for fix in RobotHeardFix.objects.filter(is_active=True, seen__gte=SEEN_TO_USE).order_by("-seen", "pk"):
        best.setdefault(fix.heard, fix.means)
    return [{"heard": h, "means": m} for h, m in best.items()]


def sentences() -> list:
    """Whole sentences the robot knows how to hear ([{"heard", "means", "seen"}]):
    the same words from its ears at least SENTENCE_SEEN times, and the teacher's
    words the same every time — an answer seen once may be a slip, two
    different ones are two things that sound alike. «means» the same as
    «heard»: the ears were right, and the robot answers it at once from then on.

    Never with a number (heardfix.sentence_lesson), nor an English guess, nor
    a lesson the owner turned off on «🎓 تعلّم السمع» (a RobotHeardFix of the
    whole sentence)."""
    from datetime import timedelta

    from .models import RobotHeard, RobotHeardFix

    since = timezone.now() - timedelta(days=SENTENCES_FROM_DAYS)
    rows = (RobotHeard.objects.filter(created_at__gte=since).exclude(local_text="").exclude(teacher_text="")
            .order_by("-created_at").values_list("local_text", "teacher_text")[:10 * SENTENCES_MOST])
    off = {heardfix.sentence_key(h) for h in RobotHeardFix.objects.filter(is_active=False)
           .values_list("heard", flat=True)}
    heard = {}
    for local, said in rows:
        lesson = heardfix.sentence_lesson(local, said) if usable(said) else None
        if lesson is None or lesson[0] in off or len(lesson[0]) > SENTENCE_LONGEST:
            continue                     # (longer than the page can turn off: a speech, not a question)
        key, means = lesson
        meanings = heard.setdefault(key, {})
        count_and_words = meanings.setdefault(heardfix.sentence_key(means), [0, means])
        count_and_words[0] += 1
    out = []
    for key, meanings in heard.items():
        if len(meanings) != 1:
            continue                     # the teacher heard different things in it
        ((seen, means),) = meanings.values()
        if seen >= heardfix.SENTENCE_SEEN:
            out.append({"heard": key, "means": means, "seen": seen})
    out.sort(key=lambda s: -s["seen"])
    return out[:SENTENCES_MOST]
