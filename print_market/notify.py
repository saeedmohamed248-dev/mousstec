"""📧 إشعارات سوق المطابع بالإيميل — بتتبعت بعد الـ commit ومابتوقّعش الطلب لو الإيميل فشل."""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse

log = logging.getLogger(__name__)


def notify(request, account, subject, line, order=None):
    if not account or not account.email:
        return
    link = request.build_absolute_uri(
        reverse('print_market:order_detail', args=[order.code]) if order else reverse('print_market:home'))
    body = f'أهلاً {account.name}،\n\n{line}\n\nافتح الطلب: {link}\n\n— سوق المطابع | Mouss Tec'
    to = account.email

    def _send():
        try:
            send_mail(f'سوق المطابع — {subject}', body, settings.DEFAULT_FROM_EMAIL, [to], fail_silently=True)
        except Exception:  # noqa: BLE001 — الإيميل مايوقّعش الطلب
            log.exception('print_market notify failed')

    transaction.on_commit(_send)
