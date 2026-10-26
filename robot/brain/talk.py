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
from .slang import FILLERS, forms

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
    عامل عامله عاملين حساب حسابه حسابها تطلع يطلع بقد لسه منه متاحه
    مضمونه بضمان
    صيني ياباني كوري الماني تركي هندي تايواني امريكي اوروبي وكاله اوريجينال اورجينال كوبي درجه فرز
    منشا بلد
    تمشي ماشيه يمشي تيجي ييجي تقضي يقضي مقاس مقاسها مقاسه مظبوط مظبوطه تظبط
    اشوف اشوفها اشوفه حاطط حاططها حاططه وريني وريهالي وريهولي
    اوفر اقل افضل زيها زيه شبهها شبهه
    how much price is it its there any other cheaper warranty brand where fit fits does
    the one a an for my car please first second third
""".split())
_PRICE_WORDS = frozenset("""
    كام بكام سعر سعره سعرها تمن تمنه تمنها ده دي دا موجود موجوده متوفر متوفره متاح
    عندك عندكم عندكو price much
    عامل عامله عاملين حساب حسابه حسابها تطلع يطلع بقد لسه متاحه
""".split())
_KINDS = (                           # on the words joined; the first match wins
    ("branches", re.compile(r"(?:^|\s)و?(?:فرع\w*|فروع|branch\w*)(?:\s|$)")),
    ("where", re.compile(r"(?:^|\s)و?(?:فين|مكان\w*|رف|حاطين\w*|حاطط\w*|اشوف\w*|وري\w*|where)(?:\s|$)")),
    ("warranty", re.compile(r"(?:^|\s)و?(?:ضمان\w*|مضمون\w*|بضمان|warranty)(?:\s|$)")),
    ("fits", re.compile(r"(?:^|\s)و?(?:بيركب|يركب|تركب|بتركب|اركب\w*|ركب|ينفع|تنفع|بينفع|مناسب\w*"
                        r"|تمشي|ماشيه|يمشي|تيجي|ييجي|تقضي|يقضي|مقاس\w*|مظبوط\w*|تظبط|fits?)(?:\s|$)")),
    ("cheaper", re.compile(r"(?:^|\s)و?(?:ارخص|اوفر|اقل|cheaper)(?:\s|$)")),
    ("alternatives", re.compile(r"(?:^|\s)و?(?:بديل\w*|بدايل|غيره|غيرها|زيها|زيه|شبهها|شبهه|احسن|افضل|اغلي|other)(?:\s|$)"
                                r"|(?:^|\s)(?:فيه|حاجه|نوع)\s+(?:حاجه\s+|نوع\s+)?تاني\w*")),
    ("brand", re.compile(r"(?:^|\s)و?(?:مارك\w*|نوع\w*|اصلي|تقليد|تجاري|صناع\w*|صنع|حالت\w*|جديد\w*|مستعمل"
                         r"|استيراد|صيني|ياباني|كوري|الماني|تركي|هندي|تايواني|امريكي|اوروبي|وكاله|اوريجينال"
                         r"|اورجينال|كوبي|درجه|فرز|منشا|بلد|brand)(?:\s|$)")),
)


def is_model(word: str) -> bool:
    return bool(_MODEL.match(word or ""))


def _plain(word: str) -> str:
    """«وده» → «ده», «وعليه» → «عليه»: a leading «و» (and) isn't a new word."""
    return word[1:] if word.startswith("و") and word[1:] in FILLER else word


def _filler(word: str) -> bool:
    # 🐛 [FIX]: «وبكام يا باشا؟»، «التانية يا ريس» مكانوش متفهمين كسؤال على نفس القطعة —
    #    «باشا» مش من كلام السؤال، فبقت سؤال جديد و«ملقتش القطعة دي».
    return _plain(word) in FILLER or word in FILLERS


def models_said(text: str) -> list:
    return [w.upper() for w in part_words(text) if is_model(w) and not w.isdigit()]


