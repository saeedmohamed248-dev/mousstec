"""
🔔 إشعار صاحب المنصة بحاجة مستنياه هو (موافقة طلب / تأكيد دفع / مشكلة مفتوحة).

🐛 [FIX — دورات ناقصة]: إيصالات الدفع في سوق المطابع، والمشاكل المفتوحة، وطلبات السوق المفتوح
المستنية موافقة — كلها كانت بتستنى صاحب المنصة يفتح الأدمن بالصدفة، والطلب واقف لحد كده.

نفس مستلمين إشعار طلبات الكتالوج: إيميل CATALOG_NOTIFY_EMAIL / SUPPORT_INBOX_EMAIL
وواتساب CATALOG_NOTIFY_WHATSAPP / SUPPORT_WHATSAPP. بيتبعت بعد الـ commit في thread
عشان مايبطّأش صفحة العميل، ومابيوقّعش أي حاجة لو فشل.
"""
import logging
import threading

from django.conf import settings
from django.db import connection, transaction

log = logging.getLogger('mouss_tec_core')


def _send(subject, body):
    to = getattr(settings, 'CATALOG_NOTIFY_EMAIL', '') or getattr(settings, 'SUPPORT_INBOX_EMAIL', '')
    if to:
        try:
            from django.core.mail import send_mail
            send_mail(f'[Mousstec 🔔] {subject}', body, settings.DEFAULT_FROM_EMAIL, [to], fail_silently=True)
        except Exception:  # noqa: BLE001
            log.exception('[OWNER NOTIFY] email failed')
    wa = getattr(settings, 'CATALOG_NOTIFY_WHATSAPP', '') or getattr(settings, 'SUPPORT_WHATSAPP', '')
    if wa and getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') and getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', ''):
        try:
            from clients.services.whatsapp_notify import send_platform_whatsapp
            send_platform_whatsapp(wa, text=f'🔔 {subject}\n{body}'[:1500])
        except Exception:  # noqa: BLE001
            log.exception('[OWNER NOTIFY] whatsapp failed')


def notify_owner(subject, body, path=''):
    """path: مسار في الموقع (هيتحط قبله الدومين الرئيسي) — صفحة الأدمن اللي فيها الإجراء."""
    base = (getattr(settings, 'PRINT_MARKET_BASE_URL', '') or 'https://mousstec.com').rstrip('/')
    full = f'{body}\n\n{base}{path}' if path else body

    def _run():
        try:
            _send(subject, full)
        finally:
            connection.close()

    def _dispatch():
        if getattr(settings, 'OWNER_NOTIFY_SYNC', False):   # الاختبارات
            _send(subject, full)
        else:
            threading.Thread(target=_run, daemon=True).start()

    transaction.on_commit(_dispatch)
