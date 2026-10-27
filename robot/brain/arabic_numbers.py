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

A part number read aloud is something else again (`spoken_codes`): digit by
digit, «واحد واحد اربعة اتنين…», or in the groups it's printed in — every
number word a digit or a group there, with no money rules.
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
    # 🐛 [FIX]: «واحد» اتقرت «و» + «احد»: «واحد واحد اربعة…» (رقم قطعة بيتقال رقم رقم) بقت
    # «1 واحد 4 …». كلمة هي نفسها رقم مش متوصّلة بـ «و».
    if len(w) > 1 and w.startswith("و") and _kind(w)[0] is None and _kind(w[1:])[0] is not None:
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


# ---------------------------------------------------------------------------
# A part number read aloud
# ---------------------------------------------------------------------------
# BMW numbers are read digit by digit («واحد واحد اربعة اتنين سبعة تسعة خمسة
# تلاتة واحد اتنين خمسة») or in the groups they're printed in («حداشر اتنين
# واربعين سبعة تسعمية تلاتة وخمسين مية خمسة وعشرين»: 11 42 7 953 125). Vosk
# writes them in words, and the part was looked for by «واحد»، «اربعة»… —
# never found. Here every number word is a digit or a group.
ZERO = _table({"صفر زيرو": 0})
CODE_FEWEST = 5             # a code, not a count: robot/services.match_part_in_sentence
CODE_IN_TEXT = 7            # written as one in a sentence (codes_to_digits): never an amount
_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_TEEN_TAIL = {"عشر", "عشره"}                       # «أحد عشر»، «ثلاثة عشر»: news Arabic


def _code_kind(w: str):
    """(kind, value) of a word in a part number read aloud, or (None, None)."""
    if w in ZERO:
        return "unit", 0
    if w in UNITS:
        return ("teen", 10) if UNITS[w] == 10 else ("unit", UNITS[w])
    for table, kind in ((TEENS, "teen"), (TENS, "ten"), (HUNDREDS, "hundreds"), (HUNDRED, "hundreds")):
        if w in table:
            return kind, table[w]
    if w.isdigit():
        return "digits", w
    return None, None


def is_number_word(word: str) -> bool:
    """A number however it's said — «واحد», «تلات», «ميتين», «وخمسين», «12»."""
    w = _norm(word).translate(_INDIC)
    if w.startswith("و") and len(w) > 1 and _code_kind(w)[0] is None and _kind(w)[0] is None:
        w = w[1:]
    return (_code_kind(w)[0] is not None or _kind(w)[0] is not None
            or any(c.isdigit() for c in w))


def _code_runs(words: list) -> list:
    """[(the digits, its groups, the index of its first word, the index after
    it)] of each run of number words in `words` (normalized), read as a part
    number is. Digits already written end a run: «2 واحد واحد اربعة…» is two
    of 114…, not 2114…"""
    runs, groups, value, last, start = [], [], None, None, None

    def close():
        nonlocal value, last
        if value is not None:
            groups.append(str(value))
        value, last = None, None

    i, n = 0, len(words)
    while i <= n:
        w = words[i] if i < n else ""
        kind, num = _code_kind(w)
        joined, step = False, 1
        if kind is None and value is not None:
            if w == "و" and i + 1 < n and _code_kind(words[i + 1])[0] not in (None, "digits"):
                kind, num = _code_kind(words[i + 1])
                joined, step = True, 2
            elif len(w) > 1 and w.startswith("و") and _code_kind(w[1:])[0] not in (None, "digits"):
                kind, num = _code_kind(w[1:])
                joined = True
        elif kind == "unit" and 0 < num < 10 and i + 1 < n and words[i + 1] in _TEEN_TAIL:
            kind, num, step = "teen", num + 10, 2                      # «ثلاثة عشر»: 13
        elif kind is None and w in SHORT and SHORT[w] < 10 and i + 1 < n and words[i + 1] in _TEEN_TAIL:
            kind, num, step = "teen", SHORT[w] + 10, 2                 # «أحد عشر»: 11
        if kind in (None, "digits"):
            close()
            if groups:
                runs.append(("".join(groups), list(groups), start, i))
            groups.clear()
            start = None
            i += 1
            continue
        if start is None:
            start = i
        if joined and (kind == "ten" and last in ("unit", "hundreds") and value % 100 < 10
                       or kind in ("unit", "teen") and last == "hundreds"
                       or kind == "unit" and last == "ten"):
            value, last = value + num, kind                            # «تلاتة وخمسين»: 53
        elif not joined and last == "hundreds" and kind in ("unit", "teen", "ten"):
            value, last = value + num, kind                            # «تسعمية تلاتة…»: 903…
        else:
            close()
            value, last = num, kind
        i += step
    return runs


