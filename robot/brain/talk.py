"""What the robot keeps in mind from one sentence to the next: when a
sentence is a follow-up, and the words that answer it.

Shared on purpose: the server (robot/memory.py, the part's data from the ERP)
and the brain with the net down (knowledge.py, the part from the catalog on
disk) import this one file, so «التانية», «وبكام؟», «عليه ضمان؟» are
understood and answered word for word the same, net or not. No Django here.

* Only an answer picks one of the parts just offered («تقصد أنهي؟»): «E90»,
  «التانية», «بتاعة الهوا». A sentence with any other word in it is a new
  question.
* Only a sentence made of follow-up words is about the part just talked
  about. One that names something else — another part — is a new question,
  as before.
* Every answer is the part's own data — what the catalog doesn't say, the
  robot says it doesn't know. A price only ever the part's retail price.
"""

import re
from typing import Optional

from .knowledge import _ARABIC_DIGITS, NOT_HERE, YES, _said, normalize, part_words, pieces, priced

TTL = 120                            # seconds of conversation remembered

# By position, with the article: «التانية» picks the second, «فيه تاني؟»
# asks for another one.
ORDINALS = {
    "الاول": 0, "الاولي": 0, "الاوله": 0, "الاولاني": 0, "الاولانيه": 0, "اول": 0, "اولاني": 0,
    "التاني": 1, "التانيه": 1, "الثاني": 1, "الثانيه": 1,
    "التالت": 2, "التالته": 2, "الثالث": 2, "الثالثه": 2,
    "first": 0, "second": 1, "third": 2,
}
# A car model said on its own — a BMW/MINI chassis (e90, f30, g20, r56,
# u11), a model (x5, m3, z4, i3) — or a year. Not any short code: «QQ999»
# is a part number, a new question.
_MODEL = re.compile(r"^(?:[efgru]\d{2}[a-z]?|[xmzi]\d|(?:19|20)\d{2})$")
# What a follow-up is made of (normalized, «ال» dropped). Anything else
# names a part: a new question.
FILLER = frozenset("""
    يا طب طيب و ال ده دي دا دول هو هي هوه منه منها منهم فيه في فيها كام بكام سعر سعره سعرها
    تمن تمنه تمنها عليه عليها ليه ليها له لها هل ايه اي انهي ازاي قد كده بقي بس اه ايوه
    لا ولا ممكن لو سمحت من فضلك عندك عندكم عندكو موجود موجوده متوفر متوفره متاح قطعه
    حته قطع حاجه واحد واحده بتاع بتاعه بتاعها بتاعت ماركه ماركته ماركتها نوع نوعه
    نوعها اصلي تقليد تجاري صناعه صنع حالته حالتها جديد جديده مستعمل استيراد ضمان
    ضمانه ضمانها مضمون بديل بدايل بديله غيره غيرها تاني تانيه ارخص اغلي احسن كمان
    اول اولي اوله اولاني اولانيه تالت تالته ثاني ثانيه ثالث ثالثه
    فين مكان مكانه مكانها رف حاطينه حاطينها بيركب يركب تركب بتركب اركبه اركبها ركب
    ينفع تنفع بينفع مناسب مناسبه علي عربيه عربيتي لعربيتي لعربيه موديل سنه
    فرع فروع فرعكم تانيين التانيين branch branches which
    بي ام دبليو bmw ميني mini
    how much price is it its there any other cheaper warranty brand where fit fits does
    the one a an for my car please first second third
""".split())
_PRICE_WORDS = frozenset("""
    كام بكام سعر سعره سعرها تمن تمنه تمنها ده دي دا موجود موجوده متوفر متوفره متاح
    عندك عندكم عندكو price much
""".split())
_KINDS = (                           # on the words joined; the first match wins
    ("branches", re.compile(r"(?:^|\s)و?(?:فرع\w*|فروع|branch\w*)(?:\s|$)")),
    ("where", re.compile(r"(?:^|\s)و?(?:فين|مكان\w*|رف|حاطين\w*|where)(?:\s|$)")),
    ("warranty", re.compile(r"(?:^|\s)و?(?:ضمان\w*|مضمون|warranty)(?:\s|$)")),
    ("fits", re.compile(r"(?:^|\s)و?(?:بيركب|يركب|تركب|بتركب|اركب\w*|ركب|ينفع|تنفع|بينفع|مناسب\w*|fits?)(?:\s|$)")),
    ("cheaper", re.compile(r"(?:^|\s)و?(?:ارخص|cheaper)(?:\s|$)")),
    ("alternatives", re.compile(r"(?:^|\s)و?(?:بديل\w*|بدايل|غيره|غيرها|other)(?:\s|$)"
                                r"|(?:^|\s)(?:فيه|حاجه|نوع)\s+(?:حاجه\s+|نوع\s+)?تاني\w*")),
    ("brand", re.compile(r"(?:^|\s)و?(?:مارك\w*|نوع\w*|اصلي|تقليد|تجاري|صناع\w*|صنع|حالت\w*|جديد\w*|مستعمل"
                         r"|استيراد|brand)(?:\s|$)")),
)


