"""
🧪 Sanity Sweep — فحص حتمي (deterministic) قبل ما نصدّق أي رد.

LLM واحد ممكن يخترع رقم. لو الرقم خارج النطاق الفيزيكي المعقول، **ده غلط مؤكد**
مفيش داعي لاستدعاء LLM تاني عشان نتأكد. الفحص ده بـ regex على نص الرد:

    • عزم تربيط: 0.3 → 600 Nm (مفك حساس صغير → مسامير صلب رأس مكينة)
    • فولت سيارة: -2 → 60 V (DC system + احتمال 48V mild-hybrid bus)
    • أوم لحساسات: 0 → 100,000 Ω (NTC/PTC, coil primary, igniters)
    • مقاس سلك: 0.22 → 95 mm² (signal wire → starter cable)

النتيجة:
    {'ok': bool, 'failures': [str, ...]}

لو ok=False، الرد بيُعاد تلقائياً (force revise) حتى لو V2 قال pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# نطاقات معقولة لكل وحدة قياس في الصيانة السياراتية
_RANGES = {
    'Nm':  (0.3, 600.0),
    'V':   (-2.0, 60.0),
    'Ω':   (0.0, 100_000.0),
    'mm2': (0.22, 95.0),
}

# Synonyms: لاقط كل شكل ممكن يكتبه الـ LLM
_UNIT_ALIASES = {
    'Nm':  (r'(?:Nm|N\.m|نيوتن[\s·.]*متر|نيوتن متر)', 'Nm'),
    'V':   (r'(?:V|volt|فولت)\b', 'V'),
    'Ω':   (r'(?:Ω|ohm|أوم|اوم|kΩ|kohm|كيلو\s*أوم)', 'Ω'),
    'mm2': (r'(?:mm²|mm2|mm\^2|مم²|ملم²|ملم2)', 'mm2'),
}

_NUMBER_RE = r'(\d+(?:[.,]\d+)?)'


@dataclass
class SanityResult:
    ok: bool
    failures: list[str]


def sweep(text: str) -> SanityResult:
    """يفحص كل رقم بوحدة معروفة في النص. لو أي واحد خارج النطاق، fail."""
    if not text:
        return SanityResult(ok=True, failures=[])

    failures: list[str] = []
    for canonical_unit, (pattern, _) in _UNIT_ALIASES.items():
        lo, hi = _RANGES[canonical_unit]
        # اقبل: "8 Nm" / "8 نيوتن متر" / "8Nm" / "8.5 V"
        full_re = re.compile(_NUMBER_RE + r'\s*' + pattern, re.IGNORECASE)
        for match in full_re.finditer(text):
            raw = match.group(1).replace(',', '.')
            try:
                value = float(raw)
            except ValueError:
                continue
            # kΩ / كيلو أوم → اضرب × 1000
            if 'k' in match.group(0).lower() or 'كيلو' in match.group(0):
                value *= 1000
            if not (lo <= value <= hi):
                failures.append(
                    f'قيمة مشكوك فيها: {value:g} {canonical_unit} '
                    f'(النطاق المعقول {lo:g}–{hi:g})'
                )

    return SanityResult(ok=not failures, failures=failures)