def pick_choice(text: str, choices: list) -> Optional[int]:
    """Which of `choices` ([{"name", "model"}], as offered) an answer to
    «تقصد أنهي؟» picks — its index, or None."""
    if not choices:
        return None
    # «تقصد طرمبة مية (E90)؟» — «أيوه»: the one offered.
    if len(choices) == 1 and just_yes(text):
        return 0
    words = part_words(text)
    sets = [set(part_words(c.get("name", ""))) | set(part_words(c.get("model", ""))) for c in choices]
    known = set().union(*sets)
    if not words or any(not (_filler(w) or _in(w, known) or is_model(w)) for w in words):
        return None
    for word in normalize(text).split():
        word = word[1:] if word.startswith("و") and word[1:] in ORDINALS else word
        if word in ORDINALS and ORDINALS[word] < len(choices):
            return ORDINALS[word]
    # The words that tell the offered parts apart («E90», «الهوا»): the one
    # part that has all of them.
    told = {w for w in words if not _filler(w) or _in(w, known)}
    told = {w for w in told if not all(_in(w, s) for s in sets)}
    fits = [i for i, s in enumerate(sets) if told and all(_in(w, s) for w in told)]
    return fits[0] if len(fits) == 1 else None


def _in(word: str, names: set) -> bool:
    """«الياباني» is in «فلتر زيت - JP», «بوش» in «… BOSCH …» (slang.BRANDS)."""
    return word in names or bool(forms(word) & names)


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


def branch_said(name: str) -> str:
    """A branch's name after «فرع»: «فرع المعادي» and «الفرع الرئيسي» lose their
    own «فرع» — never «في فرع فرع المعادي»."""
    return re.sub(r"^\s*(?:ال)?فرع\s+", "", str(name or "")).strip() or str(name or "").strip()


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
        branch = branch_said(branch)
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

# Digits said in English words; Arabic ones are read by arabic_numbers.said_digits.
_DIGIT_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "oh": "0"}
_MOBILE = re.compile(r"^01[0125]\d{8}$")
_YES = frozenset("اه ايوه ايوا اكيد ماشي تمام ياريت ok okay yes sure".split())
_YES_TOO = frozenset("يا ريت طيب ممكن لو سمحت من فضلك عايز عاوز ابلغني بلغني please".split())


def _digits(text: str) -> str:
    """The digits said, in order. 🐛 [FIX]: only a word per digit counted, so «صفر
    عشرة …» (010…) and «… تسعة وتسعين» lost digits: arabic_numbers.said_digits
    reads them the way a number is read aloud."""
    from .arabic_numbers import said_digits

    words = [_DIGIT_WORDS.get(w, w) for w in normalize((text or "").translate(_ARABIC_DIGITS)).split()]
    return said_digits(" ".join(words))


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


# ---------------------------------------------------------------------------
# The customer's car: said («عندكم قطع بي ام؟»، «عربيتي F30») or the car of the
# part they took — so «عندك طرمبة مية؟» right after it is the pump for that car,
# not every pump in the shop. Kept with the conversation; a car said again
# replaces it. Only ever a tie-break: the words said pick the part first.
# ---------------------------------------------------------------------------