def is_model(word: str) -> bool:
    return bool(_MODEL.match(word or ""))


def _plain(word: str) -> str:
    """«وده» → «ده», «وعليه» → «عليه»: a leading «و» (and) isn't a new word."""
    return word[1:] if word.startswith("و") and word[1:] in FILLER else word


def _filler(word: str) -> bool:
    return _plain(word) in FILLER


def models_said(text: str) -> list:
    return [w.upper() for w in part_words(text) if is_model(w) and not w.isdigit()]


def pick_choice(text: str, choices: list) -> Optional[int]:
    """Which of `choices` ([{"name", "model"}], as offered) an answer to
    «تقصد أنهي؟» picks — its index, or None."""
    if not choices:
        return None
    words = part_words(text)
    sets = [set(part_words(c.get("name", ""))) | set(part_words(c.get("model", ""))) for c in choices]
    known = set().union(*sets)
    if not words or any(not (_filler(w) or w in known or is_model(w)) for w in words):
        return None
    for word in normalize(text).split():
        word = word[1:] if word.startswith("و") and word[1:] in ORDINALS else word
        if word in ORDINALS and ORDINALS[word] < len(choices):
            return ORDINALS[word]
    # The words that tell the offered parts apart («E90», «الهوا»): the one
    # part that has all of them.
    told = {w for w in words if not _filler(w) or w in known}
    told = {w for w in told if not all(w in s for s in sets)}
    fits = [i for i, s in enumerate(sets) if told and told <= s]
    return fits[0] if len(fits) == 1 else None


def follow_up(text: str) -> Optional[str]:
    """What a follow-up asks about the part just talked about — where,
    warranty, fits, cheaper, alternatives, brand, price — or None when the
    sentence isn't one."""
    words = part_words(text)
    if not words or any(not (_filler(w) or is_model(w)) for w in words):
        return None
    joined = " ".join(words)
    for kind, pattern in _KINDS:
        if pattern.search(joined):
            return kind
    if models_said(text):
        return "fits"                # «وعلى F30؟»
    if any(_plain(w) in _PRICE_WORDS for w in words):
        return "price"               # «وبكام؟», «وده؟», «موجود؟»: the part, its price, its stock
    return None


# ---------------------------------------------------------------------------
# The answers: (what is said, the same cut into voice-bank clips)
# ---------------------------------------------------------------------------

