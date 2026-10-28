"""خلية CSV/Excel آمنة من «حقن المعادلات» (CSV injection).

اسم عميل أو رسالة جاية من برّه (واتساب/ماسنجر/حجز أونلاين) زي
«=HYPERLINK("http://…")» أو «@SUM(…)» كانت بتتنفّذ معادلة لما صاحب الشركة يفتح
الملف في Excel. القيمة دي بتتسبق بـ «'» فتتعرض نص زي ما هي. الأرقام السالبة
والتليفونات اللي بتبدأ بـ «+» بتفضل زي ما هي (مش معادلات).
"""
import re

_PLAIN_NUMBER = re.compile(r'^[+-]?\d[\d\s.,]*$')


def safe_cell(value):
    if not isinstance(value, str) or not value:
        return value
    first = value[0]
    if first in ('=', '@', '\t', '\r') or (first in ('+', '-') and not _PLAIN_NUMBER.match(value)):
        return "'" + value
    return value
