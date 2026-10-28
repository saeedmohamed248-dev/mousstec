"""What a part is called in an Egyptian shop, and what it's written as.

A customer asks for «كاوتش», the catalogue says «إطار»; one says «بوجيهات»,
another «شمع», the shelf label «شمعة احتراق». Each group below is one part
(or one thing a part is for) in its spellings: a word said is matched
against a product's name in any of its group's words — by the server
(robot/services.match_part_in_sentence) and by the brain offline
(knowledge.Knowledge._in_sentence) alike.

Only what an Egyptian mechanic would take for the same thing. A wrong match
is a wrong price said out loud, so «فانوس» (a tail light, or a head light)
and «كشاف» are left apart, and two parts that fit equally are still asked
about («تقصد أنهي؟»). What a shop calls its own parts is taught by voice:
«يا موس اتعلم الكبالن يعني 31607508009».
"""

import re

# Normalized (ة→ه, ى→ي, أ/إ/آ→ا), no «ال»: as robot/services._part_words leaves a word.
GROUPS = """
    طرمبه طرمبات طلمبه طلمبات مضخه مضخات بمب بامب
    رديتر رديترات ردياتير راديتر رادياتير مبرد
    كاوتش كاوتشات كوتش اطار اطارات تاير تايرات
    بوجيه بوجيهات بوجي شمعه شمعات شمع بلج سبارك
    تيل فحمات فحمه
    فرامل فرمله بريك بريكات
    مساعد مساعدين امورتيزر امرتيزر ممتص ممتصات
    شكمان اشطمان شكمانات عادم اكزوست
    دبرياج كلتش كلاتش
    رولمان رولمانات بلي بيرنج
    مارش سلف استارتر
    دينامو الترنيتر ترنيتر التينيتر تينيتر مولد
    بطاريه بطاريات باتري
    فتيس جيربوكس
    بوبينه بوبينات كويل
    حساس حساسات سنسور سينسور
    ترموستات ثرموستات
    كبوت كابوت
    اكصدام اكصدامات صدام بمبر
    رفرف رفارف فندر
    مرايه مرايات مراه
    جوان جوانات حشوه
    سير سيور حزام
    كاتينه جنزير
    مقص مقصات
    كبالن كبلن
    بستم بساتم مكبس
    ديسك ديسكات طاره
    طنبوره طنابير درام
    ازاز قزاز زجاج
    مساحه مساحات ماسحه
    دواسه دواسات بدال
    خرطوم خراطيم
    جلبه جلب جلده
    موتور ماتور محرك مكنه ماكينه
    زيت اويل
    هوا هواء اير
    ميه مياه ماء
    بنزين وقود
    كهربا كهرباء
    امامي قدامي
    خلفي وراني
    كلاكس كلكس زماره بوري هورن
    جنط جنوط
    مروحه مراوح فان
    كمبروسر كومبروسر كومبريسور
    تكييف مكيف
    صره صرر
    ماسوره مواسير
    بلف بلوف صمام صمامات
""".strip().splitlines()

# Where a part is from and whose it is, said in Arabic and written in English on
# the label: «فلتر زيت ميني كوبر» is «فلتر زيت MINI - JP», «الياباني» the «JP» one,
# «بوش» «BOSCH». 🐛 [FIX]: «عندك فلتر زيت ميني كوبر؟» اتسأل «تقصد أنهي؟» بين ٣ فلاتر
# (منهم بتوع E90)، و«بكام سعر الياباني؟» بعدها اترد عليها «ملقتش القطعة». No two-letter
# codes but «jp»: «de», «tr», «cn» are inside too many other words.
BRANDS = """
    ياباني يابانى جاباني jp japan japanese
    الماني المانى german germany
    صيني صينى china chinese
    كوري كورى korea korean
    تايواني taiwan
    تركي turkey turkish
    اصلي اوريجينال اورجينال original genuine oem
    ميني mini
    بوش bosch
    ماهلي ماهله mahle
    فيبي febi
    ليمفوردر lemforder
    فاليو valeo
    دينسو denso
    سكس sachs
    بيلشتاين bilstein
    كونتيننتال continental
    شل شيل shell
    موبيل mobil
    كاسترول castrol
    توتال total
    موتول motul
""".strip().splitlines()