# How a make is said (normalized: ة→ه، ى→ي، أ/إ→ا) → its key. Longest first.
_MAKE_WORDS = {
    "bmw": "bmw", "بي ام دبليو": "bmw", "بي ام دابليو": "bmw", "بي ام": "bmw", "بيمر": "bmw", "بيمو": "bmw",
    "mini": "mini", "ميني كوبر": "mini", "ميني": "mini",
    "mercedes": "mercedes", "benz": "mercedes", "مرسيدس بنز": "mercedes", "مرسيدس": "mercedes",
    "مارسيدس": "mercedes", "بنز": "mercedes",
    "toyota": "toyota", "تويوتا": "toyota", "hyundai": "hyundai", "هيونداي": "hyundai", "هيوندا": "hyundai",
    "kia": "kia", "كيا": "kia", "nissan": "nissan", "نيسان": "nissan",
    "chevrolet": "chevrolet", "شيفروليه": "chevrolet", "شفروليه": "chevrolet", "شيفرليه": "chevrolet",
    "opel": "opel", "اوبل": "opel", "volkswagen": "volkswagen", "vw": "volkswagen", "فولكس فاجن": "volkswagen",
    "فولكسفاجن": "volkswagen", "فولكس": "volkswagen", "audi": "audi", "اودي": "audi",
    "peugeot": "peugeot", "بيجو": "peugeot", "renault": "renault", "رينو": "renault", "fiat": "fiat", "فيات": "fiat",
    "skoda": "skoda", "سكودا": "skoda", "mitsubishi": "mitsubishi", "ميتسوبيشي": "mitsubishi",
    "متسوبيشي": "mitsubishi", "honda": "honda", "هوندا": "honda", "suzuki": "suzuki", "سوزوكي": "suzuki",
    "chery": "chery", "شيري": "chery", "ford": "ford", "فورد": "ford", "geely": "geely", "جيلي": "geely",
    "lada": "lada", "لادا": "lada", "mg": "mg", "ام جي": "mg", "byd": "byd",
}
_MAKES = sorted(((k.split(), v) for k, v in _MAKE_WORDS.items()), key=lambda kv: -len(kv[0]))
_MAKE_STARTS = frozenset(said[0] for said, _make in _MAKES)
# What a part's own car text says without its make: a MINI chassis (R56, F56,
# J01), a BMW one (E90, F30, G20, X5), a Mercedes one (W204, C200).
_MINI = re.compile(r"^(?:r\d{2}|f5[4-7]|f6[05]|j0\d|u25)$")
_MERCEDES = re.compile(r"^(?:w|c|s|e|a|x|v|r|cl|sl|ml|gl|glk|cla|gla|glc|gle|slk|clk)\d{3}$")


_JOINED = ("وال", "بال", "لل", "ال", "ل", "ب", "و")


def _bare(word: str) -> str:
    """«لمرسيدس» → «مرسيدس», «والبي» → «بي»: a make however it's joined."""
    for p in _JOINED:
        if word.startswith(p) and len(word) - len(p) >= 2 and word[len(p):] in _MAKE_STARTS:
            return word[len(p):]
    return word


def makes_in(text: str) -> list:
    """The makes said in `text`, in order («عندكم قطع بي ام؟» → ["bmw"])."""
    words, found, i = [_bare(w) for w in normalize(text).split()], [], 0
    while i < len(words):
        for said, make in _MAKES:
            if words[i:i + len(said)] == said:
                if make not in found:
                    found.append(make)
                i += len(said)
                break
        else:
            i += 1
    return found


def make_of(car_model: str, models: list) -> str:
    """A part's make: written in its car text, or told by its chassis codes — '' when neither says."""
    said = makes_in(car_model)
    if said:
        return said[0]
    for model in models or []:
        model = str(model).lower()
        if _MINI.match(model):
            return "mini"
        if _MERCEDES.match(model):
            return "mercedes"
        if is_model(model) and not model.isdigit():
            return "bmw"
    return ""


def car_in(text: str) -> dict:
    """The car said in `text` — {"make", "models"} — or {} when none was."""
    models = models_said(text)
    makes = makes_in(text)
    make = makes[0] if makes else make_of("", models)
    return {"make": make, "models": models} if (make or models) else {}


def only_car(text: str) -> bool:
    """«عربية إي نينتي», «بي ام E90»: nothing said but the car — the answer to
    «اسم العربية إيه؟», not a part to look for."""
    words = part_words(text)
    if not words or not car_in(text):
        return False
    makes = {w for said, _make in _MAKES for w in said}
    return all(_filler(w) or is_model(w) or w in makes for w in words)


def car_of(car_model: str, chassis) -> dict:
    """The car a part is for (its catalog data) — {} when it doesn't say."""
    car_model = car_model if isinstance(car_model, str) else ""
    models = models_of(chassis, car_model)
    tokens = [t for t in re.split(r"[\s,،/|\-]+", (car_model or "").lower()) if t]
    make = make_of(car_model, models + [t for t in tokens if _MERCEDES.match(t)])
    return {"make": make, "models": models} if (make or models) else {}


