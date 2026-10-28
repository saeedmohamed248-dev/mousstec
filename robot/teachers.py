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
import queue
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

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
_ARABIC_WORD = re.compile(r"[\u0621-\u064A]{2,}")


@dataclass
class Heard:
    text: str
    teacher: str
    confidence: Optional[float] = None
    billed_seconds: int = 0
    # The teacher's hearings of it, likeliest first ([(text, confidence)]: the first is `text`).
    alternatives: list = field(default_factory=list)


# What else the teacher may have heard: Azure's NBest, Google's alternatives. When the
# shop makes no sense of the likeliest and does of the next («فلتر زيت» second to
# «فلتر زي»), the next is what was said — not much less sure than the first, though.
ALTERNATIVES = 5
ALTERNATIVE_MARGIN = 0.25
# A teacher this unsure, and nothing in the shop made sense of it: «مسمعتكش كويس»,
# not an answer to something nobody said (robot/views._answer_voice).
UNSURE_BELOW = 0.45


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


TEACHERS = ("azure", "google", "gemini")
_SERVICE = {"azure": "azure_stt", "google": "google_stt", "gemini": "gemini"}   # robot/health.py
_BUDGETS = {"azure": ("ROBOT_AZURE_FREE_MINUTES", "300"), "google": ("ROBOT_GOOGLE_FREE_MINUTES", "60"),
            "gemini": ("ROBOT_GEMINI_TEACH_MINUTES", "600")}
SENTENCES_FROM_DAYS = 90   # what the ears wrote lately: a new model, a new microphone hear otherwise
SENTENCES_MOST = 2000
SENTENCE_LONGEST = 120     # RobotHeardFix.heard: what «🎓 تعلّم السمع» can turn off
TRUSTED_MOST = 3000
LESSONS_KEEP_S = 600       # worked out again sooner when a teacher hears something new


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
    """What `teacher` billed this month (this shop's robots): what it heard and was
    kept (RobotHeard), and what it billed with nothing kept (RobotTeacherUsage)."""
    from .models import RobotHeard, RobotTeacherUsage

    now = now or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = 0
    for model in (RobotHeard, RobotTeacherUsage):
        total += (model.objects.filter(teacher=teacher, created_at__gte=start)
                  .aggregate(s=Sum("billed_seconds"))["s"]) or 0
    return int(total)


def _bill(device, teacher: str, kind: str, seconds: float, billed: int) -> None:
    """Seconds billed with nothing kept of them: on the ledger, not an empty RobotHeard row."""
    from .models import RobotTeacherUsage

    if billed:
        RobotTeacherUsage.objects.create(device=device, teacher=teacher, kind=kind,
                                         seconds=round(seconds, 2), billed_seconds=billed)


def minutes_left(teacher: str) -> float:
    return max(0.0, free_minutes(teacher) - used_seconds(teacher) / 60)


def _azure_request():
    """(url, params, headers) of Azure's speech-to-text for a 16 kHz PCM WAV."""
    region, key = _env("ROBOT_AZURE_SPEECH_REGION"), _env("ROBOT_AZURE_SPEECH_KEY")
    return (f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1",
            {"language": LANGUAGE, "format": "detailed"},
            {"Ocp-Apim-Subscription-Key": key, "Accept": "application/json",
             "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000"})


def _azure(pcm_wav: bytes, seconds: float, timeout: float = TIMEOUT_S) -> Optional[Heard]:
    import requests

    url, params, headers = _azure_request()
    resp = requests.post(url, params=params, headers=headers, data=pcm_wav, timeout=timeout)
    resp.raise_for_status()
    return _azure_words(resp.json(), seconds)


def _azure_words(body: dict, seconds: float) -> Heard:
    """What Azure's answer says was heard in `seconds` of sound."""
    billed = math.ceil(seconds)
    if body.get("RecognitionStatus") != "Success":
        return Heard("", "azure", None, billed)
    nbest = [b for b in body.get("NBest") or [] if isinstance(b, dict)]
    best = (nbest or [{}])[0]
    text = str(best.get("Display") or body.get("DisplayText") or "")
    others = [(str(b.get("Display") or ""), b.get("Confidence")) for b in nbest[:ALTERNATIVES] if b.get("Display")]
    return Heard(text, "azure", best.get("Confidence"), billed, others)


