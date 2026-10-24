"""
robot/language.py — answer in the language the customer spoke.

Customers talk to the robot in Egyptian Arabic or in English. The replies are
written in Egyptian Arabic; when the speaker used English the reply is
translated (Gemini, the same key speech-to-text uses) and the TTS voice
follows the text, so an English question gets an English answer in an
English voice. If translation is unavailable the Arabic reply is spoken.
"""

from __future__ import annotations

import re

from .pricing import redact

_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")

_TO_ENGLISH = (
    "Translate this reply from a robot in an Egyptian BMW/MINI car-parts shop "
    "into short, natural spoken English. Keep every number, price, part number "
    "and shelf code exactly as written; say جنيه as \"pounds\". Translate or "
    "transliterate Arabic product names into English. Reply with ONLY the "
    "translation."
)


def detect(text: str) -> str:
    """'en' when the text is mostly English letters, else 'ar'.

    Words with a digit (part numbers, fault codes, "N20") are left out: they
    are written in Latin letters whichever language is spoken around them.
    """
    words = [w for w in (text or "").split() if not any(c.isdigit() for c in w)]
    arabic = sum(len(_ARABIC.findall(w)) for w in words)
    latin = sum(len(_LATIN.findall(w)) for w in words)
    return "en" if latin > arabic else "ar"


def to_english(text: str) -> str:
    """The reply in English, or unchanged when it has no Arabic or can't be
    translated right now."""
    if not _ARABIC.search(text or ""):
        return text
    from . import audio
    translated = audio.gemini_generate([{"text": _TO_ENGLISH}, {"text": text}],
                                       what="translate")
    return redact(translated) if translated else text


def reply_in(lang: str, reply: str) -> str:
    """`reply` (Egyptian Arabic) as it should be spoken to a `lang` speaker."""
    return to_english(reply) if lang == "en" and reply else reply


# ---------------------------------------------------------------------------
# Numbers said the Egyptian way («ربعمية وخمسين جنيه»)
# ---------------------------------------------------------------------------
# The Egyptian TTS voice reads digits in formal Arabic («أربعمائة وخمسون»), so
# every price sounded like a news bulletin. Replies keep their digits (the
# dashboard log stays readable); they're turned into words just before speech.

_ONES = ["", "واحد", "اتنين", "تلاتة", "أربعة", "خمسة", "ستة", "سبعة", "تمانية", "تسعة",
         "عشرة", "حداشر", "اتناشر", "تلتاشر", "أربعتاشر", "خمستاشر", "ستاشر", "سبعتاشر",
         "تمنتاشر", "تسعتاشر"]
_TENS = ["", "", "عشرين", "تلاتين", "أربعين", "خمسين", "ستين", "سبعين", "تمانين", "تسعين"]
_HUNDREDS = ["", "مية", "ميتين", "تلتمية", "ربعمية", "خمسمية", "ستمية", "سبعمية", "تمنمية",
             "تسعمية"]
_FEW_THOUSANDS = {3: "تلات", 4: "أربع", 5: "خمس", 6: "ست", 7: "سبع", 8: "تمن", 9: "تسع", 10: "عشر"}
_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
# A whole number of up to 6 digits, optionally glued to «و» («و50 قرش»); not
# part of a code (E90, 11427953125), a decimal, or a number with a leading 0.
_SPOKEN_NUMBER = re.compile(r"(?<![\w.])(و?)(?!0\d)(\d{1,6})(?!\w|\.\d)")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = [_HUNDREDS[hundreds]] if hundreds else []
    if rest:
        tens, ones = divmod(rest, 10)
        if rest < 20:
            parts.append(_ONES[rest])
        else:
            parts.append(f"{_ONES[ones]} و{_TENS[tens]}" if ones else _TENS[tens])
    return " و".join(parts)


def egyptian_number(n: int) -> str:
    """0..999999 in Egyptian Arabic words: 1800 → «ألف وتمنمية»."""
    if n == 0:
        return "صفر"
    thousands, rest = divmod(n, 1000)
    parts = []
    if thousands == 1:
        parts.append("ألف")
    elif thousands == 2:
        parts.append("ألفين")
    elif thousands in _FEW_THOUSANDS:
        parts.append(f"{_FEW_THOUSANDS[thousands]} تلاف")
    elif thousands:
        parts.append(f"{_below_thousand(thousands)} ألف")
    if rest:
        parts.append(_below_thousand(rest))
    return " و".join(parts)


# ---------------------------------------------------------------------------
# Part names said the way people say them («فلتر زيت ميني ياباني»)
# ---------------------------------------------------------------------------
# The catalog writes «فلتر زيت MINI - JP (—)»: the voice read out the dash, the
# brackets and «جي بي». What's spoken keeps the name and says the country.
_MADE_IN = {"JP": "ياباني", "JPN": "ياباني", "DE": "ألماني", "GER": "ألماني", "CN": "صيني",
            "KR": "كوري", "TR": "تركي", "TW": "تايواني", "US": "أمريكاني", "USA": "أمريكاني",
            "IT": "إيطالي", "FR": "فرنساوي", "ES": "إسباني", "UK": "إنجليزي", "GB": "إنجليزي",
            "IN": "هندي", "TH": "تايلاندي", "EG": "مصري", "BR": "برازيلي", "PL": "بولندي",
            "CZ": "تشيكي", "SE": "سويدي", "AT": "نمساوي", "HU": "مجري", "MX": "مكسيكي",
            "MY": "ماليزي", "ID": "إندونيسي", "AE": "إماراتي", "SA": "سعودي"}
_NOTHING_IN_BRACKETS = re.compile(r"\(\s*[—–\-_.]*\s*\)")
_COUNTRY_AFTER_DASH = re.compile(r"[—–-]\s*(" + "|".join(sorted(_MADE_IN, key=len, reverse=True)) + r")(?![A-Za-z0-9])")
_DASH_BETWEEN = re.compile(r"\s+[—–-]\s+|\s+[—–-](?=[^\s\d])|(?<=[^\s\d])[—–-]\s+")


def speakable(text: str) -> str:
    """Arabic text as it should be heard: no «(—)», no dashes between a part's
    words, «- JP» said «ياباني». Only for the voice — the words on screen and
    in the log stay as the catalog has them."""
    t = _NOTHING_IN_BRACKETS.sub(" ", text or "")
    t = _COUNTRY_AFTER_DASH.sub(lambda m: " " + _MADE_IN[m.group(1)], t)
    t = _DASH_BETWEEN.sub(" ", t)
    t = re.sub(r"\s+([،,.؟?!:])", r"\1", " ".join(t.split()))
    return t


def egyptian_numbers(text: str) -> str:
    """Arabic text with its numbers written out as Egyptians say them."""
    return _SPOKEN_NUMBER.sub(lambda m: m.group(1) + egyptian_number(int(m.group(2))),
                              (text or "").translate(_INDIC_DIGITS))