def with_car(old: dict, said: dict) -> dict:
    """The car once `said` was: models said replace the old ones; a make
    alone keeps the old models only when they're that make's."""
    old = old or {}
    if not said:
        return old
    if said.get("models"):
        return {"make": said.get("make") or old.get("make", ""), "models": list(said["models"])}
    models = [m for m in old.get("models") or [] if make_of("", [m]) in ("", said.get("make"))]
    return {"make": said.get("make", ""), "models": models}


def fit(car: dict, car_model: str, chassis) -> int:
    """How a part fits the customer's car: 2 its model, 1 its make (or a part
    whose car isn't known), 0 another car. 0 when no car is known — then it
    breaks no tie."""
    if not car:
        return 0
    part = car_of(car_model, chassis)
    models = {m.upper() for m in car.get("models") or []}
    if models and models & {m.upper() for m in part.get("models") or []}:
        return 2
    make = car.get("make") or make_of("", list(models))
    if not part.get("make") or not make:
        return 1
    return 1 if part["make"] == make else 0


def took(car: dict, part_car: dict) -> dict:
    """The car once the customer took a part: still theirs when the part fits
    it, else the part's (they asked for that car's part)."""
    if not part_car:
        return car or {}
    if car and car.get("models") and {m.upper() for m in car["models"]} & set(part_car.get("models") or []):
        return car
    return {"make": part_car.get("make") or (car or {}).get("make", ""), "models": list(part_car.get("models") or [])}


# ---------------------------------------------------------------------------
# What a customer says that isn't about a part: «السلام عليكم»، «شكراً»،
# «إنت مين؟»، «قول تاني» — answered the same, net or not (the brain has the
# clips: PHRASES). A greeting or a goodbye starts the conversation over.
# ---------------------------------------------------------------------------

W_SALAM = "وعليكم السلام،"
MORNING = "صباح النور،"
EVENING = "مساء النور،"
WELCOME = "أهلاً بيك،"
FINE = "الحمد لله، تمام."
ASK_PART = "تحت أمرك، محتاج قطعة إيه؟"
THANKS_BACK = "العفو، أي خدمة."
BYE_BACK = "مع السلامة، نورتنا."
SHOP_ROBOT = "روبوت المحل."
CAN = "اسألني عن أي قطعة: موجودة ولا لأ، وسعرها كام، ومكانها فين، وعليها ضمان ولا لأ."
WAITING = "ماشي، أنا مستنيك."
AT_ORDER = "تحت أمرك."
NO_PROBLEM = "ماشي، لو احتجت أي حاجة قولي."
ASK_STAFF_HERE = "اتفضل اسأل أي حد من الموظفين، وأنا موجود لو عايز تعرف أي قطعة."
NOTHING_YET = "تحت أمرك، اسألني عن أي قطعة."
TO_CASHIER = "تمام، اتفضل عند الكاشير يجهزهالك ويحاسبك."
MISUNDERSTOOD = "معلش، فهمتك غلط. قولي تاني محتاج إيه؟"
NOT_UNDERSTOOD = "معلش، مفهمتش قصدك. ممكن تقولها تاني بشكل تاني؟"
PHRASES += [W_SALAM, MORNING, EVENING, WELCOME, FINE, ASK_PART, THANKS_BACK, BYE_BACK, SHOP_ROBOT, CAN,
            WAITING, AT_ORDER, NO_PROBLEM, ASK_STAFF_HERE, NOTHING_YET, TO_CASHIER, MISUNDERSTOOD, NOT_UNDERSTOOD]
# What asking for something sounds like: «عندك فانوس عربية؟» asks for a part the robot
# doesn't know the word for; «بكلمه انجليزي على العربي» doesn't ask for anything.
_ASKING = frozenset("""
    عندك عندكم عندكو عندكوا فيه عايز عاوز عايزه عاوزه محتاج محتاجه محتاجين بكام كام سعر سعره سعرها
    تمن تمنه تمنها موجود موجوده متوفر متوفره متاح هات هاتلي جيبلي تجيبلي جايبلي ابحث دورلي
""".split())


