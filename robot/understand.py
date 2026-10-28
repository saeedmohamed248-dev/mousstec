"""
robot/understand.py — «🧠 مخ الفهم»: what was meant, not only what was heard.

The robot used to understand by rules: a pattern for each way a thing is said
(«ضيف … للمخزون»، «إف سيتي» = F30، «شكرا» ends it…). Every shop log brought
sentences no rule had seen — «تعرف تضيفلي في مخزون منتج جديد»، «The facility
متوريني twenty» (Azure's guess at «إف سيتي موتور إن توينتي») — and a rule more.

Now each sentence is understood first, by Gemini, with all the robot knows at
that moment:
  • every hearing of it — the robot's own ears (Vosk) and the teacher's (Azure);
  • what was just said, the robot's last question, and the step it waits on
    (robot/wizard.py: «القطعة إيه؟»، «أضيفه صنف جديد؟»);
  • the customer's car, and the names of what the branch sells.
Its answer is a meaning, not a reply: an intent and the sentence the robot acts
on, written the way the robot's own rules read for sure («ضيف منتج جديد اسمه
طقم فانوس F45 للمخزون»، «عربيتي F30 موتور N20»، «أيوه»).

The robot's rules still do everything: a price comes from the catalog, stock
and money move through the wizard's steps and «أسجله؟», by the employee's role
and PIN. The AI understands; it never acts.

It understands, it doesn't memorise: every sentence is understood afresh, with
what's going on at that moment. Every meaning teaches the shop's own robot
(RobotUnderstanding) all the same:
  • the same words understood the same way twice — and needing nothing said
    before them — stand in when Gemini doesn't answer (out of time, its quota);
  • those (with no step waiting) go to «مخ موس» with the catalog, so it
    understands them offline too (teachers.lessons → heard_sentences);
  • the rows are a dataset («🎓 تعلّم السمع»'s download) to train the shop's own model.

Never for a PIN (nothing of it leaves the server), nor while a count, a branch
question or a staff-photo round waits on its own words. Without a Gemini key,
over the time allowed, or with ROBOT_UNDERSTAND=0: the rules alone, as before.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("mouss_tec_core")

# 🐛 [FIX]: from the shop's log (brain-0.9.12) — 2 s a model was under what the first one takes
# with this prompt (1.9–2.2 s): most sentences timed out, a second model was asked, and the
# answer came 5–10 s late. Flash-Lite first (well under a second), and room for it.
TIMEOUT_S = 3.0                    # one model: the customer stands waiting
BUDGET_S = 3.5                     # all of them
MODEL = "gemini-flash-lite-latest"  # asked first (ROBOT_UNDERSTAND_MODEL another)
LEARN_SEEN = 2                     # understood the same way this many times: learned
LEARN_FROM_DAYS = 90
LESSONS_MOST = 400
LESSONS_KEEP_S = 600
HINTS_MOST = 40                    # part names sent with the sentence

INTENTS = frozenset({"part", "car", "stock", "sale", "purchase", "expense", "answer", "yes", "no", "cancel",
                     "thanks", "greeting", "chat", "workshop", "staff", "other", "unclear"})
# What can be learned whatever came before it (never an answer to a step's question).
_LEARNABLE = frozenset({"part", "car", "stock", "sale", "purchase", "cancel", "thanks", "greeting", "chat",
                        "workshop"})
# …and what «مخ موس» may be taught to understand offline (the rest wait for the server).
_OFFLINE = frozenset({"part", "car", "thanks", "greeting", "chat"})
# Said plainly: the rules read these for sure, no need to ask.
_PLAIN = frozenset("ايوه ايوا اه لا لأ لاء شكرا الغي تمام مفيش جديد".split())

_PROMPT = """\
إنت «مخ الفهم» لروبوت في محل قطع غيار BMW و MINI في مصر. الروبوت بيسمع الزباين والموظفين بالعامية المصرية،
وسمعه ساعات بيغلط: «الساتي» أو «The facility متوريني twenty» ممكن تكون «إف سيتي موتور إن توينتي» (F30، N20).
شغلتك تفهم الشخص كان قاصد إيه، مش ترد عليه.

هتاخد JSON فيه:
- hearings: كل اللي اتسمع للجملة (سمع الروبوت وسمع المدرّس) — ممكن يختلفوا، اختار الأقرب للمعنى مع السياق.
- waiting_for: لو الروبوت سأل سؤال ومستني إجابته (خطوة في فاتورة أو إضافة للمخزون): الخطوة والسؤال.
- robot_said: آخر حاجة الروبوت قالها. recent: آخر كام جملة. car: عربية الزبون لو اتقالت.
- branch_parts: أسماء أصناف عند الفرع (اكتب اسم القطعة زيها لو هي المقصودة).