# Said as two words, written as one thing (or the other way round).
PHRASES = {
    "بريك باد": "تيل",
    "اويل فلتر": "فلتر زيت",
    "اير فلتر": "فلتر هوا",
    "فلتر اير": "فلتر هوا",
    "رولمان بلي": "رولمان",
    "طقم جوانات": "جوانات",
}

# Words a question is made of in a shop — never a part's name: «عامل كام يا باشا؟»
FILLERS = frozenset("""
    عامل عامله عاملين تجيبلي جيبلي هاتلي ابعتلي محتاجين عايزين عاوزين ينفع معاك معاكم معاكو
    حضرتك باشا ريس معلم هندسه اسطي اسطا يسطا كابتن حاج بيه برنس غالي
    لو سمحت بقولك بص شوف كدا بقي طيب يعني نوعه انهي اني فين امتي واحد واحده
""".split())


# Parts said two at a time: «فلترين» is «2 فلتر», «طرمبتين» «2 طرمبة». Only
# these words — «بنزين» ends like a dual too, and isn't two of anything.
TWO_OF = """
    فلتر لمبه فانوس كشاف جنط طقم ياي بلف ماسوره مسمار صامولة كرسي عمود كوريك شنطه
""".split()


def _normalize(word: str) -> str:
    return (word.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
                .replace("ى", "ي").replace("ة", "ه").strip())


_INDEX = {}
for _line in GROUPS + BRANDS:
    _group = frozenset(_normalize(w) for w in _line.split())
    for _w in _group:
        _INDEX[_w] = _group | _INDEX.get(_w, frozenset())

_PHRASES = [(re.compile(rf"(?:^|(?<=\s)){re.escape(_normalize(k))}(?=\s|$)"), _normalize(v))
            for k, v in PHRASES.items()]


# What isn't counted in pieces: «ميتين» is 200, not two waters.
_UNCOUNTED = set("ميه مياه ماء زيت اويل هوا هواء اير بنزين وقود كهربا كهرباء امامي قدامي خلفي وراني".split())


def _duals():
    words = {_normalize(w) for line in GROUPS for w in line.split()} | {_normalize(w) for w in TWO_OF}
    duals = {}
    for w in words - _UNCOUNTED:
        said = {w + "ين", w + "ان"}
        if w.endswith("ه"):
            said |= {w[:-1] + "تين", w[:-1] + "تان"}
        for d in said - words:                       # «مساعدين» is a word of its own
            duals[d] = w
    return duals


_DUALS = _duals()


def two_of(word: str):
    """«فلترين» → «فلتر», «طرمبتين» → «طرمبه»: the part said two at a time, or None."""
    return _DUALS.get(_normalize(word))


def forms(word: str) -> set:
    """The word, its group's words, and both without a written case ending
    (Vosk's news Arabic heard «فلتر زيت» as «فلترا زيت»)."""
    out = {word}
    if len(word) >= 4 and word.endswith("ا"):
        out.add(word[:-1])
    for w in list(out):
        out |= _INDEX.get(w, frozenset())
    return out


# A chassis said aloud, as Azure writes it: «إي. نينتي» / «اي تسعين» / «إيه 92» /
# «اف تلاتين» / «اي ستة وأربعين» → «e90», «e92», «f30», «e46».
# 🐛 [FIX]: «عربية إي. نينتي» بعد «ممكن تقولي اسم العربية؟» راح للذكاء الاصطناعي اللي طلب رقم الشاسيه،
# و«إيه 92؟» جواب «تقصد أنهي؟» (وفيهم طلمبة e92) اترد عليه «ملقتش القطعة».
_CHASSIS_LETTERS = {"اي": "e", "ايه": "e", "e": "e", "اف": "f", "f": "f", "جي": "g", "g": "g",
                    "ار": "r", "r": "r", "يو": "u", "u": "u",
                    # an engine too: «إن تونتي» → n20, «بي فورتي ايت» → b48 (only before a number said)
                    "ان": "n", "الان": "n", "n": "n", "بي": "b", "b": "b"}