def asks_for_a_part(text: str, keys: list, in_names) -> bool:
    """Whether a sentence nothing was found for asked for a part — a part
    number, asking words («عندك …؟»), a shop word for a part, a word of a
    part's name (`in_names(words)`: the server's products, the brain's
    catalog) — so «ملقتش القطعة» is the answer; else «مفهمتش قصدك».
    🐛 [FIX]: «شكرا ليك يا موسى» و«الله ينور عليك» اتردّ عليهم «ملقتش القطعة دي في المخزون»."""
    if any(len(k) >= 5 and any(c.isdigit() for c in k) for k in keys):
        return True
    if set(normalize(text).split()) & _ASKING:
        return True
    from . import slang
    words = [k for k in keys if len(k) >= 3 and not k.isdigit()]
    return any(slang.known(k) for k in words) or (bool(words) and bool(in_names(words)))
# «الساعة كام؟» the way it's said in Egypt: «الساعة أربعة وتلت العصر».
_HOURS = ("اتناشر", "واحدة", "اتنين", "تلاتة", "أربعة", "خمسة", "ستة", "سبعة", "تمانية", "تسعة", "عشرة",
          "حداشر", "اتناشر")
_MINUTES = {0: "", 5: "وخمسة", 10: "وعشرة", 15: "وربع", 20: "وتلت", 25: "ونص إلا خمسة", 30: "ونص",
            35: "ونص وخمسة", 40: "إلا تلت", 45: "إلا ربع", 50: "إلا عشرة", 55: "إلا خمسة"}


def spoken_time(now) -> str:
    """«الساعة أربعة وتلت العصر.» — to the nearest five minutes; «إلا ربع»
    is the next hour's."""
    minute = int(round(now.minute / 5.0)) * 5
    hour = now.hour + (1 if minute >= 40 else 0)
    minute %= 60
    hour %= 24
    part = ("الصبح" if 5 <= hour < 12 else "الضهر" if 12 <= hour < 15
            else "العصر" if 15 <= hour < 18 else "بالليل")
    return " ".join(w for w in ("الساعة", _HOURS[hour % 12], _MINUTES[minute], part) if w) + "."