ON_SHELF = "في الرف"
NO_PLACE = "مش متسجل مكانه، اسأل حد من الموظفين."
WARRANTY = "عليه ضمان"
AND_WARRANTY = "وعليه ضمان"
NO_WARRANTY = "مفيش ضمان متسجل على"
ASK_STAFF = "اسأل حد من الموظفين."
BRAND = "ماركة"
NO_BRAND = "ماركته مش متسجلة"
FITS = "بيركب على"
FITS_ONLY = "مسجل إنه بيركب على"
CHECK_CHASSIS = "بس. اتأكد من رقم الشاسيه مع الموظف."
FITS_UNKNOWN = "التوافق مش متسجل لـ"
BRING_CHASSIS = "اسأل حد من الموظفين ومعاك رقم الشاسيه."
ALSO = "فيه كمان:"
NO_CHEAPER = "مفيش حاجة أرخص منه موجودة دلوقتي."
NO_ALTERNATIVE = "مفيش بديل لـ"
AVAILABLE_NOW = "موجود دلوقتي."
CONDITIONS = {"new": "جديد", "used": "استيراد مستعمل", "core": "تالف للتجديد"}
PHRASES = [ON_SHELF, NO_PLACE, WARRANTY, AND_WARRANTY, NO_WARRANTY, ASK_STAFF, BRAND, NO_BRAND,
           FITS, FITS_ONLY, CHECK_CHASSIS, FITS_UNKNOWN, BRING_CHASSIS, ALSO, NO_CHEAPER,
           NO_ALTERNATIVE, AVAILABLE_NOW] + list(CONDITIONS.values())
SHOWN_MODELS = 5                     # models said in one answer
SHOWN_ALTERNATIVES = 3


def months(n: int) -> str:
    """«شهر», «شهرين», «6 شهور», «سنة», «سنتين», «18 شهر»."""
    n = int(n)
    return {1: "شهر", 2: "شهرين", 12: "سنة", 24: "سنتين"}.get(n) or (f"{n} شهور" if n <= 10 else f"{n} شهر")


def models_of(chassis, car_model: str) -> list:
    """What the catalog says the part fits: its chassis list, then the
    models written in its car-model text («E90 / E91 2008»)."""
    out = []
    values = chassis if isinstance(chassis, (list, tuple)) else ([chassis] if isinstance(chassis, str) else [])
    for value in values:
        value = str(value or "").strip().upper()
        if value and value not in out:
            out.append(value)
    for token in re.split(r"[\s,،/|\-]+", car_model or ""):
        token = token.strip().upper()
        if token and token not in out and is_model(token.lower()) and not token.isdigit():
            out.append(token)
    return out


def where(name: str, shelf: str, stock: int) -> tuple:
    if stock <= 0:
        return f"{name} {NOT_HERE}.", [name, NOT_HERE]
    if shelf:
        return f"{name} {ON_SHELF} {shelf}.", [name, ON_SHELF, shelf]
    return f"{name} {NO_PLACE}", [name, NO_PLACE]


HAVE_HERE = "موجود هنا، عندي منه"
IN_BRANCH = "وفي فرع"
NOWHERE = "مش موجود في أي فرع دلوقتي."
BRANCHES_NEED_NET = "والفروع التانية محتاج النت علشان أعرفها."


def branches(name: str, stock: int, others) -> tuple:
    """«في أنهي فرع؟»: how many here, then in each other branch that has it.
    `others`: [(branch, quantity)] with stock — or None offline (this
    branch's catalog is all the brain has)."""
    if stock > 0:
        said, clips = [f"{name} {HAVE_HERE} {pieces(stock)}"], [name, HAVE_HERE, pieces(stock)]
    elif others or others is None:
        said, clips = [f"{name} {NOT_HERE}"], [name, NOT_HERE]
    else:
        return f"{name} {NOWHERE}", [name, NOWHERE]
    for branch, qty in others or []:
        said.append(f"{IN_BRANCH} {branch} {pieces(qty)}")
        clips += [IN_BRANCH, branch, pieces(qty)]
    if others is None:
        said.append(BRANCHES_NEED_NET[:-1])
        clips.append(BRANCHES_NEED_NET)
    return "، ".join(said) + ".", clips


