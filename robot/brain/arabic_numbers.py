"""Amounts and counts said in words → digits, the way the books need them.

Vosk writes what it hears in words — «مية وخمسين جنيه», «ألفين وخمسمية»,
«مائتين» (it was trained on news Arabic) — and the voice entries read only
digits: an expense said in words came back «قول المصروف بالمبلغ». Egyptian
and news Arabic both:

    «مية وخمسين» 150 · «ألفين وخمسمية» 2500 · «ألف ونص» 1500 · «مية وربع» 125
    «تلات تلاف» 3000 · «خمسة وعشرين» 25 · «خمستاشر» / «خمسة عشر» 15 · «2 ألف» 2000

A price joined to «ب» is one too: «بميتين» → «بـ 200».

Words that are something else as well are numbers only where a number is
said: «طرمبة مية» is a water pump («مصروف مية» the water bill), «تمن» a
price, «ست» a lady, «فلتر واحد» one of it — «مية» counts beside another
number, «جنيه» or «بسعر», «واحد» beside another number or «جنيه», the short
«خمس», «ست»… only before «مية» / «تلاف» / «عشر».
"""

import re

_TASHKEEL = re.compile(r"[ً-ْٰ]")
_EDGES = " ،,.؟?!:-()«»\"'"


def _norm(word: str) -> str:
    w = _TASHKEEL.sub("", word or "").strip(_EDGES)
    return (w.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
             .replace("ى", "ي").replace("ة", "ه").replace("ـ", ""))


def _table(spec: dict) -> dict:
    return {_norm(w): v for words, v in spec.items() for w in words.split()}


UNITS = _table({"واحد واحده": 1, "اتنين اثنين اثنان": 2, "تلاته ثلاثه": 3, "اربعه": 4, "خمسه": 5,
                "سته": 6, "سبعه": 7, "تمانيه ثمانيه": 8, "تسعه": 9, "عشره": 10})
# Only before «مية» / «تلاف» / «عشر» («خمس تلاف»، «أحد عشر»): alone they're other words too.
SHORT = _table({"احد": 1, "اثنا اثني": 2, "تلات ثلاث": 3, "اربع": 4, "خمس": 5, "ست": 6, "سبع": 7,
                "تمن ثمان تماني ثماني": 8, "تسع": 9, "عشر": 10})
TEENS = _table({"حداشر احداشر": 11, "اتناشر اطناشر": 12, "تلتاشر": 13, "اربعتاشر اربعطاشر": 14,
                "خمستاشر خمسطاشر": 15, "ستاشر سطاشر": 16, "سبعتاشر سبعطاشر": 17, "تمنتاشر تمانتاشر": 18,
                "تسعتاشر تسعطاشر": 19})
TENS = _table({"عشرين عشرون": 20, "تلاتين ثلاثين ثلاثون": 30, "اربعين اربعون": 40, "خمسين خمسون": 50,
               "ستين ستون": 60, "سبعين سبعون": 70, "تمانين ثمانين ثمانون": 80, "تسعين تسعون": 90})
HUNDREDS = _table({"ميتين مائتين مائتان مئتين مئتان": 200, "تلتميه ثلاثمائه ثلاثميه تلاتميه": 300,
                   "ربعميه اربعمائه اربعميه": 400, "خمسميه خمسمائه": 500, "ستميه ستمائه": 600,
                   "سبعميه سبعمائه": 700, "تمنميه ثمانمائه ثمنميه": 800, "تسعميه تسعمائه": 900})
HUNDRED = _table({"ميه مائه مئه": 100})               # «ميه» is water too: see _hundred_ok
THOUSAND = _table({"الف": 1000})
THOUSANDS = _table({"الاف تلاف ألاف": 1000})           # «تلات تلاف»: after a count
TWO_THOUSAND = _table({"الفين الفان": 2000})
# Said as one word: «خمستلاف».
THOUSANDS_WORD = _table({"تلاتلاف تلتلاف": 3000, "اربعتلاف ربعتلاف": 4000, "خمستلاف": 5000,
                         "ستلاف": 6000, "سبعتلاف": 7000, "تمنتلاف تمانتلاف": 8000, "تسعتلاف": 9000,
                         "عشرتلاف": 10000})
MILLION = _table({"مليون": 1_000_000})
TWO_MILLION = _table({"مليونين": 2_000_000})
HALF, QUARTER = {"نص", "نصف"}, {"ربع"}
CURRENCY = {_norm(w) for w in "جنيه جنية جنيها جنيهات ج egp pound pounds".split()}
# Not «مصروف»: «مصروف مية» is the water bill.
PRICE_WORDS = {_norm(w) for w in "بسعر سعر بـ ب باجمالي بإجمالي اجمالي إجمالي صرفنا صرفت "
               "دفعنا دفعت بمبلغ مبلغ".split()}
_MULTIPLIERS = set(HUNDRED) | set(THOUSAND) | set(THOUSANDS) | set(MILLION)


def _kind(w: str):
    for table, kind in ((UNITS, "unit"), (TEENS, "teen"), (TENS, "ten"), (HUNDREDS, "hundreds"),
                        (HUNDRED, "hundred"), (THOUSAND, "thousand"), (THOUSANDS, "thousands"),
                        (TWO_THOUSAND, "two_thousand"), (THOUSANDS_WORD, "thousands_word"),
                        (MILLION, "million"), (TWO_MILLION, "two_million")):
        if w in table:
            return kind, table[w]
    if w in SHORT:
        return "short", SHORT[w]
    if w in HALF:
        return "half", 0.5
    if w in QUARTER:
        return "quarter", 0.25
    if w.isdigit():
        return "digits", int(w)
    return None, None


