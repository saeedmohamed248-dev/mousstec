"""
🛍️ السوق المفتوح (مناقصات العملاء) — قفل دورة الطلب المقبول.

🐛 [FIX — دورة ناقصة]: الطلب المقبول كان بيتقفل «مكتمل» بس لو العميل قيّم التاجر — مفيش
تذكير ولا قفل تلقائي، فطلبات كتير بتفضل «مقبول» للأبد، وتقييم التجار (اللي بيترتّبوا بيه)
مابيتجمعش. زي الأسواق العالمية (Thumbtack / Upwork):

* بعد ``RATING_REMINDER_AFTER`` من القبول ⇒ تذكير واحد للعميل يقيّم.
* بعد ``AUTO_COMPLETE_AFTER`` ⇒ الطلب بيتقفل «مكتمل» تلقائي — والعميل لسه يقدر يقيّم.

بيشتغل يومياً من ``clients.tasks.close_stale_tender_requests``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models.functions import Coalesce
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

RATING_REMINDER_AFTER = timedelta(days=7)
AUTO_COMPLETE_AFTER = timedelta(days=30)


def _notify(customer, **kwargs):
    if customer is None:
        return
    from clients.models import CustomerNotification
    try:
        CustomerNotification.objects.create(customer=customer, **kwargs)
    except Exception:  # noqa: BLE001 — الإشعار مايوقّفش القفل
        logger.exception("[TENDERS] notification failed for customer %s", getattr(customer, 'pk', None))


def close_stale_accepted_requests(now=None) -> dict:
    """تذكير بالتقييم + قفل تلقائي للطلبات المقبولة القديمة. بيرجّع {'reminded': n, 'closed': n}."""
    from clients.models import ServiceRequest

    now = now or timezone.now()
    accepted = (ServiceRequest.objects.filter(status='accepted', accepted_offer__isnull=False)
                .annotate(started=Coalesce('accepted_at', 'created_at'))
                .select_related('customer', 'accepted_offer'))

    closed = 0
    for req in list(accepted.filter(started__lte=now - AUTO_COMPLETE_AFTER)[:500]):
        # مشروط بالحالة: لو العميل قيّم في نفس اللحظة مانكتبش فوقه
        if not ServiceRequest.objects.filter(pk=req.pk, status='accepted').update(
                status='completed', completed_at=now):
            continue
        closed += 1
        _notify(req.customer, title='✅ قفلنا طلبك كمكتمل',
                body=(f'«{req.title[:80]}» عدّى عليه {AUTO_COMPLETE_AFTER.days} يوم من قبول العرض فاتقفل مكتمل. '
                      'لو اتعاملت مع التاجر قيّمه — تقييمك بيساعد العملاء التانيين.'),
                level='info', icon='fa-check-double',
                action_url=f'/marketplace/request/{req.request_code}/', action_label='قيّم التاجر')

    reminded = 0
    for req in list(accepted.filter(started__lte=now - RATING_REMINDER_AFTER,
                                    rating_reminded_at__isnull=True)[:500]):
        if not ServiceRequest.objects.filter(pk=req.pk, status='accepted',
                                             rating_reminded_at__isnull=True).update(rating_reminded_at=now):
            continue
        reminded += 1
        _notify(req.customer, title='⭐ اتعاملت مع التاجر؟',
                body=f'قيّم التاجر اللي قبلت عرضه على «{req.title[:80]}» — دقيقة واحدة.',
                level='info', icon='fa-star',
                action_url=f'/marketplace/request/{req.request_code}/', action_label='قيّم التاجر')

    if closed or reminded:
        logger.info("[TENDERS] rating reminders=%d auto-completed=%d", reminded, closed)
    return {'reminded': reminded, 'closed': closed}