def warranty(name: str, n: int) -> tuple:
    n = int(n or 0)
    if n > 0:
        return f"{name} {WARRANTY} {months(n)}.", [name, WARRANTY, months(n)]
    return f"{NO_WARRANTY} {name}. {ASK_STAFF}", [NO_WARRANTY, name, ASK_STAFF]


def brand(name: str, brand_name: str, condition: str, n: int) -> tuple:
    brand_name, n = (brand_name or "").strip(), int(n or 0)
    text, clips = (f"{name} {BRAND} {brand_name}", [name, BRAND, brand_name]) if brand_name \
        else (f"{name} {NO_BRAND}", [name, NO_BRAND])
    said = CONDITIONS.get(condition or "", "")
    if said:
        text, clips = text + f"، {said}", clips + [said]
    if n > 0:
        text, clips = text + f"، {AND_WARRANTY} {months(n)}", clips + [AND_WARRANTY, months(n)]
    return text + ".", clips


def fits(name: str, models: list, asked: list) -> tuple:
    if not models:
        return f"{FITS_UNKNOWN} {name}. {BRING_CHASSIS}", [FITS_UNKNOWN, name, BRING_CHASSIS]
    if asked and all(a in models for a in asked):
        return f"{YES} {name} {FITS} {'، '.join(asked)}.", [YES, name, FITS] + list(asked)
    shown = models[:SHOWN_MODELS]
    if asked:
        return (f"{name} {FITS_ONLY} {'، '.join(shown)} {CHECK_CHASSIS}",
                [name, FITS_ONLY] + shown + [CHECK_CHASSIS])
    return f"{name} {FITS} {'، '.join(shown)}.", [name, FITS] + shown


def alternatives(kind: str, name: str, price: float, alts: list) -> tuple:
    """«فيه بديل؟» / «فيه أرخص؟». `alts`: [(name, retail price)] in stock
    here, in the server's order. → (text, clips, the indices said)."""
    price = float(price or 0)
    picked = list(range(len(alts)))
    if kind == "cheaper":
        picked = [i for i in picked if price > 0 and 0 < float(alts[i][1] or 0) < price]
    # Cheapest first; one with no price set last (it can't be compared).
    picked.sort(key=lambda i: (float(alts[i][1] or 0) <= 0, float(alts[i][1] or 0)))
    picked = picked[:SHOWN_ALTERNATIVES]
    if not picked:
        if kind == "cheaper":
            return NO_CHEAPER, [NO_CHEAPER], []
        return f"{NO_ALTERNATIVE} {name} {AVAILABLE_NOW}", [NO_ALTERNATIVE, name, AVAILABLE_NOW], []
    said, clips = [], [ALSO]
    for i in picked:
        alt_name, alt_price = alts[i][0], float(alts[i][1] or 0)
        said.append(f"{alt_name} {priced(alt_price)}" if alt_price > 0 else alt_name)
        clips += [alt_name] + ([priced(alt_price)] if alt_price > 0 else [])
    return f"{ALSO} {'، '.join(said)}.", clips, picked


# ---------------------------------------------------------------------------
# «دوّر على النت …»: a question for the internet (robot/assistant.py)
# ---------------------------------------------------------------------------

# Said as a command, the verb first: «دوّر على النت على …», «شوفلي في جوجل
# …», «اسأل جوجل …». Without the net in it («دورلي على فلتر زيت») it's a
# question about the stock, as before.
_NET = r"(?:ال|عال|بال)?(?:نت|انترنت|جوجل|اونلاين|google|internet|online|web)"
_LOOK = (r"(?:ت|ن)?(?:دور|دورلي|دورلنا|ابحث|ابحثلي|ابحثلنا|شوف|شوفلي|شوفلنا|اعرف|اعرفلي|اعرفلنا"
         r"|اسال|search|look|check|google)")
