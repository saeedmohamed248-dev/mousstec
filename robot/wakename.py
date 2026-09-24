"""
robot/wakename.py — the robot answers only when it's spoken to by name.

The classic ESP32 can't run an on-device wake-word model, so the bridge sends
every utterance its voice-activity detector hears, and the server decides:
was the robot's name ("موس") said? If not, the speech was people talking to
each other near the robot — it stays silent and nothing is logged.

Once called, it keeps listening for CONVERSATION_SECONDS so follow-ups
("وبكام؟") don't need the name again. No name is needed during flows the
robot itself started (a staff enrollment round, a stock count) or when the
push-to-talk button is held.

Speech-to-text spells a short Arabic name several ways (موس / موص / ماوس /
Mouss), so matching normalizes Arabic letter forms and accepts the device's
`wake_aliases` plus common transliterations.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone

CONVERSATION_SECONDS = 20

# Extra spellings STT tends to produce for the default name "موس".
_DEFAULT_ALIASES = {
    "موس": ["موص", "مووس", "ماوس", "mouss", "mous", "moss", "mouse", "moose", "mose"],
}

_DIACRITICS = re.compile(r"[ً-ْٰـ]")  # tashkeel + tatweel
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_VOCATIVES = ("يا", "ياا", "hey", "hi", "ya", "o")


def normalize(text: str) -> str:
    """Lowercase, drop tashkeel/punctuation, unify Arabic letter variants."""
    t = _DIACRITICS.sub("", (text or "").lower())
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي"))
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split())


def names_for(device) -> list:
    """Every normalized spelling that counts as the robot's name."""
    base = (getattr(device, "wake_name", "") or "موس").strip()
    names = [base] + list(getattr(device, "wake_aliases", None) or [])
    names += _DEFAULT_ALIASES.get(base, [])
    out = []
    for n in names:
        n = normalize(str(n))
        if n and n not in out:
            out.append(n)
    return out


def strip_name(device, transcript: str):
    """(addressed, remaining_text).

    `addressed` is True when the name appears as a whole word (optionally
    after "يا"/"hey"). The name and its vocative are removed and the rest is
    returned in its ORIGINAL spelling (part names must still match the
    catalogue): "يا موس، اجرد الفلاتر" → "اجرد الفلاتر".
    """
    raw = (transcript or "").split()
    norm = [normalize(w) for w in raw]          # one entry per original word
    names = [n.split() for n in names_for(device)]
    for i in range(len(norm)):
        for parts in names:
            # STT sometimes glues the vocative on: "ياموس".
            if len(parts) == 1 and norm[i] == "يا" + parts[0]:
                return True, " ".join(raw[:i] + raw[i + 1:]).strip(" ،,.!؟?:-")
            if norm[i:i + len(parts)] == parts:
                start = i - 1 if i > 0 and norm[i - 1] in _VOCATIVES else i
                rest = raw[:start] + raw[i + len(parts):]
                return True, " ".join(rest).strip(" ،,.!؟?:-")
    return False, (transcript or "").strip()


def in_conversation(device, now=None) -> bool:
    until = getattr(device, "listening_until", None)
    return bool(until and until > (now or timezone.now()))


def keep_listening(device, now=None):
    """Open/extend the follow-up window after the robot was addressed."""
    device.listening_until = (now or timezone.now()) + timedelta(seconds=CONVERSATION_SECONDS)
    device.save(update_fields=["listening_until"])


def gate(device, transcript: str, *, push_to_talk: bool = False, ongoing_flow: bool = False):
    """Decide whether a heard utterance is for the robot.

    Returns (for_robot, text_to_handle, called_by_name_only). `text_to_handle`
    has the name removed. `called_by_name_only` is True for a bare "يا موس"
    (answer "أيوه؟" and wait for the question).
    """
    addressed, rest = strip_name(device, transcript)
    if not getattr(device, "wake_required", True):
        return True, (rest if addressed else transcript.strip()), addressed and not rest
    if addressed:
        return True, rest, not rest
    if push_to_talk or ongoing_flow or in_conversation(device):
        return True, transcript.strip(), False
    return False, "", False
