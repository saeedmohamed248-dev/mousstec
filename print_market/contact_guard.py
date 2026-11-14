"""
🛡️ حارس التواصل — ممنوع أي طرف يبعت موبايل / إيميل / لينك / واتساب برّه المنصة.

بيتطبّق على كل نص حر في السوق: الرسائل، وصف المنتجات، ملاحظات العروض والبروفات،
تفاصيل الطلب، تعليق العميل، ونبذة المطبعة.
"""
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

_AR_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')

# موبايل مصري (01x + ٨ أرقام) بعد شيل الفواصل بين الأرقام، أو أي ١٠ أرقام ورا بعض
_PHONE_RE = re.compile(r'(?:\+?20|0)1[0125]\d{8}|\d{10,}')
_EMAIL_RE = re.compile(r'[\w.+-]+\s*(?:@|\(at\)|\[at\])\s*[\w-]+\s*\.\s*[a-z]{2,}', re.I)
_URL_RE = re.compile(
    r'https?://|www\.|wa\.me|t\.me|bit\.ly|fb\.me|m\.me|instagram\.com|facebook\.com|'
    r'\b[\w-]+\.(?:com|net|org|eg|me|io|co|app|link)\b', re.I)
_HANDLE_RE = re.compile(r'(?<![\w.])@[A-Za-z0-9_.]{3,}')
_WORDS_RE = re.compile(
    r'واتس|وتس|واتساب|whats\s*app|whatsapp|\bwhats\b|تليجرام|تلجرام|telegram|سجنال|signal|'
    r'رقمي|رقم\s*موبايل|رقم\s*تليفون|رقم\s*الموبايل|كلمني\s*على|كلمنى\s*على|اتصل\s*بي|'
    r'ماسنجر|messenger|انستا\s*باي|instapay|فودافون\s*كاش|vodafone',
    re.I)


def _digits_view(text):
    """يحوّل الأرقام العربي لإنجليزي ويشيل الفواصل اللي بين الأرقام (مسافة/شرطة/نقطة)."""
    t = (text or '').translate(_AR_DIGITS)
    return re.sub(r'(?<=\d)[\s\-.()_/]{1,3}(?=\d)', '', t)


def find_contact(text):
    """يرجّع سبب المنع (نص) لو فيه وسيلة تواصل، أو '' لو النص سليم."""
    if not text:
        return ''
    if _PHONE_RE.search(_digits_view(text)):
        return _('أرقام الموبايل ممنوعة — كل التواصل من جوه المنصة.')
    if _EMAIL_RE.search(text):
        return _('الإيميلات ممنوعة — كل التواصل من جوه المنصة.')
    if _URL_RE.search(text) or _HANDLE_RE.search(text):
        return _('اللينكات والحسابات الخارجية ممنوعة — كل التواصل من جوه المنصة.')
    if _WORDS_RE.search(text):
        return _('ممنوع طلب التواصل أو الدفع برّه المنصة — الدفع لـ Mouss Tec بس.')
    return ''


def clean_text(text, max_len=None):
    """ينضّف النص ويرمي ValidationError لو فيه وسيلة تواصل."""
    text = (text or '').strip()
    if max_len:
        text = text[:max_len]
    reason = find_contact(text)
    if reason:
        raise ValidationError(reason)
    return text
