"""How these ears hear the robot's name, learned on this computer.

Vosk (Kaldi) writes only words it knows, and «موس» isn't one: «يا موس» came
out as «يمس», «انظر إلى كابوس», «حيث» — but the same few, for the same voice.
`python -m brain wake` has «يا موس» said a few times and keeps what was
heard the same way twice or more (home/wake_heard.txt) — once is chance;
at the START of a sentence, those words are
taken for the name from then on. Never in the middle, never a lone
everyday word that starts sentences anyway («في», «عندك», «هات»…), and never
someone else's name: «احمد», «مصطفي» start «احمد، هات الفلتر» to a colleague.
"""

from collections import Counter
from pathlib import Path

from .knowledge import normalize

FILE = "wake_heard.txt"
TIMES = 6                                    # «يا موس» said this many times
LONGEST = 3                                  # words: more than that, they said more than the name
# 🐛 [FIX]: على اللاب اتقال «يا موس» عشر مرات واتكتب ٨ كلمات مختلفة («الله»، «اسال»، «النص»…) واتحفظوا
# كلهم — فأي جملة بتبدأ بـ «الله يخليك» بقت للروبوت. الكلمة اللي اتسمعت مرة صدفة؛ مرتين = دي طريقة
# الودان دي في كتابة الاسم.
AGREE = 2                                    # the same spelling this many times in one round
FEWEST_LETTERS = 3                           # «مو»: too little to tell the name by

# Everyday words a sentence starts with: never the name on their own.
COMMON = frozenset("""
    في من على علي الي الى انا انت انتي هو هي احنا ده دي دا يا و لا لاء ايوه اه ايه
    عندك عندكم فيه هات بكام كام مين امتي فين ازاي ليه لو طيب طب ماشي تمام شكرا
    السلام عليكم صباح مساء اهلا ممكن عايز عاوز الله والله اقصد قصدي اسال بص شوف خد خلي
""".split())

# The names people in a shop call each other by (normalized): never the robot's.
PEOPLE = frozenset("""
    محمد احمد محمود مصطفي علي حسن حسين ابراهيم اسماعيل عمر عمرو خالد يوسف كريم طارق سيد
    سعيد عبده عبدالله عبدالرحمن عادل جمال ياسر هاني وليد شريف ايمن ايهاب اسلام سامح سامي
    رامي مينا جرجس بيتر مايكل ابانوب عماد عصام اشرف حمدي مجدي فتحي رضا صلاح ناصر حازم
    تامر شادي مدحت هشام ماجد مروان زياد ادهم انس بلال حمزه نور فاطمه مريم منه ساره ايه
    هبه دينا رانيا امل مني نهي اسامه عاطف رمضان شعبان سمير منير نبيل جمعه حماده
""".split())


def _path(home) -> Path:
    return Path(home) / FILE


def _someone(word: str) -> bool:
    """«احمد», and «يا احمد» as Kaldi runs it together: «يحمد», «ياحمد»."""
    for rest in (word, word[2:] if word.startswith("يا") else "", word[1:] if word.startswith("ي") else ""):
        if rest and (rest in PEOPLE or "ا" + rest in PEOPLE):
            return True
    return False


def load(home) -> list:
    """The learned spellings, as word lists, longest first. 🐛 [FIX]: a name
    kept before this rule («احمد») is left out here too, so the robot stops
    answering colleagues without the shop having to learn the name again."""
    try:
        lines = _path(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seqs = [line.split() for line in (normalize(x) for x in lines) if line]
    seqs = [seq for seq in seqs if not any(_someone(w) for w in seq)]
    return sorted(seqs, key=len, reverse=True)


def keepable(heard: str, name: str):
    """What was heard for a bare «يا موس», normalized — or None: nothing,
    the name itself (understood already), a whole sentence, or only
    everyday words."""
    words = normalize(heard).split()
    if not words or len(words) > LONGEST:
        return None
    base = normalize(name)
    if base in words or f"يا{base}" in words:     # the name itself: understood as it is
        return None
    if all(w in COMMON for w in words) or len("".join(words)) < FEWEST_LETTERS:
        return None
    if any(_someone(w) for w in words):       # someone else's name: a colleague called
        return None
    return " ".join(words)


def _counted(heard: list, name: str) -> Counter:
    return Counter(k for k in (keepable(h, name) for h in heard) if k)


def save(home, heard: list, name: str, others=()) -> list:
    """Add what was heard to what was learned before: a spelling heard AGREE
    times in this round, but not one heard when other people were called
    (`others`): those are their names as these ears write them, not the
    robot's."""
    theirs = {" ".join(normalize(o).split()) for o in others} - {""}
    kept = [k for k, n in _counted(heard, name).items() if n >= AGREE]
    old = [" ".join(seq) for seq in load(home)]
    every = [k for k in dict.fromkeys(old + kept) if k not in theirs]
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in every), encoding="utf-8")
    return every


def once(heard: list, name: str) -> list:
    """The spellings heard fewer than AGREE times in this round: chance, not kept."""
    return [k for k, n in _counted(heard, name).items() if n < AGREE]


def forget(home) -> None:
    try:
        _path(home).unlink()
    except OSError:
        pass


def as_named(text: str, learned: list, name: str):
    """(True, «يا موس …») when `text` starts — or ends — with a learned
    spelling of the name; (False, text) otherwise.

    🐛 [FIX]: «الموظفين يمس» («… يا موس», the name said last, the Egyptian
    way) was chatter: only a sentence starting with it was to the robot."""
    raw = (text or "").split()
    norm = [normalize(w) for w in raw]
    for seq in learned:
        if norm[:len(seq)] == seq:
            rest = " ".join(raw[len(seq):]).strip(" ،,.!؟?:-")
            return True, f"يا {name} {rest}".strip()
    for seq in learned:
        if len(norm) > len(seq) and norm[-len(seq):] == seq:
            rest = " ".join(raw[:-len(seq)]).strip(" ،,.!؟?:-")
            return True, f"يا {name} {rest}".strip()
    return False, text