رجّع JSON بس، بالشكل ده:
{"meant": "...", "intent": "...", "command": "...", "needs_context": true/false, "confidence": 0.0-1.0}

- meant: الجملة اللي اتقالت فعلاً، مصححة، بالعامية المصرية.
- intent واحد من: part (سؤال عن قطعة: موجودة؟ بكام؟ فين؟ تركب على إيه؟)، car (العربية أو الموتور بس)،
  stock (إضافة للمخزون)، sale (فاتورة مبيعات)، purchase (فاتورة مشتريات)، expense (مصروف)،
  answer (إجابة على سؤال الروبوت المستني)، yes، no، cancel (إلغاء)، thanks، greeting،
  chat (دردشة أو مناقشة عامة في أي موضوع: كورة، الجو، الشغل، رأيه في عربية)،
  workshop (سؤال فني: صيانة، وايرنج، فيشة كمبيوتر، كود عطل، عطل، تركيب)، staff (سؤال إداري: مبيعات النهارده، النواقص)،
  other، unclear (مش مفهوم خالص — متخمنش).
- command: الجملة اللي الروبوت هينفذها، بالشكل اللي قواعده بتفهمه أكيد:
  • قطعة: «عندك <القطعة> <الشاسيه>؟» أو «بكام <القطعة>؟» أو «<القطعة> مكانها فين؟»
  • عربية: «عربيتي <الشاسيه> موتور <الموتور>» — الشاسيه والموتور بحروف إنجليزي كبيرة وأرقام: F30، E90، N20، B48.
  • مخزون: «ضيف <العدد> <القطعة> للمخزون»؛ صنف جديد: «ضيف منتج جديد اسمه <الاسم> للمخزون»
    (من غير اسم: «ضيف منتج جديد للمخزون»؛ من غير عدد: سيب العدد).
  • «اعمل فاتورة مبيعات»، «اعمل فاتورة مشتريات»، «سجل مصروف <المبلغ> <على إيه>».
  • سؤال فني: السؤال نفسه مصحح وكامل، وكود العطل مكتوب زي ما بيتكتب (بي زيرو تلاتة زيرو واحد → P0301)،
    واسم الكمبيوتر بحروف إنجليزي (DME، FRM، CAS)، وضيف العربية لو معروفة.
  • chat: الجملة نفسها مصححة، علشان الروبوت يكمّل المناقشة.
  • yes: «أيوه»، no: «لا»، cancel: «الغي»، thanks: «شكرا»، greeting: «السلام عليكم».
  • answer: الإجابة بس من غير كلام زيادة — الاسم، أو الرقم بالأرقام (تلاتة → 3، ألف وخمسمية → 1500)،
    أو «جديد»، أو «مفيش». لو بيطلب حاجة تانية خالص وهو في خطوة، اكتب طلبه الجديد بالشكل بتاعه.
  • تصحيح وهو بيأكد («أسجله؟»): «سعر البيع <رقم>» أو «سعر الشرا <رقم>» أو «العدد <رقم>».
  • غير كده: meant نفسها.
