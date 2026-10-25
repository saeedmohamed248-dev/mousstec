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
for _line in GROUPS:
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


def rewrite(normalized: str) -> str:
    """Two-word names said as the shop says them, as one («بريك باد» → «تيل»)."""
    for pattern, to in _PHRASES:
        normalized = pattern.sub(to, normalized)
    return normalized
