"""«الردود الجاهزة»: what the shop answers to the questions every shop gets —
the hours, where it is, its phone, how to pay, the warranty — written once by
the owner on the dashboard («🗨️ الردود الجاهزة») and said at once, net or not.

The same match on the server (robot/views._handle_voice, robot/replies.py)
and on «مخ موس» offline (knowledge.Knowledge.answer). A reply is said only
when one of its questions was asked — every word that matters in it — and
little else was: «عندك فلتر زيت؟» stays a question about a part, and a
question that matches nothing goes on as before (the parts, then the AI).
"""

import re

from .slang import FILLERS

_TASHKEEL = re.compile(r"[ً-ْٰـ]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

# What any question is made of, never what it's about.
LITTLE = frozenset("""
    يا هو هي هما انت انتي انتو انتم انتوا حضرتك حضرتكم احنا ده دي دا دول ايه اي ايش امتي امتا فين
    ازاي كام هل لو سمحت ممكن عايز عاوز عايزه عاوزه عايزين عاوزين طيب طب بقي بس كده كدا يعني من
    في على علي عن مع لحد لغايه ل و او اه ايوه لا معلش بالله please
""".split()) | FILLERS                    # «يا باشا», «يا هندسة», «لو سمحت»

# How many words a question may carry besides the reply's own: «انتو بتفتحوا
# الساعه كام النهارده؟» is still «بتفتحوا امتى»; «… وعندك فلتر زيت؟» is not.
EXTRA_WORDS = 2

_PREFIXES = ("وبال", "وال", "بال", "فال", "لل", "ال", "و", "ب", "ف")
_SUFFIXES = ("كوا", "كم", "كو", "نا", "ها", "هم", "ه", "ك")


def normalize(text: str) -> str:
    t = _TASHKEEL.sub("", (text or "").lower())
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ى", "ي").replace("ة", "ه"))
    return " ".join(_NON_WORD.sub(" ", t).split())


def _stem(word: str) -> str:
    """«مواعيدكم» → «مواعيد», «والعنوان» → «عنوان»: the same word however it's joined."""
    for p in _PREFIXES:
        if word.startswith(p) and len(word) - len(p) >= 3:
            word = word[len(p):]
            break
    for s in _SUFFIXES:
        if word.endswith(s) and len(word) - len(s) >= 3:
            return word[:-len(s)]
    return word


def _content(text: str) -> set:
    return {_stem(w) for w in normalize(text).split() if w not in LITTLE}


def questions(text: str) -> list:
    """The questions of a reply as written on the dashboard: one per line."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def match(said: str, replies: list):
    """(the reply asked for in `said`, the other words said) or None.
    `replies`: [{"questions": [...], "answer": "..."}], the branch's own
    before the shop's (the first wins a tie).

    The other words are for the caller to check against the parts: «فيه ضمان
    على الطرمبة؟» is about the pump, not the shop's warranty. A question
    that's only little words («انتو فين») is matched when they were all said
    and nothing else was («انتو فين يا باشا»)."""
    words = normalize(said).split()
    have = {_stem(w) for w in words if w not in LITTLE}
    if not words:
        return None
    best, best_score = None, 0
    for reply in replies or []:
        if not str(reply.get("answer") or "").strip():
            continue
        for question in reply.get("questions") or []:
            need = _content(question)
            if not need:
                asked = set(normalize(question).split())
                score = 100 if asked and asked <= set(words) and not have else 0
            elif need <= have and len(have - need) <= EXTRA_WORDS:
                score = 10 * len(need) - len(have - need)
            else:
                continue
            if score > best_score:
                best, best_score = (reply, need), score
    if best is None:
        return None
    reply, need = best
    rest = " ".join(w for w in words if w not in LITTLE and _stem(w) not in need)
    return reply, rest
