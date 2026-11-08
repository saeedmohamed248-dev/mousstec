"""
🔔 تنبيهات المبيعات لصاحب المنصة — «زبون دخل المحل»
====================================================
* شركة جديدة سجّلت ⇒ إيميل فوري (SUPPORT_INBOX_EMAIL) فيه الاسم والمجال والموبايل والإيميل
  ورابط واتساب جاهز تكلّمه منه — أول ساعة بعد التسجيل هي أسخن وقت للمكالمة.
* عميل بعت ملف بضاعته عشان نرفعهاله (ImportAssistRequest) ⇒ نفس الإيميل بالملف.
الواتساب من رقم المنصة بيتبعت كمان لو مفاتيحه متظبطة (من غيرها مابيعملش حاجة).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger('mouss_tec_core')


def _owner_email():
    return getattr(settings, 'SUPPORT_INBOX_EMAIL', '') or settings.DEFAULT_FROM_EMAIL


def _wa_link(phone):
    from clients.services.whatsapp_notify import normalize_phone
    num = normalize_phone(phone or '')
    return f"https://wa.me/{num}" if num else ''


def _notify(subject, body):
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [_owner_email()], fail_silently=True)
    except Exception:   # noqa: BLE001 — التنبيه مايوقّعش التسجيل
        logger.exception('[SALES ALERT] email failed')
    try:
        from clients.services.whatsapp_notify import send_platform_whatsapp
        send_platform_whatsapp(getattr(settings, 'SUPPORT_WHATSAPP', ''), text=f"{subject}\n\n{body}"[:1500])
    except Exception:   # noqa: BLE001
        logger.exception('[SALES ALERT] whatsapp failed')


def new_signup(tenant):
    """🔔 شركة جديدة سجّلت دلوقتي."""
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    subject = f"🔔 تسجيل جديد: {tenant.name} ({tenant.get_industry_display()})"
    body = "\n".join(filter(None, [
        f"الشركة: {tenant.name}",
        f"المسؤول: {tenant.owner_name}",
        f"المجال: {tenant.get_industry_display()}",
        f"الموبايل: {tenant.phone}",
        f"واتساب: {_wa_link(tenant.phone)}",
        f"الإيميل: {tenant.email or '—'}",
        f"الرابط: https://{tenant.schema_name.replace('_', '-')}.{base}/",
        "",
        "💡 كلّمه في أول ساعة: «أهلاً من موس تك — ابعتلي ملف بضاعتك وأنا أرفعهولك».",
    ]))
    _notify(subject, body)


def import_request(req):
    """📦 عميل بعت ملف بضاعته عشان نرفعهاله."""
    t = req.tenant
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    admin_url = getattr(settings, 'ADMIN_URL', 'secure-portal')
    subject = f"📦 طلب رفع بضاعة: {t.name}"
    body = "\n".join(filter(None, [
        f"الشركة: {t.name} ({t.get_industry_display()})",
        f"الموبايل: {req.contact_phone or t.phone}",
        f"واتساب: {_wa_link(req.contact_phone or t.phone)}",
        f"الملف: {req.file.name.rsplit('/', 1)[-1] if req.file else '—'}",
        f"ملاحظة العميل: {req.note}" if req.note else '',
        f"افتح الطلب: https://{base}/{admin_url}/clients/importassistrequest/{req.pk}/change/",
    ]))
    _notify(subject, body)