# «إرسال الصوت أول بأول» (robot/consumers.py): Azure hearing a sentence while it's still being said.
STREAM_IDLE_S = 8              # no sound for this long mid-sentence (the robot gone): given up
STREAM_OFF_S = 6 * 3600        # Azure refused a live hearing: whole recordings only, for a while
STREAM_TRUST_S = 7 * 24 * 3600   # a live hearing came back with words: its «nothing heard» is believed too
STREAM_FEWEST_S = 20           # fewer of the month's seconds left: the last ones go to whole recordings
STREAM_SETTLE_S = 1.0          # let go: how long its trip is given to stop
_STREAM_OFF, _STREAM_OK = "robot:azure:stream:off", "robot:azure:stream:ok"
_END, _DROP = object(), object()
_PCM_RATE = 16000 * 2          # bytes a second of 16-bit 16 kHz sound


class _Dropped(Exception):
    """A live hearing let go of (nobody needs its words), or gone quiet."""


def _stream_header() -> bytes:
    """A 16 kHz PCM WAV's header for a sentence still being said: only the
    first chunk carries one, and its length isn't known yet — the sizes say
    MOST_SECONDS, the most that's ever sent; the sound's end is the stream's."""
    size = MOST_SECONDS * _PCM_RATE
    return (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, _PCM_RATE, 2, 16)
            + b"data" + struct.pack("<I", size))


def can_stream() -> bool:
    """Whether Azure may hear the next sentence while it's said: its key is
    set, the month's minutes have room, and it hasn't refused lately."""
    from django.core.cache import cache

    return ("azure" in configured() and not cache.get(_STREAM_OFF)
            and minutes_left("azure") * 60 >= STREAM_FEWEST_S)