- needs_context: true لو المعنى معتمد على اللي اتقال قبلها (زي «توينتي» بعد «الموتور إيه؟» أو أي answer).
- confidence: قد إيه إنت متأكد.
متألفش أسماء ولا أرقام مش في الكلام. الأسعار والكميات من الكلام بس.
"""


@dataclass
class Meaning:
    intent: str
    command: str
    meant: str = ""
    needs_context: bool = False
    confidence: Optional[float] = None
    source: str = "gemini"
    ms: int = 0


def enabled() -> bool:
    from .audio import _gemini_key

    return os.getenv("ROBOT_UNDERSTAND", "1").strip() != "0" and bool(_gemini_key())


def _key(step: str, heard: str) -> str:
    from .brain.heardfix import sentence_key

    return f"{step}|{sentence_key(heard)}"


def _plain(said: str, step: str) -> bool:
    """Said in the words the rules read for sure: «أيوه»، «لا»، «شكرا»، a number to a step."""
    from .brain.knowledge import normalize

    words = normalize(said or "").split()
    return bool(words) and (all(w in _PLAIN for w in words) or (bool(step) and all(w.isdigit() for w in words)))


def _busy_elsewhere(device, said: str) -> bool:
    """`said` answers a flow that reads its own words: a count, a branch («أنهي فرع؟»), a staff-photo
    round's «التالي», the mobile number asked for.

    🐛 [FIX]: from the shop's log (brain-0.9.12) — any of those merely open (the mobile asked for once
    a part was out, an entry waiting on its branch) kept every sentence from being understood:
    «عربية إي ناينتي … موتور إن فورتي سكس» found an oil-pressure sensor. Only its own answers now."""
    from . import entries, memory, services, views
    from .brain import talk

    try:
        if memory.recall(device).get("restock_for") and (talk.mobile_in(said) or talk.tried_a_number(said)):
            return True
        if views._is_enrollment_control(said):
            return True
        # «أسجلها على أنهي فرع؟» — «هنا»، «المعادي»: a branch is a word or two.
        if len(said.split()) <= 3 and entries.pending_for(device) is not None:
            return True
        if services.get_open_stock_take(device) is not None:
            query, qty = services.parse_count_utterance(said)
            return query is not None and qty is not None
    except Exception:                       # can't tell: understood, the rules still decide
        logger.warning("robot understand: couldn't tell what's waiting", exc_info=True)
    return False


def _hints(device, hearings: list) -> list:
    """The branch's part names nearest what was heard, then its best stocked."""
    from . import teachers
    from .brain.knowledge import part_words

    names = teachers.hints_for(device)
    said = {w for h in hearings for w in part_words(h) if len(w) > 2}
    near = [n for n in names if said & set(part_words(n))]
    return list(dict.fromkeys(near[:HINTS_MOST // 2] + names))[:HINTS_MOST]


def _ask(device, hearings: list, waiting: dict) -> Optional[dict]:
    from . import audio, memory

    turns = [{"said": str(s)[:160], "robot": str(r)[:200]} for s, r in (memory.recall(device).get("turns") or [])[-3:]]
    context = {"hearings": hearings, "waiting_for": waiting or None, "robot_said": memory.last_reply(device)[:200],
               "recent": turns, "car": memory.car(device) or None, "branch_parts": _hints(device, hearings)}
    text = audio.gemini_generate([{"text": _PROMPT}, {"text": json.dumps(context, ensure_ascii=False)}],
                                 what="understand", timeout=TIMEOUT_S, budget=BUDGET_S, json_out=True,
                                 prefer=os.getenv("ROBOT_UNDERSTAND_MODEL", "").strip() or MODEL)
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        found = json.loads(text)
    except ValueError:
        logger.warning("robot understand: not JSON: %s", text[:200])
        return None
    return found if isinstance(found, dict) else None


def _clean(found: dict) -> Optional[Meaning]:
    intent = str(found.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        return None
    command = " ".join(str(found.get("command") or "").split())[:255]
    meant = " ".join(str(found.get("meant") or "").split())[:255]
    if not command and intent != "unclear":
        command = meant
    if not command and intent != "unclear":
        return None
    try:
        confidence = max(0.0, min(1.0, float(found.get("confidence"))))
    except (TypeError, ValueError):
        confidence = None
    return Meaning(intent=intent, command=command, meant=meant or command,
                   needs_context=bool(found.get("needs_context")) or intent in ("answer", "yes", "no"),
                   confidence=confidence)


def meaning(device, said: str, *, ears: str = "", teacher: str = "", via: str = "robot") -> Optional[Meaning]:
    """What `said` meant here and now, or None — the rules alone then (no key, a PIN, an answer the
    rules read for sure…). Gemini asked and no meaning came back (out of time, not JSON): a Meaning
    with no intent (`source` "none"), for the robot's log to say so.

    `ears`: the robot's own hearing when `said` is another's (a teacher's, or a lesson's);
    `teacher`: the teacher's hearing, when there was one."""
    from . import wizard

    said = (said or "").strip()
    if not said or not enabled():
        return None
    waiting = wizard.context(device, via)
    step = waiting.get("step", "")
    if step == "pin" or wizard.wants_secret(device) or _plain(said, step) or wizard.reads(step, said):
        return None                         # (a price, a count, a «أيوه» to a step: the rules' own, at once)
    if not step and _busy_elsewhere(device, said):
        return None
    heard = (ears or said).strip()
    # 🐛 [FIX]: «عاوزه يفهم ذكي مش يحفظ» — a sentence learned was answered as it was the last time,
    # whatever was said before it. Understood afresh every time, with what's going on now; what was
    # learned stands in only when Gemini doesn't answer (and offline, on «مخ موس»).
    hearings = list(dict.fromkeys(h for h in (ears, teacher, said) if h and h.strip()))
    started = time.monotonic()
    try:
        found = _ask(device, hearings, waiting)
    except Exception:                       # understanding is a help: never worth the answer itself
        logger.warning("robot understand: failed", exc_info=True)
        found = None
    got = _clean(found) if found else None
    if got is None:
        learned = learned_meanings().get(_key(step, heard))
        if learned is not None:
            got = Meaning(intent=learned["intent"], command=learned["command"], meant=learned["command"],
                          source="learned", ms=int((time.monotonic() - started) * 1000))
            _keep(device, got, heard, teacher, waiting)
            return got
        return Meaning(intent="", command="", source="none", ms=int((time.monotonic() - started) * 1000))
    got.ms = int((time.monotonic() - started) * 1000)
    _keep(device, got, heard, teacher, waiting)
    return got


def _keep(device, got: Meaning, heard: str, teacher: str, waiting: dict) -> None:
    from .models import RobotUnderstanding

    try:
        RobotUnderstanding.objects.create(
            device=device, heard=heard[:255], teacher_text=(teacher or "")[:255], step=waiting.get("step", "")[:30],
            question=waiting.get("question", "")[:255], intent=got.intent[:30], meant=got.meant[:255],
            command=got.command[:255], needs_context=got.needs_context, confidence=got.confidence,
            source=got.source, ms=got.ms)
    except Exception:                       # the lesson's lost, the answer isn't
        logger.warning("robot understand: not kept", exc_info=True)


# ---------------------------------------------------------------------------
# What the shop's robot has learned
# ---------------------------------------------------------------------------

def learned_meanings() -> dict:
    """{step|sentence: {"intent", "command", "seen"}}: words understood the same way at least
    LEARN_SEEN times (and never otherwise), needing nothing said before them — worked out once
    for what's been understood so far, not on every sentence."""
    from datetime import timedelta

    from django.core.cache import cache
    from django.db.models import Count, Max
    from django.utils import timezone

    from .models import RobotUnderstanding

    rows = RobotUnderstanding.objects.filter(source="gemini")
    stamp = rows.aggregate(n=Count("id"), last=Max("id"))
    key = f"robot:understand:learned:{stamp['n']}:{stamp['last']}"
    try:
        found = cache.get(key)
    except Exception:
        found = None
    if isinstance(found, dict):
        return found
    since = timezone.now() - timedelta(days=LEARN_FROM_DAYS)
    seen: dict = {}
    for step, heard, intent, command, context in (rows.filter(created_at__gte=since).order_by("-created_at")
                                                  .values_list("step", "heard", "intent", "command",
                                                               "needs_context")[:20 * LESSONS_MOST]):
        k = _key(step, heard)
        if len(k.split("|", 1)[1].split()) < 2:
            continue                        # one word: what it means is what was asked before it
        meanings = seen.setdefault(k, {})
        if context or intent not in _LEARNABLE:
            meanings[None] = None           # it needed what came before: never learned
            continue
        entry = meanings.setdefault(command, {"intent": intent, "command": command, "seen": 0})
        entry["seen"] += 1
    found = {}
    for k, meanings in seen.items():
        if len(meanings) != 1 or None in meanings:
            continue
        ((_command, entry),) = meanings.items()
        if entry["seen"] >= LEARN_SEEN:
            found[k] = entry
    try:
        cache.set(key, found, LESSONS_KEEP_S)
    except Exception:
        pass
    return found


def lessons() -> list:
    """What «مخ موس» understands offline too ([{"heard", "means"}]): learned with no step waiting,
    a question or the car (never a stock, a sale, money — those wait for the server), and no part
    number in it (a misheard digit is another part)."""
    out = []
    for k, entry in sorted(learned_meanings().items(), key=lambda kv: -kv[1]["seen"]):
        step, heard = k.split("|", 1)
        if step or entry["intent"] not in _OFFLINE or any(c.isdigit() for c in heard):
            continue
        if re.search(r"\d{4,}", entry["command"]):
            continue
        out.append({"heard": heard, "means": entry["command"]})
    return out[:LESSONS_MOST]


def dataset_rows():
    """Every meaning Gemini gave, for the training download (JSON lines)."""
    from .models import RobotUnderstanding

    return (RobotUnderstanding.objects.filter(source="gemini").order_by("created_at")
            .values("heard", "teacher_text", "step", "question", "intent", "meant", "command", "needs_context",
                    "confidence", "created_at"))


def summary() -> dict:
    """{"understood", "learned", "at_once"} for «🎓 تعلّم السمع»."""
    from .models import RobotUnderstanding

    return {"understood": RobotUnderstanding.objects.filter(source="gemini").count(),
            "learned": len(learned_meanings()),
            "at_once": RobotUnderstanding.objects.filter(source="learned").count()}
