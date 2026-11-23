"""
🔎 بحث نصي متسامح — مشترك بين أسواق المنصة.

🐛 [FIX]: البحث كان `icontains` على العنوان العربي بس — فـ«tri-fold» مابتلاقيش
«بروشور تري فولد» (الاسم الإنجليزي في `title_en`)، و«اسكندرية» مابتلاقيش «إسكندرية»،
و«تري فولد» مابتلاقيش «تري-فولد». دلوقتي:
  - الكلمات بتتقسم وكل كلمة لازم تلاقي نفسها في أي حقل (AND بين الكلمات، OR بين الحقول).
  - أ/إ/آ/ا و ى/ي و ة/ه و الأرقام العربي/الإنجليزي ومسافة/شرطة بيتعاملوا كأنهم واحد.
"""
import re

from django.db.models import Q

_AR_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')
_CLASSES = {
    'ا': '[اأإآ]', 'أ': '[اأإآ]', 'إ': '[اأإآ]', 'آ': '[اأإآ]',
    'ي': '[يى]', 'ى': '[يى]', 'ة': '[ةه]', 'ه': '[ةه]',
}
_DIGIT_CLASSES = {str(i): f'[{i}{"٠١٢٣٤٥٦٧٨٩"[i]}]' for i in range(10)}
_SEP = r'[\s\-_/.]*'
MAX_TERMS = 6


def _term_regex(term):
    """كلمة ← regex متسامح (الشرطة جوه الكلمة اختيارية: tri-fold = trifold = tri fold)."""
    out = []
    for ch in term:
        if ch in _CLASSES:
            out.append(_CLASSES[ch])
        elif ch in _DIGIT_CLASSES:
            out.append(_DIGIT_CLASSES[ch])
        elif ch in '-_/.':
            out.append(_SEP)
        else:
            out.append(re.escape(ch))
    # بين أي حرفين ممكن تيجي شرطة/مسافة (trifold يلاقي tri-fold)
    return _SEP.join(p for p in out if p != _SEP) if out else ''


def terms(q):
    q = (q or '').translate(_AR_DIGITS).strip()
    parts = [p for p in re.split(r'\s+', q) if p]
    return parts[:MAX_TERMS]


def text_filter(q, fields):
    """يرجّع Q: كل كلمة لازم تتلاقي في واحد من `fields` على الأقل. None لو مفيش بحث."""
    words = terms(q)
    if not words:
        return None
    cond = Q()
    for w in words:
        rx = _term_regex(w)
        any_field = Q()
        for f in fields:
            any_field |= Q(**{f'{f}__iregex': rx})
        cond &= any_field
    return cond
