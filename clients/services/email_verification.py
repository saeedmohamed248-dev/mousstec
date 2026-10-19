"""✉️ تأكيد إيميل صاحب الشركة الجديدة.

كان «شكلي»: صفحة نجاح التسجيل بتعرض رابط التأكيد وبتحوّل عليه لوحدها بعد ٥
ثواني — يعني أي حد يسجّل بإيميل مش بتاعه والشركة تتفعّل. دلوقتي:

* لو الإرسال متظبط (Brevo API أو SMTP كامل) ⇒ الرابط بيروح **على الإيميل بس**
  والصفحة بتقول «افتح بريدك» مع زرار إعادة إرسال.
* لو مش متظبط (تطوير/سيرفر من غير إيميل) أو الإرسال فشل فعلاً ⇒ زي الأول
  (الرابط على الشاشة) — عشان محدش يتقفل برّه حسابه بسبب إعدادات.

الإعداد: SIGNUP_EMAIL_VERIFICATION = auto (الافتراضي) | required | off.
"""
import logging
import time

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMessage

logger = logging.getLogger('mouss_tec_core')

SALT = 'tenant-email-verify'
MAX_AGE = 60 * 60 * 48   # الرابط صالح ٤٨ ساعة


def delivery_configured() -> bool:
    """فيه طريقة إرسال حقيقية متظبطة؟ (console/locmem/dummy ⇒ لأ)."""
    backend = getattr(settings, 'EMAIL_BACKEND', '') or ''
    if backend.endswith('BrevoAPIEmailBackend'):
        key = str(getattr(settings, 'BREVO_API_KEY', '') or '').strip().strip('"\'')
        return key.startswith('xkeysib-')
    if backend.endswith('smtp.EmailBackend'):
        return all(getattr(settings, name, '') for name in
                   ('EMAIL_HOST', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD'))
    return False


def verification_required() -> bool:
    mode = (getattr(settings, 'SIGNUP_EMAIL_VERIFICATION', 'auto') or 'auto').strip().lower()
    if mode == 'required':
        return True
    if mode == 'off':
        return False
    return delivery_configured()


def make_token(schema_name, user_id, email) -> str:
    return signing.dumps({'schema_name': schema_name, 'user_id': user_id,
                          'email': email, 'created': int(time.time())}, salt=SALT)


def verify_url(request, token, host=None) -> str:
    return f"{request.scheme}://{host or request.get_host()}/account/verify-email/?token={token}"


def send_verification_email(to, *, company, url, name='', resend=False) -> bool:
    """بيبعت الرابط ويستنى النتيجة (Brevo بياخد ثانية). True = اتبعت فعلاً."""
    greeting = f"أهلاً {name}،\n\n" if name else ''
    msg = EmailMessage(
        subject='✉️ رابط تأكيد جديد | Mouss Tec' if resend else '✉️ أكّد بريدك الإلكتروني | Mouss Tec',
        body=(f"{greeting}اضغط الرابط لتفعيل حساب {company}:\n\n{url}\n\n"
              f"الرابط صالح لمدة 48 ساعة.\n\nMouss Tec"),
        from_email=None,
        to=[to],
    )
    # 🐛 الـ subject/body عربي — من غير utf-8 الـ SMTP كان بيرمي 'ascii' codec
    msg.encoding = 'utf-8'
    try:
        return msg.send(fail_silently=False) == 1
    except Exception as exc:  # noqa: BLE001 — أي فشل ⇒ الرابط يظهر على الشاشة بدل ما العميل يتقفل
        logger.error("[EMAIL VERIFY] sending to %s failed: %s", to, exc)
        return False
