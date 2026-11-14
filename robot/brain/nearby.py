"""The part that was nearly said — for ears that write a word a letter off.

With the net down the robot hears with Vosk alone, and Vosk writes Egyptian the way a newsreader
would: «فلترا الزي» for «فلتر زيت», «طلمبه» for «طرمبة», «تيل فرامن» for «تيل فرامل». The
catalog's own search wants the words as the shop wrote them, so a part on the shelf came back
«ملقتش القطعة دي» for one letter. Here every part's name is scored against what was heard, letter
by letter:

    the same word (or a form of it the shop's slang knows)          1.0
    a letter or two off, once both are written the way they sound   0.7   («طلمبه»/«طرمبه»)
    the start of the word, a letter short («زي» for «زيت»)          0.5

What it finds is only ever *offered* — «تقصد فلتر زيت؟» — never answered as if it were heard:
a price is said once they say yes (robot/brain/talk.pick_choice). And never online, where a
teacher hears the recording instead (Knowledge.understands doesn't count these).
"""

# Letters Vosk (and people) swap: written the way they sound in Egyptian.
_SOUNDS = str.maketrans({
    "ط": "ت", "ض": "د", "ص": "س", "ث": "س", "ذ": "ز", "ظ": "ز", "ق": "ك", "ج": "ج", "غ": "ع",
    "ة": "ه", "ى": "ي", "أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و", "ئ": "ي", "ء": "",
})
SAME, NEAR, START = 1.0, 0.7, 0.5
ENOUGH = 1.2                       # a part's score to be offered: a word and a bit, not one loose letter
SHOWN = 3


def sounds(word: str) -> str:
    """`word` as it sounds: the swapped letters as one, doubled letters once, a final «ه» let go."""
    w = (word or "").translate(_SOUNDS)
    out = []
    for ch in w:
        if not out or out[-1] != ch:
            out.append(ch)
    w = "".join(out)
    return w[:-1] if len(w) > 3 and w.endswith("ه") else w


def off_by(a: str, b: str, most: int) -> int:
    """Letters to change, add or drop to turn `a` into `b` — `most + 1` once it's more than `most`."""
    if abs(len(a) - len(b)) > most:
        return most + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(row) > most:
            return most + 1
        prev = row
    return prev[-1]


def alike(said: str, named: str) -> float:
    """How much the word heard is the word in a part's name: SAME, NEAR, START or 0."""
    if said == named:
        return SAME
    a, b = sounds(said), sounds(named)
    if a == b and len(a) >= 2:
        return SAME
    if len(a) >= 4 and len(b) >= 4 and off_by(a, b, 1 if min(len(a), len(b)) <= 5 else 2) <= (
            1 if min(len(a), len(b)) <= 5 else 2):
        return NEAR
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 2 and len(long_) - len(short) == 1 and long_.startswith(short):
        return START
    return 0.0


def score(keys: list, name_words, forms=lambda w: {w}) -> float:
    """What was heard against one part's name: each word heard counts once, at its best."""
    total = 0.0
    for key in keys:
        best = 0.0
        for form in forms(key):
            for word in name_words:
                best = max(best, alike(form, word))
                if best == SAME:
                    break
        total += best
    return total


def nearest(keys: list, parts, forms=lambda w: {w}, shown: int = SHOWN) -> list:
    """The parts nearest what was heard, best first — [] when none is near enough. A part whose
    every word was heard right isn't this module's to find (the catalog's own search did)."""
    keys = [k for k in dict.fromkeys(keys) if len(k) >= 2][:8]
    if not keys:
        return []
    found = []
    for part in parts:
        if not getattr(part, "active", True):
            continue
        got = score(keys, part.name_words, forms)
        if got < ENOUGH:
            continue
        # most of what was said has to be in the name, and most of the name in what was said
        if got < 0.6 * len(keys) or got < 0.5 * len(part.name_words):
            continue
        found.append((got - 0.05 * len(part.name_words), -(part.stock or 0), part))
    found.sort(key=lambda f: (-f[0], f[1]))
    if not found:
        return []
    best = found[0][0]
    return [part for value, _stock, part in found if value >= best - 0.3][:shown]