_WEB = re.compile(rf"^{_LOOK}(?:\s+\S+)*?\s+{_NET}(?:\s|$)|^(?:اسال\s+)?جوجل(?:\s|$)|^google(?:\s|$)")
_STOCK = re.compile(r"(?:^|\s)و?(?:عندك|عندكم|عندكو|موجود|موجوده|متوفر|متوفره|have|stock)(?:\s|$)")


def web_question(text: str) -> bool:
    return bool(_WEB.search(_said(text)))


def about_the_stock(text: str) -> bool:
    """«عندك …؟», «موجود …؟»: asked of the shop, not of the internet."""
    return bool(_STOCK.search(" ".join(part_words(text))))


# ---------------------------------------------------------------------------
# «أبلغك أول ما ييجي؟»: a mobile number for a part that's out (robot/restock.py)
# ---------------------------------------------------------------------------

RESTOCK_ASK = "لو تحب أبلغك أول ما ييجي، قولي رقم موبايلك."
RESTOCK_SAVED = "تمام، سجّلت رقمك، وهبلّغك أول ما"
RESTOCK_ARRIVES = "يوصل."
RESTOCK_SAY_IT = "تمام، قولي رقم موبايلك."
RESTOCK_AGAIN = "الرقم ده مش مظبوط. قوله تاني: 11 رقم بيبدأ بـ 01."
PHRASES += [RESTOCK_ASK, RESTOCK_SAVED, RESTOCK_ARRIVES, RESTOCK_SAY_IT, RESTOCK_AGAIN,
            HAVE_HERE, NOWHERE, BRANCHES_NEED_NET]

# Digits said as words («صفر واحد صفر …»), normalized (ة→ه).
_DIGIT_WORDS = {"صفر": "0", "واحد": "1", "اتنين": "2", "اثنين": "2", "تلاته": "3", "ثلاثه": "3",
                "اربعه": "4", "خمسه": "5", "سته": "6", "سبعه": "7", "تمانيه": "8", "ثمانيه": "8",
                "تسعه": "9", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "oh": "0"}
_MOBILE = re.compile(r"^01[0125]\d{8}$")
_YES = frozenset("اه ايوه ايوا اكيد ماشي تمام ياريت ok okay yes sure".split())
_YES_TOO = frozenset("يا ريت طيب ممكن لو سمحت من فضلك عايز عاوز ابلغني بلغني please".split())


def _digits(text: str) -> str:
    out = []
    for word in normalize((text or "").translate(_ARABIC_DIGITS)).split():
        word = word[1:] if word.startswith("و") and word[1:] in _DIGIT_WORDS else word
        if word in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[word])
        elif word.isdigit():
            out.append(word)
    return "".join(out)


def mobile_in(text: str) -> str:
    """The Egyptian mobile said in `text` («010 1234 5678», «+20 10…»,
    «صفر واحد صفر …»), as 01XXXXXXXXX — or ''."""
    digits = _digits((text or "").replace("+", " "))
    if digits.startswith("0020"):
        digits = "0" + digits[4:]
    elif digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits if _MOBILE.match(digits) else ""


def tried_a_number(text: str) -> bool:
    """A mobile said wrong («010 123 456»). 🐛 [FIX]: any 7 digits counted, so
    «عندك 11427953125؟» right after «قولي رقم موبايلك» got «الرقم ده مش
    مظبوط» instead of that part's answer: only digits that start like a
    mobile do."""
    digits = _digits((text or "").replace("+", " "))
    return len(digits) >= 7 and digits.startswith(("01", "201", "00201"))


def just_yes(text: str) -> bool:
    """«آه», «أيوه بلغني», «ياريت»: yes, without the number yet."""
    words = normalize(text).split()
    return bool(words) and any(w in _YES for w in words) and all(w in _YES or w in _YES_TOO for w in words)


def restock_saved(name: str) -> tuple:
    return f"{RESTOCK_SAVED} {name} {RESTOCK_ARRIVES}", [RESTOCK_SAVED, name, RESTOCK_ARRIVES]