_GREETINGS = {          # what's said → how it's answered
    "salam": ("السلام عليكم", "سلام عليكم", "السلام عليكو", "سلام عليكو", "سلامو عليكو", "سلامو عليكم",
              "السلام عليكم ورحمه الله", "السلام عليكم ورحمه الله وبركاته", "سلام عليكم ورحمه الله"),
    "morning": ("صباح الخير", "صباح الفل", "صباح النور", "صباح الورد", "صباحو", "صباح الفل عليك"),
    "evening": ("مساء الخير", "مساء الفل", "مساء النور", "مساء الورد", "مسا الخير", "مساء الفل عليك"),
    "hello": ("اهلا", "اهلين", "اهلا بيك", "اهلا وسهلا", "اهلا اهلا", "هاي", "هالو", "هلو", "الو", "مرحبا",
              "نهارك سعيد", "نهارك ابيض", "hi", "hello", "hey", "good morning", "good evening"),
}
_GREETING_REPLY = {"salam": W_SALAM, "morning": MORNING, "evening": EVENING, "hello": WELCOME}
_CHAT = {               # the rest of small talk, by what it asks for
    "how": ("ازيك", "ازيكم", "ازيكو", "عامل ايه", "عاملين ايه", "عامله ايه", "اخبارك ايه", "اخبارك",
            "ايه الاخبار", "ايه اخبارك", "كله تمام", "انت كويس", "كيفك", "اخبارك ايه النهارده", "how are you"),
    "thanks": ("شكرا", "شكرا جزيلا", "شكرا ليك", "متشكر", "متشكرين", "متشكره", "تسلم", "تسلم ايدك",
               "تسلملي", "تسلم يا غالي", "ربنا يخليك", "الله يخليك", "ربنا يكرمك", "مرسي", "ميرسي",
               "كتر خيرك", "جزاك الله خير", "تعيش", "الله ينور عليك", "ينور عليك", "الله ينور", "منور",
               "نورت", "حبيبي", "يا حبيبي", "thanks", "thank you", "thx"),
    "bye": ("مع السلامه", "سلام", "باي", "باي باي", "يلا سلام", "في امان الله", "اشوفك بعدين",
            "تصبح علي خير", "نتقابل بعدين", "bye", "goodbye"),
    "who": ("انت مين", "مين انت", "انتي مين", "اسمك ايه", "ايه اسمك", "انت اسمك ايه", "انت روبوت",
            "انت انسان", "انت حقيقي", "انت بني ادم", "انت ايه", "مين بيكلمني", "who are you"),
    "can": ("بتعمل ايه", "بتعرف تعمل ايه", "تقدر تعمل ايه", "تقدر تساعدني", "ممكن تساعدني", "ساعدني",
            "تساعدني ازاي", "اسالك ازاي", "بتفهم ايه", "شغلتك ايه", "انت بتعمل ايه هنا", "اعمل ايه",
            "اقولك ايه", "what can you do", "help"),
    "wait": ("استني", "استنا", "استني شويه", "استنا شويه", "ثانيه", "ثانيه واحده", "لحظه", "لحظه واحده",
             "دقيقه", "دقيقه واحده", "ثواني", "خليك معايا", "اصبر", "اصبر شويه", "ثواني ماشي",
             "هقولك رقمها", "هقولك الرقم", "هجيبلك الرقم", "هجيبلك رقمها", "استني اجيبلك الرقم", "wait"),
    "repeat": ("قول تاني", "قولها تاني", "قوله تاني", "عيد", "عيدها", "عيد تاني", "عيد كلامك", "قلت ايه",
               "بتقول ايه", "مسمعتش", "مسمعتكش", "مش سامع", "مش سامعك", "مفهمتش", "مش فاهم", "مش فاهمك",
               "ايه بتقول", "تاني كده", "repeat", "again"),
    "ok": ("تمام", "ماشي", "اوك", "اوكي", "ok", "okay", "حلو", "جميل", "عظيم", "طب تمام", "ماشي تمام",
           "تمام كده", "تمام اوي", "فهمت"),
    "no": ("لا", "لاء", "لا خلاص", "مش عايز", "مش عاوز", "مش محتاج", "مش لازم", "خلاص مش عايز", "no"),
    "buy": ("هاخدها", "هاخده", "هاخدهم", "خلاص هاخدها", "هاخد واحده", "هاخد اتنين", "اديني واحده",
            "اديني اتنين", "اديهالي", "لفهالي", "لفهولي", "جهزهالي", "جهزهولي", "هشتريها", "هشتريه",
            "عايز اشتريها", "عايز اخدها", "ابعتهالي الكاشير", "اوكي هاخدها", "حلو هاخدها"),
    # 🐛 [FIX]: «خطأ» راح للذكاء الاصطناعي واستنى ٨ ثواني عشان يعتذر؛ و«الساعة كام؟» اترد عليها
    # «تقصد حساس ساعة الزيت؟». الاتنين دلوقتي كلام عادي بيترد عليه على طول.
    "wrong": ("غلط", "خطا", "لا غلط", "انت غلطان", "غلطان", "فهمت غلط", "مش ده", "مش دي", "مش كده",
              "مش قصدي", "مش ده قصدي", "مش دي اللي انا عايزها", "مش ده اللي انا عايزه", "wrong"),
    "time": ("الساعه كام", "الساعه كم", "كام الساعه", "الساعه كام دلوقتي", "الساعه كام معاك",
             "معاك ساعه", "الساعه بقت كام", "what time is it"),
    "human": ("عايز اكلم حد", "عاوز اكلم حد", "عايز حد يساعدني", "عايز موظف", "فين الموظف", "فين الموظفين",
              "فين البياع", "فين حد", "مفيش حد هنا", "عايز اكلم بني ادم", "عايز المسؤول", "فين صاحب المحل",
              "عايز المدير", "عايز اكلم المدير"),
}
# Said along with any of it and changing nothing: «شكراً يا باشا»، «تمام يا ريس».
_POLITE = frozenset("يا و باشا ريس معلم هندسه اسطي اسطا يسطا كابتن حاج بيه برنس غالي حبيبي عم استاذ "
                    "حضرتك لو سمحت من فضلك بقي طيب طب يعني معلش كده كدا بس اوي خالص جدا موس والله".split())
