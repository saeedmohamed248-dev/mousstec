"""«🎓 تعلّم السمع»: what these ears write, and what was said.

When the robot isn't sure what it heard, a «teacher» (Azure's or Google's
speech-to-text, on the server: robot/teachers.py) hears the same recording.
Where the two differ by a word or three — «فلترا الزي» / «فلتر زيت» — that's
a lesson; seen twice, the server sends it with the catalog and the robot
writes what was said from then on, the net up or down.

Never a number: «ميتين» heard as «مية» is a price, and a wrong lesson there
is wrong money. Never the little words a question is made of.
"""

from difflib import SequenceMatcher

from .arabic_numbers import to_digits
from .replies import LITTLE, normalize

MOST_WORDS = 3             # a lesson is a word or a few, not a sentence
# A mishearing sounds like what was said: «فلترا الزي» / «فلتر زيت», not
# «كابوس» / «فلتر هوا» (the teacher heard something else there).
SOUNDS_LIKE = 0.5


def _is_number(words: list) -> bool:
    said = " ".join(words)
    return any(c.isdigit() for c in said) or to_digits(said) != said


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
        if SequenceMatcher(a=wrote, b=meant, autojunk=False).ratio() < SOUNDS_LIKE:
            continue
        out.append((wrote, meant))
    return out


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
