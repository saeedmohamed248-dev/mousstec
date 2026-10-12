"""
🔢 عرض الأرقام بشكل واضح (زي الأنظمة العالمية): ‎1,848,400.00‎ بدل ‎1848400,00‎.

مسجّلة builtins في الإعدادات — متاحة في كل القوالب من غير ``{% load %}``.

    {{ invoice.total|amount }}     → 1,848,400.00   (فلوس: خانتين عشريتين دايماً)
    {{ stock|num }}                → 1,848,400      (عدد صحيح)
    {{ ratio|num:1 }}              → 12.5
    {{ price|num:"-2" }}           → 12 أو 12.50    (العشري لو موجود بس)
    {{ value|compact }}            → 1.85 مليون     (كروت الأرقام الكبيرة)

للعرض بس — مش لقيم الحقول (``value="…"``) أو الـ JS اللي بيقرا الرقم.
فاضي/مش رقم → نص فاضي (زي ``floatformat``).
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


def _dec(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(',', '') if isinstance(value, str) else str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fmt(d, places):
    q = Decimal(1).scaleb(-places) if places else Decimal(1)
    d = d.quantize(q, rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)   # -0.00 → 0.00
    return f"{d:,.{places}f}"


@register.filter(name='amount')
def amount(value, places=2):
    """مبلغ: ‎1,848,400.00‎."""
    d = _dec(value)
    if d is None:
        return ''
    try:
        places = int(places)
    except (TypeError, ValueError):
        places = 2
    return _fmt(d, max(places, 0))


@register.filter(name='num')
def num(value, places=0):
    """عدد: ‎1,848,400‎ — ``"-2"`` = العشري يظهر بس لو الرقم مش صحيح."""
    d = _dec(value)
    if d is None:
        return ''
    try:
        places = int(places)
    except (TypeError, ValueError):
        places = 0
    if places < 0:
        places = 0 if d == d.to_integral_value() else -places
    return _fmt(d, places)


@register.filter(name='compact')
def compact(value):
    """الأرقام الكبيرة بالعربي: ‎1.85 مليون‎ / ‎2.4 مليار‎ / ‎185 ألف‎ — وغير كده الرقم نفسه."""
    d = _dec(value)
    if d is None:
        return ''
    a = abs(d)
    for size, word in ((Decimal('1e9'), 'مليار'), (Decimal('1e6'), 'مليون'), (Decimal('1e3'), 'ألف')):
        if a >= size:
            n = (d / size).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP).normalize()
            return f"{n:f} {word}"
    return _fmt(d, 0)