_TALK = sorted([(p.split(), ("greet", g)) for g, said in _GREETINGS.items() for p in said]
               + [(p.split(), (k, "")) for k, said in _CHAT.items() for p in said],
               key=lambda kv: -len(kv[0]))


def _read_talk(words: list):
    """[(kind, greeting)] the words are made of, and how many were read before
    the first that isn't small talk."""
    found, i = [], 0
    while i < len(words):
        if words[i] in _POLITE:
            i += 1
            continue
        for said, what in _TALK:
            if words[i:i + len(said)] == said:
                found.append(what)
                i += len(said)
                break
        else:
            break
    return found, i


def small_talk(text: str) -> Optional[dict]:
    """{"greet": "salam"|"morning"|"evening"|"hello"|"", "kinds": {...}} when
    the sentence is only greetings, thanks, a goodbye … — else None."""
    words = normalize(text).split()
    found, read = _read_talk(words)
    if not found or read < len(words):
        return None
    greet = next((g for kind, g in found if kind == "greet"), "")
    return {"greet": greet, "kinds": {kind for kind, _g in found if kind != "greet"}}


def opening(text: str) -> tuple:
    """(the greeting answered, the rest as said, the answer's clips) for
    «السلام عليكم، عندك طرمبة؟» — ("", text, []) when it doesn't open with one."""
    raw = (text or "").split()
    norm = [normalize(w) for w in raw]
    words = " ".join(norm).split()
    found, read = _read_talk(words)
    if not found or found[0][0] != "greet" or read >= len(words):
        return "", text, []
    if any(kind not in ("greet", "how") for kind, _g in found):
        return "", text, []
    clips = [_GREETING_REPLY[found[0][1]]] + ([FINE] if any(k == "how" for k, _g in found) else [])
    taken, rest = 0, []
    for w_raw, w_norm in zip(raw, norm):          # back to the words as said
        if taken >= read:
            rest.append(w_raw)
        taken += len(w_norm.split())
    return " ".join(clips), " ".join(rest).strip(" ،,.!؟?:-"), clips


REPLY_ORDER = ("bye", "wrong", "no", "buy", "thanks", "time", "who", "can", "human", "wait", "repeat", "ok")


def main_kind(talk: dict) -> str:
    """What the answer is to: a goodbye over thanks, thanks over «تمام» …"""
    return next((k for k in REPLY_ORDER if k in (talk.get("kinds") or set())), "")


def small_talk_reply(talk: dict, name: str, last: str = "", now=None) -> tuple:
    """(what is said, its clips, whether the conversation starts over).
    `name`: the robot's name; `last`: what it said before («قول تاني»);
    `now`: the shop's time («الساعة كام؟»), this computer's when not given.
    «غلط» starts over too: the part it was on was the wrong one."""
    said = []
    if talk.get("greet"):
        said.append(_GREETING_REPLY[talk["greet"]])
    kinds = talk.get("kinds") or set()
    if "how" in kinds:
        said.append(FINE)
    clips = list(said)
    for kind in REPLY_ORDER:
        if kind not in kinds:
            continue
        if kind == "who":
            me = f"أنا {name}،"
            said += [me, SHOP_ROBOT, CAN]
            clips += [me, SHOP_ROBOT, CAN]
        elif kind == "repeat":
            said.append(last or NOTHING_YET)
            clips.append(last or NOTHING_YET)
        elif kind == "time":
            from datetime import datetime
            line = spoken_time(now or datetime.now())
            said.append(line)
            clips.append(line)
        else:
            line = {"bye": BYE_BACK, "thanks": THANKS_BACK, "can": CAN, "human": ASK_STAFF_HERE,
                    "wait": WAITING, "no": NO_PROBLEM, "ok": AT_ORDER, "buy": TO_CASHIER,
                    "wrong": MISUNDERSTOOD}[kind]
            said.append(line)
            clips.append(line)
        break
    else:
        said.append(ASK_PART)
        clips.append(ASK_PART)
    return " ".join(said), clips, bool(talk.get("greet")) or bool(kinds & {"bye", "wrong"})