def _stream_off(why: str) -> None:
    from django.core.cache import cache

    logger.warning("robot teacher azure: no live hearing for %d h (%s) — whole recordings, as before",
                   STREAM_OFF_S // 3600, why)
    cache.set(_STREAM_OFF, why[:80], STREAM_OFF_S)


class AzureStream:
    """Azure hearing a sentence while it's still being said (the live socket,
    robot/consumers.py). Its sound goes up in chunks as it comes
    (Transfer-Encoding: chunked — Azure starts on it at once), so the words
    are ready a moment after the last one, not once the whole recording has
    gone up and been heard from its start.

    `check` and `hear` take its words instead of a second trip with the
    whole recording — and make that trip as before when it failed (or heard
    nothing, before a live hearing was ever seen to hear words)."""

    def __init__(self, post=None):
        self._sound: queue.Queue = queue.Queue()
        self._post = post                # requests.post (a stand-in in the tests)
        self.seconds = 0.0               # of sound gone up so far
        self.taken = False               # its words were asked for: their seconds are counted with them
        self.status = None               # Azure's HTTP status, once it answered
        self.heard: Optional[Heard] = None
        self.error: Optional[BaseException] = None
        self._done = threading.Event()
        threading.Thread(target=self._hear, daemon=True).start()

    def feed(self, pcm: bytes) -> None:
        """16-bit 16 kHz PCM, as it's said."""
        if pcm and not self._done.is_set():
            self._sound.put(pcm)

    def cancel(self) -> None:
        """Not wanted after all: its trip is cut short."""
        self._sound.put(_DROP)

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def _chunks(self):
        yield _stream_header()
        while True:
            try:
                piece = self._sound.get(timeout=STREAM_IDLE_S)
            except queue.Empty:
                raise _Dropped("no sound for a while") from None
            if piece is _END:
                return
            if piece is _DROP:
                raise _Dropped("let go")
            room = int((MOST_SECONDS - self.seconds) * _PCM_RATE) // 2 * 2
            piece = piece[:max(0, room)]
            if piece:                    # past MOST_SECONDS: no more goes up
                self.seconds += len(piece) / _PCM_RATE
                yield piece

    def _hear(self) -> None:
        try:
            import requests

            url, params, headers = _azure_request()
            # Chunks: what Azure asks for with them (it answers «100 Continue», which is read past).
            headers["Expect"] = "100-continue"
            resp = (self._post or requests.post)(url, params=params, headers=headers, data=self._chunks(),
                                                 timeout=TIMEOUT_S)
            self.status = resp.status_code
            resp.raise_for_status()
            self.heard = _azure_words(resp.json(), self.seconds)
        except BaseException as exc:     # told to whoever asks for its words
            self.error = exc
        finally:
            self._done.set()

    def result(self, timeout: float) -> Heard:
        """Its words, once the sentence's last sound has gone up — `timeout` s
        from now at most. Raises what went wrong."""
        self.taken = True
        self._sound.put(_END)
        if not self._done.wait(max(0.0, timeout)):
            self.cancel()
            raise TimeoutError("Azure's live hearing took too long")
        if self.error is not None:
            raise self.error
        return self.heard

    def billed(self) -> int:
        """The seconds Azure counts for it: all the sound that went up —
        none when it refused it (an HTTP error)."""
        if isinstance(self.status, int) and self.status >= 400:
            return 0
        return math.ceil(self.seconds) if self.seconds > 0 else 0


def settle(device, stream: AzureStream) -> None:
    """A live hearing once its sentence is over: let go when nothing took its
    words — the seconds Azure heard counted all the same, against the
    month's minutes (on the ledger: nothing is learned from it)."""
    if stream.taken:
        return
    stream.taken = True
    stream.cancel()
    stream.wait(STREAM_SETTLE_S)
    _bill(device, "azure", "let_go", stream.seconds, stream.billed())


def _by_azure(device, pcm_wav: bytes, seconds: float, timeout: float, streamed=None) -> Optional[Heard]:
    """Azure's words for the recording: the live hearing's (AzureStream), when
    there is one and it heard them — the whole recording's otherwise, as
    before. Whatever Azure counted for the live hearing is counted with them."""
    from django.core.cache import cache

    if streamed is None:
        return _azure(pcm_wav, seconds, timeout=timeout)
    started, live = time.monotonic(), None
    try:
        live = streamed.result(timeout)
    except _Dropped:                      # gone quiet: the whole recording then
        pass
    except Exception as exc:              # refused, cut off, too slow: the whole recording — and, till
        # it's tried again, whole recordings only (a failing live hearing would cost its seconds twice)
        _stream_off(f"{type(exc).__name__} {health.code_of(exc) or ''}".strip())
    billed = streamed.billed()
    if live is not None and (live.text or cache.get(_STREAM_OK)):
        if live.text:
            cache.set(_STREAM_OK, 1, STREAM_TRUST_S)
        return live
    left = timeout - (time.monotonic() - started)
    try:
        if left < TEACHER_FEWEST_S:
            raise TimeoutError("no time left for the whole recording")
        got = _azure(pcm_wav, seconds, timeout=left)
    except Exception:
        _bill(device, "azure", "failed", streamed.seconds, billed)   # counted, though nothing came of it
        raise
    if got is not None:
        got.billed_seconds += billed
        if live is not None and got.text:
            # Words in the whole recording, none in the same sound live: the live
            # hearing isn't heard right (its header?) — whole recordings for a while.
            _stream_off("nothing heard live, words in the whole recording")
    return got


def _google(pcm: bytes, rate: int, seconds: float, hints: list, timeout: float = TIMEOUT_S) -> Optional[Heard]:
    import requests

    config = {"encoding": "LINEAR16", "sampleRateHertz": rate, "languageCode": LANGUAGE,
              "maxAlternatives": ALTERNATIVES}
    if hints:                             # the shop's parts: «فلتر زيت» heard as such
        config["speechContexts"] = [{"phrases": hints[:500]}]
    resp = requests.post(
        "https://speech.googleapis.com/v1/speech:recognize", params={"key": _env("ROBOT_GOOGLE_STT_KEY")},
        json={"config": config, "audio": {"content": base64.b64encode(pcm).decode()}}, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    billed = re.match(r"(\d+)", str(body.get("totalBilledTime") or ""))
    billed = int(billed.group(1)) if billed else GOOGLE_ROUNDING_S * math.ceil(seconds / GOOGLE_ROUNDING_S)
    results = [r for r in body.get("results") or [] if isinstance(r, dict)]
    alternatives = [(r.get("alternatives") or [{}])[0] for r in results]
    text = " ".join(str(a.get("transcript") or "").strip() for a in alternatives).strip()
    confidence = alternatives[0].get("confidence") if alternatives else None
    # A question is one result; its other hearings only make sense whole.
    others = ([(str(a.get("transcript") or "").strip(), a.get("confidence"))
               for a in results[0].get("alternatives") or [] if str(a.get("transcript") or "").strip()]
              if len(results) == 1 else [])
    return Heard(text, "google", confidence, billed, others)


def _gemini(pcm_wav: bytes, seconds: float, hints: list, name: str, timeout: float = TIMEOUT_S) -> Optional[Heard]:
    """Gemini hears it (the last teacher), the branch's part names in mind."""
    try:
        text = audio_svc.transcribe(pcm_wav, name=name, strict=True, hints=hints, budget=timeout)
    except audio_svc.SttUnavailable as exc:
        raise RuntimeError(str(exc)) from exc
    return Heard(text, "gemini", None, math.ceil(seconds))


def usable(text: str, against: str = "") -> bool:
    """Whether what a teacher wrote can stand for the question: Arabic, or a
    real English sentence — not a few English words guessed at a short Arabic one.

    `against`: what the robot's own ears wrote. 🐛 [FIX]: Azure (ar-EG) wrote «Yeah most anna.
    Murderer. وعايز أعمل فاتورة.» where Vosk had «يا موس أنا مدير وعايز عمل فاتورة» right — mostly
    English letters, and the robot answered in English «Sorry, I didn't get what you mean». Arabic
    the ears heard stands against a teacher's Arabic sentence half-guessed in English (a sentence all
    English is someone speaking English: Vosk writes Arabic whatever it hears)."""
    text = text or ""
    if against and len(_ARABIC_WORD.findall(against)) >= 2 and _ARABIC_WORD.search(text):
        latin = sum(len(w) for w in _LATIN_WORD.findall(text))
        if latin > len(_ARABIC.findall(text)):
            return False
    if _ARABIC.search(text):
        return True
    return len(_LATIN_WORD.findall(text)) >= FOREIGN_FEWEST_WORDS


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


def _likeliest(device, got: Heard, understood: Optional[Callable[[str], bool]]) -> tuple:
    """(text, confidence): the teacher's likeliest hearing — or, when the shop
    makes no sense of it, the first of its other hearings the shop does, not
    much less sure (ALTERNATIVE_MARGIN)."""
    first = (got.text, got.confidence)
    try:
        if understood is None or not got.text or len(got.alternatives) < 2 or understood(got.text):
            return first
        top = got.alternatives[0][1]
        for rank, (text, conf) in enumerate(got.alternatives[1:], start=1):
            # 🐛 [FIX]: Google says how sure it is of its first hearing only — its fifth one, with no
            # number, replaced it with no margin at all. Without one, only the very next hearing is taken.
            if not isinstance(conf, (int, float)):
                if rank > 1:
                    continue
            elif isinstance(top, (int, float)) and conf < top - ALTERNATIVE_MARGIN:
                continue
            text = _without_name(device, text)
            if text and usable(text) and understood(text):
                return text, (conf if isinstance(conf, (int, float)) else got.confidence)
    except Exception as exc:              # 🐛 [FIX]: a failure here lost the row of seconds already billed
        logger.warning("robot teacher: weighing its hearings failed: %s", exc)
    return first


def check(device, wav: bytes, heard: str, confidence: Optional[float] = None,
          understood: Optional[Callable[[str], bool]] = None, streamed=None, keep: bool = True) -> Optional[Heard]:
    """What a teacher heard in `wav` (a recording «مخ موس» wasn't sure of —
    it heard `heard`, without the name), or None: no teacher set up, the
    month's minutes used, or none could hear it. Kept with the recording, and
    what it teaches is learned. `understood(text)`: whether the shop makes
    sense of a hearing — the teacher's next likeliest is taken when it does
    and the first doesn't."""
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
                got = _by_azure(device, pcm_wav, seconds, left, streamed)
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
        got.text, got.confidence = _likeliest(device, got, understood)
        if not keep:
            # A PIN said (robot/wizard.py): its seconds counted, nothing of it kept nor learned.
            _bill(device, teacher, "secret", seconds, got.billed_seconds)
            return got if got.text else None          # digits, not words: `usable` would refuse them
        row = RobotHeard(device=device, local_text=(heard or "")[:255], local_confidence=confidence,
                         teacher=teacher, teacher_text=got.text[:255], teacher_confidence=got.confidence,
                         seconds=round(seconds, 2), billed_seconds=got.billed_seconds)
        row.audio.save(f"{device.pk}-{timezone.now():%Y%m%d%H%M%S%f}.wav", ContentFile(wav), save=False)
        row.save()
        if got.text and not usable(got.text, heard):
            return None                   # kept on the page, but what the ears heard stands
        if got.text:
            learn(heard, got.text)
            return got
    return None


def hear(device, wav: bytes, streamed=None, keep_audio: bool = False) -> str:
    """The robot's ears when its computer has none fast enough («a test clip
    took 14 s here: the server listens»): Azure hears the whole recording,
    the name too, within its minutes — '' when it can't (Gemini then).

    🐛 [FIX]: the server heard every sentence with Gemini only: on the free
    quota it answered 429 (and the retired gemini-2.5-flash 404) and the
    robot went deaf — 16–18 s and «the server can't turn speech into text».

    `keep_audio`: the recording is kept with the teacher's words — a sample to train the shop's
    own hearing on («🎓 تعلّم السمع»'s download): «📱 كلّم موس» with «علّم الروبوت» on."""
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
        got = _by_azure(device, pcm_wav, seconds, TIMEOUT_S, streamed)
    except Exception as exc:              # down, a wrong key, its quota: Gemini's turn
        logger.warning("robot ears: azure failed: %s", exc)
        health.note("azure_stt", False, code=health.code_of(exc), detail=str(exc))
        return ""
    health.note("azure_stt", True)
    if got is None:
        return ""
    fields = dict(device=device, local_text="", teacher="azure", teacher_text=got.text[:255],
                  teacher_confidence=got.confidence, seconds=round(seconds, 2), billed_seconds=got.billed_seconds)
    if keep_audio and got.text:
        row = RobotHeard(**fields)
        row.audio.save(f"{device.pk}-{timezone.now():%Y%m%d%H%M%S%f}.wav", ContentFile(wav), save=False)
        row.save()
    else:
        RobotHeard.objects.create(**fields)
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


def _pairs():
    """(what the ears wrote, what a teacher heard) lately — the lessons' evidence."""
    from datetime import timedelta

    from .models import RobotHeard

    since = timezone.now() - timedelta(days=SENTENCES_FROM_DAYS)
    return (RobotHeard.objects.filter(created_at__gte=since).exclude(local_text="").exclude(teacher_text="")
            .order_by("-created_at").values_list("local_text", "teacher_text")[:10 * SENTENCES_MOST])


def trusted_words(rows=None) -> list:
    """The words the robot's ears write right ([word], surest first,
    heardfix.trusted): a sentence of them is answered at once, no teacher."""
    return heardfix.trusted((local, said) for local, said in (rows if rows is not None else _pairs())
                            if usable(said, local))[:TRUSTED_MOST]


def lessons() -> dict:
    """{"sentences", "trusted"} for the catalog — worked out once for what the
    teachers have heard so far, not every minute for every robot (the
    catalog's asked that often): kept until a teacher hears something new or
    the owner turns a lesson off."""
    from django.core.cache import cache
    from django.db.models import Count, Max

    from .models import RobotHeard, RobotHeardFix

    heard = RobotHeard.objects.exclude(local_text="").aggregate(n=Count("id"), last=Max("id"))
    fixes = RobotHeardFix.objects.aggregate(n=Count("id"), last=Max("updated_at"))
    key = "robot:heard:lessons:{}:{}:{}:{}".format(heard["n"], heard["last"], fixes["n"],
                                                     fixes["last"].isoformat() if fixes["last"] else "")
    found = cache.get(key)
    if not isinstance(found, dict) or "off" not in found:
        rows = list(_pairs())             # 🐛 [FIX]: fetched twice (once for each) on every new hearing
        found = {"sentences": sentences(rows), "trusted": trusted_words(rows),
                 # the whole sentences the owner turned off: never answered at once, trusted words or not
                 "off": list(RobotHeardFix.objects.filter(is_active=False)
                             .values_list("heard", flat=True)[:SENTENCES_MOST])}
        cache.set(key, found, LESSONS_KEEP_S)
    return found


def sentences(rows=None) -> list:
    """Whole sentences the robot knows how to hear ([{"heard", "means", "seen"}]):
    the same words from its ears at least SENTENCE_SEEN times, and the teacher's
    words the same every time — an answer seen once may be a slip, two
    different ones are two things that sound alike. «means» the same as
    «heard»: the ears were right, and the robot answers it at once from then on.

    Never with a number (heardfix.sentence_lesson), nor an English guess, nor
    a lesson the owner turned off on «🎓 تعلّم السمع» (a RobotHeardFix of the
    whole sentence)."""
    from .models import RobotHeardFix

    rows = _pairs() if rows is None else rows
    off = {heardfix.sentence_key(h) for h in RobotHeardFix.objects.filter(is_active=False)
           .values_list("heard", flat=True)}
    heard = {}
    for local, said in rows:
        lesson = heardfix.sentence_lesson(local, said) if usable(said, local) else None
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


def _accuracy(pairs) -> Optional[int]:
    """Of the words the teachers heard, the share (%) the robot's ears wrote
    the same, in the same order — or None with nothing to go on."""
    from difflib import SequenceMatcher

    said = same = 0
    for local, teacher in pairs:
        a, b = heardfix.sentence_key(local).split(), heardfix.sentence_key(teacher).split()
        if not b:
            continue
        said += len(b)
        same += sum(block.size for block in SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks())
    return round(100 * same / said) if said else None


def hearing_quality(now=None) -> dict:
    """«🎯 جودة السمع»: how right the robot's own ears were this week (and the
    week before) against the teachers, what it knows by heart, and how many
    questions it answered at once, without a teacher."""
    from datetime import timedelta

    from .models import RobotHeard, RobotVoiceInteraction

    now = now or timezone.now()
    week = timedelta(days=7)
    heard = RobotHeard.objects.exclude(local_text="").exclude(teacher_text="")
    this = list(heard.filter(created_at__gte=now - week).values_list("local_text", "teacher_text")[:5000])
    before = list(heard.filter(created_at__gte=now - 2 * week, created_at__lt=now - week)
                  .values_list("local_text", "teacher_text")[:5000])
    turns = RobotVoiceInteraction.objects.filter(created_at__gte=now - week)
    total = turns.count()
    at_once = turns.filter(payload__heard_as__in=["learned", "trusted"]).count() if total else 0
    known = lessons()
    from django.db.models import Sum

    from .models import RobotTeacherUsage
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    unkept = RobotTeacherUsage.objects.filter(created_at__gte=month).aggregate(s=Sum("billed_seconds"))["s"] or 0
    return {"accuracy": _accuracy(this), "accuracy_before": _accuracy(before), "taught": len(this),
            "sentences": len(known["sentences"]), "trusted": len(known["trusted"]),
            "at_once": round(100 * at_once / total) if total else None,
            "unkept_minutes": round(unkept / 60, 1) if unkept else 0,
            "unkept_said": f"{round(unkept / 60, 1):g}"}       # «1.5», not the locale's «1,5»


# ---------------------------------------------------------------------------
# «📦 بيانات تدريب موديل المحل»: the recordings with the words a teacher heard
# in them — what a model of the shop's own (Whisper fine-tuned on Egyptian shop
# talk, a better Vosk) is trained on, to hear every letter with the net down.
# ---------------------------------------------------------------------------

TRAINING_README = """# بيانات تدريب موديل سمع المحل — Mouss Tec

كل تسجيل هنا جملة اتقالت للروبوت، ومعاها الكلام اللي المدرّس (Azure / Google / Gemini)
سمعه فيها: ده الكلام الصح اللي الموديل بيتعلّم منه.

- `audio/` — التسجيلات، WAV 16 kHz 16-bit mono.
- `metadata.csv` — لكل تسجيل: `file_name` (مكانه)، `transcription` (الكلام الصح)،
  `robot_heard` (اللي ودان الروبوت كتبته)، `teacher`، `teacher_confidence`، `seconds`،
  `recorded_at`، `robot`.

الصيغة دي هي «audiofolder» بتاعة Hugging Face، فبتتقري على طول:

```python
from datasets import load_dataset, Audio
ds = load_dataset("audiofolder", data_dir="FOLDER")["train"].cast_column("audio", Audio(16000))
```

وبعدها تدريب Whisper (fine-tuning) على العربي المصري — ده اللي هيخلي الروبوت يسمع كل حرف
صح وهو أوفلاين. التسجيلات فيها أصوات الزباين: متشاركهاش برا المحل.

Each recording is a sentence said to the robot, with the words a teacher heard in it —
the transcript a model of the shop's own is trained on (Hugging Face "audiofolder" format).
The recordings hold customers' voices: keep them inside the business.
"""


def training_rows():
    """The recordings a teacher wrote words for (an English guess left out)."""
    from .models import RobotHeard

    return (RobotHeard.objects.exclude(audio="").exclude(teacher_text="")
            .select_related("device").order_by("created_at"))


def training_summary() -> dict:
    """{"count", "minutes", "oldest", "days_kept"} for the page."""
    # 🐛 [FIX]: the card counted the English guesses the zip leaves out (training_zip skips
    # what isn't `usable`): «200 تسجيل» on the page, 170 in the download.
    count, seconds, oldest = 0, 0.0, None
    for said, local, secs, when in training_rows().values_list("teacher_text", "local_text", "seconds",
                                                               "created_at"):
        if usable(said, local):
            count, seconds = count + 1, seconds + (secs or 0)
            oldest = when if oldest is None else min(oldest, when)
    try:
        days = int(_env("ROBOT_HEARD_AUDIO_DAYS", str(AUDIO_DAYS)))
    except ValueError:
        days = AUDIO_DAYS
    return {"count": count, "minutes": round(seconds / 60), "oldest": oldest, "days_kept": max(days, 0)}


class _Pipe:
    """What zipfile writes, handed on as it's written (a zip of hours of
    recordings never sits whole in the server's memory)."""

    def __init__(self):
        self.chunks = []

    def write(self, data) -> int:
        self.chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out, self.chunks = b"".join(self.chunks), []
        return out


def training_zip(rows=None):
    """The dataset as a zip, streamed: audio/<id>.wav (16-bit PCM), metadata.csv, README.md."""
    import csv
    import io
    import zipfile

    rows = training_rows() if rows is None else rows
    pipe, table = _Pipe(), io.StringIO()
    writer = csv.writer(table)
    writer.writerow(["file_name", "transcription", "robot_heard", "teacher", "teacher_confidence", "seconds",
                     "recorded_at", "robot"])
    with zipfile.ZipFile(pipe, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", TRAINING_README)
        for row in rows.iterator():
            if not usable(row.teacher_text, row.local_text):
                continue
            try:
                with row.audio.open("rb") as fh:
                    wav = audio_svc.as_pcm_wav(fh.read())
            except (OSError, ValueError):            # gone from the disk: its words alone teach nothing
                continue
            if not audio_svc.wav_parts(wav):
                continue
            name = f"audio/{row.pk}.wav"
            zf.writestr(zipfile.ZipInfo(name, date_time=row.created_at.timetuple()[:6]), wav,
                        compress_type=zipfile.ZIP_STORED)          # sound doesn't squeeze
            writer.writerow([name, row.teacher_text, row.local_text, row.teacher,
                             "" if row.teacher_confidence is None else round(row.teacher_confidence, 3),
                             round(row.seconds or 0, 2), row.created_at.isoformat(timespec="seconds"),
                             getattr(row.device, "name", "")])
            yield pipe.take()
        zf.writestr("metadata.csv", table.getvalue())
    yield pipe.take()

