"""How these ears hear the robot's name, learned on this computer.

Vosk (Kaldi) writes only words it knows, and «موس» isn't one: «يا موس» came
out as «يمس», «انظر إلى كابوس», «حيث» — but the same few, for the same voice.
`python -m brain wake` has «يا موس» said a few times and keeps what was
heard (home/wake_heard.txt); at the START of a sentence, those words are
taken for the name from then on. Never in the middle, and never a lone
everyday word that starts sentences anyway («في», «عندك», «هات»…).
"""

from pathlib import Path

from .knowledge import normalize

FILE = "wake_heard.txt"
TIMES = 6                                    # «يا موس» said this many times
LONGEST = 3                                  # words: more than that, they said more than the name

# Everyday words a sentence starts with: never the name on their own.
COMMON = frozenset("""
    في من على علي الي الى انا انت انتي هو هي احنا ده دي دا يا و لا لاء ايوه اه ايه
    عندك عندكم فيه هات بكام كام مين امتي فين ازاي ليه لو طيب طب ماشي تمام شكرا
    السلام عليكم صباح مساء اهلا ممكن عايز عاوز
""".split())


def _path(home) -> Path:
    return Path(home) / FILE


def load(home) -> list:
    """The learned spellings, as word lists, longest first."""
    try:
        lines = _path(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seqs = [line.split() for line in (normalize(x) for x in lines) if line]
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
    if all(w in COMMON for w in words):
        return None
    return " ".join(words)


def save(home, heard: list, name: str) -> list:
    """Add what was heard to what was learned before; every spelling kept."""
    kept = [k for k in (keepable(h, name) for h in heard) if k]
    old = [" ".join(seq) for seq in load(home)]
    every = list(dict.fromkeys(old + kept))
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in every), encoding="utf-8")
    return every


def forget(home) -> None:
    try:
        _path(home).unlink()
    except OSError:
        pass


def as_named(text: str, learned: list, name: str):
    """(True, «يا موس …») when `text` starts with a learned spelling of the
    name; (False, text) otherwise."""
    raw = (text or "").split()
    norm = [normalize(w) for w in raw]
    for seq in learned:
        if norm[:len(seq)] == seq:
            rest = " ".join(raw[len(seq):]).strip(" ،,.!؟?:-")
            return True, f"يا {name} {rest}".strip()
    return False, text