# 🐛 [FIX]: from the shop's log — Azure wrote «عربية إف سيتي 2015 موتور الإن تونتي» (F30, N20): «سيتي»
# and «تونتي» are its «thirty» and «twenty».
_TENS = {"عشرين": 20, "توينتي": 20, "تونتي": 20, "تلاتين": 30, "ثلاثين": 30, "ثيرتي": 30, "تيرتي": 30,
         "سيتي": 30, "سرتي": 30,
         "اربعين": 40, "فورتي": 40, "خمسين": 50, "فيفتي": 50, "ستين": 60, "سيكستي": 60,
         "سبعين": 70, "سفنتي": 70, "تمانين": 80, "ثمانين": 80, "ايتي": 80, "تسعين": 90, "نينتي": 90}
_UNITS = {"واحد": 1, "اتنين": 2, "اثنين": 2, "تلاته": 3, "ثلاثه": 3, "اربعه": 4, "خمسه": 5,
          "سته": 6, "سبعه": 7, "تمانيه": 8, "ثمانيه": 8, "تسعه": 9}
_EN_UNITS = {"ون": 1, "وان": 1, "تو": 2, "ثري": 3, "تري": 3, "فور": 4, "فايف": 5, "سكس": 6,
             "سيكس": 6, "سفن": 7, "سيفن": 7, "ايت": 8, "ناين": 9}


def _two_digits(words: list):
    """(the number 10–99 said in `words`' first one or two, how many words) or (None, 0)."""
    first = words[0] if words else ""
    if first.isdigit() and len(first) == 2:
        return int(first), 1
    if first in _TENS:
        if len(words) > 1 and words[1] in _EN_UNITS:
            return _TENS[first] + _EN_UNITS[words[1]], 2
        return _TENS[first], 1
    if first in _UNITS and len(words) > 1 and words[1].startswith("و") and words[1][1:] in _TENS:
        return _TENS[words[1][1:]] + _UNITS[first], 2
    return None, 0


# 🐛 [FIX]: from the shop's log — «عندك فلتر زيت، إيه فسيتي؟»: «إف سيتي» (F30) written as one word.
# Only the English-sounding tens: «بعشرين» is a price, «فعشرين» is «في عشرين».
_GLUED_LETTERS = (("اف", "f"), ("ف", "f"), ("اي", "e"), ("ان", "n"))
_GLUED_TENS = frozenset("توينتي تونتي ثيرتي تيرتي سيتي سرتي فورتي فيفتي سيكستي سفنتي ايتي نينتي".split())


def _unglued(word: str) -> list:
    """«فسيتي» → ['اف', 'سيتي']; any other word as it is."""
    for said, _letter in _GLUED_LETTERS:
        if word.startswith(said) and word[len(said):] in _GLUED_TENS:
            return ["اف" if said == "ف" else said, word[len(said):]]
    return [word]


def _chassis(words: list) -> list:
    words = [part for word in words for part in _unglued(word)]
    out, i = [], 0
    while i < len(words):
        letter = _CHASSIS_LETTERS.get(words[i])
        if letter:
            number, used = _two_digits(words[i + 1:i + 3])
            if number is not None and number >= 10:
                out.append(f"{letter}{number}")
                i += 1 + used
                continue
        out.append(words[i])
        i += 1
    return out


def known(word: str) -> bool:
    """A word of the shop's for a part, a brand or where it's from («طرمبه», «بوش»)."""
    return word in _INDEX or (len(word) >= 4 and word.endswith("ا") and word[:-1] in _INDEX)


def rewrite(normalized: str) -> str:
    """Two-word names said as the shop says them, as one («بريك باد» → «تيل»),
    and a chassis said aloud as it's written («اي نينتي» → «e90»)."""
    for pattern, to in _PHRASES:
        normalized = pattern.sub(to, normalized)
    return " ".join(_chassis(normalized.split()))