def _joined(w: str):
    """«وخمسين» → «خمسين» (joined by و)."""
    if len(w) > 1 and w.startswith("و") and _kind(w[1:])[0] is not None:
        return w[1:]
    return None


def _run(words: list, i: int):
    """(value, the index after it) of the number said from words[i], or None."""
    n = len(words)
    total, current, scale, j, started = 0, 0, 1, i, False
    while j < n:
        raw = words[j]
        joined = False
        if started:
            inner = _joined(raw)
            if inner is not None:
                raw, joined = inner, True
            elif raw == "و" and j + 1 < n and _kind(words[j + 1])[0] is not None:
                j += 1
                raw, joined = words[j], True
        kind, value = _kind(raw)
        nxt = words[j + 1] if j + 1 < n else ""
        if kind is None:
            break
        if kind == "digits":
            # Digits stay as they are, except a count before «ألف» («2 ألف»).
            if started or nxt not in THOUSAND and nxt not in THOUSANDS and nxt not in MILLION:
                break
            current = value
        elif kind == "short":
            if raw == "عشر" and started and not joined and 0 < current < 10:
                current += 10                                   # «خمسة عشر»: 15
            elif (nxt in _MULTIPLIERS or nxt in ("عشر", "عشره") and value < 10) and (not started or joined):
                current += value                                # «خمس تلاف»، «أحد عشر»
            else:
                break
        elif kind in ("unit", "teen", "ten", "hundreds"):
            if value == 1 and not started and not _counted(words, j):
                break                                           # «فلتر واحد»: one of it, not «1»
            if raw == "عشره" and started and not joined and 0 < current < 10:
                current += 10                                   # «ست عشرة»: 16
            elif started and not joined:
                break                                           # another number: «2 فلتر 3 …»
            else:
                current += value
                scale = 100 if kind == "hundreds" else 1
        elif kind == "hundred":
            if not started and not _hundred_ok(words, j):
                break                                           # «طرمبة مية»: water
            if started and not joined and 0 < current < 10:
                current *= 100                                  # «خمس مية»
            elif started and not joined:
                break
            else:
                current += 100
            scale = 100
        elif kind in ("thousand", "thousands", "million"):
            if kind == "thousands" and not (started and 2 < current <= 10):
                break                                           # «تلاف» needs a count before it
            if started and joined:
                break
            total += (current or 1) * value
            current, scale = 0, value
        elif kind in ("two_thousand", "thousands_word", "two_million"):
            if started and not joined:
                break
            total += value
            scale = 1_000_000 if kind == "two_million" else 1000
        elif kind in ("half", "quarter"):
            if not (started and joined and scale >= 100):
                break                                           # «اتنين ونص» isn't money
            current += int(scale * value)
        started = True
        j += 1
    if not started:
        return None
    return total + current, j


def _and_more(words: list, j: int) -> bool:
    """A number said in words joins words[j]: «… وخمسين», «… و خمسين» — not «… و2 فلتر»."""
    nxt = words[j + 1] if j + 1 < len(words) else ""
    after = words[j + 2] if j + 2 < len(words) else ""
    joined = _joined(nxt) or (after if nxt == "و" else None)
    return joined is not None and _kind(joined)[0] not in (None, "digits")


def _counted(words: list, j: int) -> bool:
    """«واحد» is a number only in one: «واحد وعشرين», «واحد جنيه», «واحد مليون»."""
    nxt = words[j + 1] if j + 1 < len(words) else ""
    return nxt in CURRENCY or nxt in _MULTIPLIERS or _and_more(words, j)


def _hundred_ok(words: list, j: int) -> bool:
    """«ميه» alone is 100 only beside money: «… جنيه», «بسعر ميه», «ميه وخمسين»
    — never «طرمبة مية و2 فلتر»."""
    nxt = words[j + 1] if j + 1 < len(words) else ""
    prev = words[j - 1] if j > 0 else ""
    return (nxt in CURRENCY or nxt in THOUSAND or nxt in THOUSANDS or prev in PRICE_WORDS
            or _and_more(words, j))


def _priced(words: list, i: int):
    """«بميتين» / «بخمسين جنيه»: (value, the index after it) of a price joined
    to «ب», or None."""
    w = words[i]
    if len(w) < 3 or not w.startswith("ب") or _kind(w[1:])[0] in (None, "digits"):
        return None
    found = _run(words[:i] + ["ب", w[1:]] + words[i + 1:], i + 1)
    return (found[0], found[1] - 1) if found is not None else None


def to_digits(text: str) -> str:
    """The text with each amount or count said in words written as digits."""
    raw = (text or "").split()
    words = [_norm(w) for w in raw]
    out, i = [], 0
    while i < len(raw):
        found, said = _run(words, i), "{}"
        if found is None or found[1] <= i:
            found, said = _priced(words, i), "بـ {}"
        if found is not None and found[1] > i:
            value, end = found
            tail = raw[end - 1][len(raw[end - 1].rstrip(_EDGES)):]       # «…خمسين،» keeps its «،»
            out.append(said.format(value) + tail)
            i = end
        else:
            out.append(raw[i])
            i += 1
    return " ".join(out)
