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
