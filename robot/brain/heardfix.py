"""«🎓 تعلّم السمع»: what these ears write, and what was said.

When the robot isn't sure what it heard, a «teacher» (Azure's or Google's
speech-to-text, on the server: robot/teachers.py) hears the same recording.
Where the two differ by a word or three — «فلترا الزي» / «فلتر زيت» — that's
a lesson; seen twice, the server sends it with the catalog and the robot
writes what was said from then on, the net up or down.

A whole sentence is learned too: the same words from these ears, and the
same words from the teacher every time (twice at least) — the robot then
writes what the teacher heard at once, and answers without asking one again
(robot/teachers.sentences). When the teacher heard what the ears wrote, the
ears were right: that sentence is answered at once from then on, too.

Never a number: «ميتين» heard as «مية» is a price, and a wrong lesson there
is wrong money. Never the little words a question is made of.
"""

from difflib import SequenceMatcher
from typing import Optional

from .arabic_numbers import is_number_word, spoken_codes, to_digits
from .replies import LITTLE, normalize

MOST_WORDS = 3             # a lesson is a word or a few, not a sentence
# A mishearing sounds like what was said: «فلترا الزي» / «فلتر زيت», not
# «كابوس» / «فلتر هوا» (the teacher heard something else there).
SOUNDS_LIKE = 0.5
SENTENCE_SEEN = 2          # a sentence is learned once heard so twice, the teacher's words the same
# Letters an Egyptian says alike, and that news-Arabic ears write apart: «سلطة الزيت» is
# «فلتر الزيت» said in a shop (ط/ت), «تلاتة» «ثلاثة», «ضهر» «ظهر», «ازاز» «قزاز». Only to
# tell whether two words sound alike — the lesson keeps the teacher's own spelling.
_SAID_ALIKE = str.maketrans("طصضظثذقءؤئ", "تسددتدااوي")


def _sounds(text: str) -> str:
    return text.translate(_SAID_ALIKE)


def _is_number(words: list) -> bool:
    # 🐛 [FIX]: «واحد» لوحدها مكانتش بتتحسب رقم (to_digits بيسيب «فلتر واحد» كلام)، فدرس فيها كان
    # ممكن يغيّر رقم في رقم قطعة بيتقال رقم رقم.
    return any(is_number_word(w) for w in words)


def learn(heard: str, said: str) -> list:
    """[(heard, means)]: the words these ears wrote and what was said, where
    the two differ and sound alike."""
    a, b = normalize(heard).split(), normalize(said).split()
    if not a or not b or a == b:
        return []
    match = SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in match.get_opcodes():
        if tag != "replace" or not (0 < i2 - i1 <= MOST_WORDS and 0 < j2 - j1 <= MOST_WORDS):
            continue
        wrote, meant = a[i1:i2], b[j1:j2]
        if _is_number(wrote) or _is_number(meant):
            continue
        if all(w in LITTLE for w in wrote) or all(w in LITTLE for w in meant):
            continue
        wrote, meant = " ".join(wrote), " ".join(meant)
        if SequenceMatcher(a=_sounds(wrote), b=_sounds(meant), autojunk=False).ratio() < SOUNDS_LIKE:
            continue
        out.append((wrote, meant))
    return out


def sentence_key(text: str) -> str:
    """A sentence as it's looked up: normalized, no punctuation (robot/brain/knowledge.normalize)."""
    from .knowledge import normalize as words_of

    return words_of(text)


def _says_a_number(text: str) -> bool:
    """A sentence with a number in it: digits, an amount or a count (to_digits
    reads one), or a part number read aloud — not «طرمبة مية» (water)."""
    return any(c.isdigit() for c in text) or to_digits(text) != text or bool(spoken_codes(text))


def sentence_lesson(heard: str, said: str) -> Optional[tuple]:
    """(the sentence as these ears wrote it, what the teacher heard) when the
    pair can teach a whole sentence — never one with a number in it."""
    key, means = sentence_key(heard), " ".join((said or "").split())
    if not key or not sentence_key(means):
        return None
    if _says_a_number(key) or _says_a_number(sentence_key(means)):
        return None
    return key, means


def sentence(text: str, lessons: dict) -> Optional[str]:
    """What the teacher heard when these ears wrote `text` before, or None.
    `lessons`: {sentence_key: what was said}."""
    return lessons.get(sentence_key(text)) if lessons else None


def apply(text: str, fixes: list):
    """(the text with each lesson applied, the lessons used). `fixes`:
    [{"heard", "means"}], the longest first."""
    words = normalize(text).split()
    if not words or not fixes:
        return text, []
    lessons = sorted(((f.get("heard") or "").split(), (f.get("means") or "").split()) for f in fixes)
    lessons = sorted((p for p in lessons if p[0] and p[1]), key=lambda p: -len(p[0]))
    out, used, i = [], [], 0
    while i < len(words):
        for wrote, meant in lessons:
            if words[i:i + len(wrote)] == wrote:
                out += meant
                used.append((" ".join(wrote), " ".join(meant)))
                i += len(wrote)
                break
        else:
            out.append(words[i])
            i += 1
    return (" ".join(out) if used else text), used
