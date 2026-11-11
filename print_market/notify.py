"""
📣 إشعارات سوق المطابع:
* إشعار جوه السوق (جرس + صفحة الإشعارات) — دايماً.
* إيميل — للطرفين.
* واتساب من رقم المنصة — للمطبعة بس لو حاطة رقم (الرقم مابيظهرش للعميل أبداً).
الإيميل والواتساب بيتبعتوا بعد الـ commit ومابيوقّعوش الطلب لو فشلوا.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse

log = logging.getLogger(__name__)


def _link(request, order):
    path = reverse('print_market:order_detail', args=[order.code]) if order else reverse('print_market:home')
    if request is not None:
        return request.build_absolute_uri(path)
    return settings.PRINT_MARKET_BASE_URL.rstrip('/') + path


def notify(request, account, subject, line, order=None):
    """request ممكن يبقى None (مهام الخلفية زي التأكيد التلقائي)."""
    if not account:
        return
    from .models import MarketNotification
    MarketNotification.objects.create(account=account, order=order, title=str(subject)[:150],
                                      body=str(line)[:500])
    link = _link(request, order)
    body = f'أهلاً {account.name}،\n\n{line}\n\nافتح الطلب: {link}\n\n— سوق المطابع | Mouss Tec'
    to, phone = account.email, (account.whatsapp if account.is_shop else '')

    def _send():
        if to:
            try:
                send_mail(f'سوق المطابع — {subject}', body, settings.DEFAULT_FROM_EMAIL, [to], fail_silently=True)
            except Exception:  # noqa: BLE001 — الإيميل مايوقّعش الطلب
                log.exception('print_market email failed')
        if phone:
            _whatsapp(phone, account.name, str(line), link)

    transaction.on_commit(_send)


def _whatsapp(phone, name, line, link):
    if not (getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') and getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')):
        return   # الواتساب مش متظبط — الإشعار جوه السوق والإيميل كفاية
    try:
        from .tasks import send_shop_whatsapp
        send_shop_whatsapp.delay(phone, name, line, link)
    except Exception:  # noqa: BLE001 — من غير Celery/Redis نبعت على طول
        send_shop_whatsapp_now(phone, name, line, link)


def send_shop_whatsapp_now(phone, name, line, link):
    from clients.services.whatsapp_notify import send_platform_whatsapp
    tpl = settings.PRINT_MARKET_WHATSAPP_TEMPLATE
    try:
        return send_platform_whatsapp(
            phone, template=tpl, params=(name, line, link),
            text='' if tpl else f'🖨️ سوق المطابع\n{name}، {line}\n{link}')
    except Exception:  # noqa: BLE001
        log.exception('print_market whatsapp failed')
        return False