def _a_code(digits: str, groups: list) -> bool:
    """Read as a part number: five digits or more, said in three groups or
    more («بين ميتين تلاتمية» is a price), or seven digits and more."""
    return len(digits) >= CODE_IN_TEXT or len(digits) >= CODE_FEWEST and len(groups) >= 3


def _after_a_count(digits: str, groups: list) -> str:
    """The code without the count said right before it — «اتنين واحد واحد
    اربعة…» is 2 of 114… — or '' when what's left isn't a code."""
    rest = digits[len(groups[0]):] if len(groups) > 1 and len(groups[0]) <= 2 else ""
    return rest if len(rest) >= CODE_IN_TEXT else ""


def spoken_codes(text: str) -> list:
    """The part numbers read aloud in a sentence, as digits — each run of
    number words that reads as one (and without a count said before it), and
    all of them together when a pause split one («… اتنين، يعني سبعة …»).
    «عندك تلاتة فلتر؟» has none."""
    words = [_norm(w).translate(_INDIC) for w in (text or "").split()]
    runs = _code_runs(words)
    codes = []
    for digits, groups, _start, _end in runs:
        for code in (digits if _a_code(digits, groups) else "", _after_a_count(digits, groups)):
            if code and code not in codes:
                codes.append(code)
    together = "".join(digits for digits, _groups, _start, _end in runs)
    if len(runs) > 1 and len(together) >= CODE_IN_TEXT and together not in codes:
        codes.append(together)
    return codes


def codes_to_digits(text: str, known=None) -> str:
    """The text with each part number read aloud (CODE_IN_TEXT digits or more)
    written as one: «بعت واحد واحد اربعة … كاش» → «بعت 11427953125 كاش».
    `known(code)`: whether a part has that number — a count said right before
    it («بعت اتنين واحد واحد اربعة …») stays a count when only the rest is one.
    Amounts stay words, for to_digits."""
    raw = (text or "").split()
    words = [_norm(w).translate(_INDIC) for w in raw]
    out, i = [], 0
    for digits, groups, start, end in _code_runs(words):
        if len(digits) < CODE_IN_TEXT:
            continue
        said, rest = digits, _after_a_count(digits, groups)
        if rest and known is not None and not known(digits) and known(rest):
            said = f"{groups[0]} {rest}"
        out += raw[i:start] + [said + raw[end - 1][len(raw[end - 1].rstrip(_EDGES)):]]
        i = end
    return " ".join(out + raw[i:])


def said_digits(text: str) -> str:
    """Every digit said in `text`, in order — digits as written, number words
    read the way a number is read aloud: «صفر عشرة اتنين اتنين…» → «01022…»
    (a mobile), «واحد واحد اربعة…» → «114…»."""
    words = [_norm(w).translate(_INDIC) for w in (text or "").split()]
    found = [(i, w) for i, w in enumerate(words) if w.isdigit()]
    found += [(start, digits) for digits, _groups, start, _end in _code_runs(words)]
    return "".join(digits for _i, digits in sorted(found))
