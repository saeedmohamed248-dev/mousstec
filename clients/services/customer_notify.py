"""
🔔 إشعارات عملاء السوق من قرارات الإدارة — مكان واحد.

🐛 [FIX]: قرارات كتير في لوحة المشرف كانت بتخلص من غير ما العميل يعرف: تأكيد/رفض إيصال
فودافون كاش، تفعيل/رفض باقة تصاميم، نشر/رفض قطعة غيار، كل خطوة في طلب الطباعة.
العميل كان بيكتشف بالصدفة (أو مابيكتشفش خالص لو الإيصال اترفض).
"""
from __future__ import annotations

import logging

logger = logging.getLogger('mouss_tec_core')


def notify_customer(customer, *, title, body, level='info', icon='fa-bell',
                    action_url='', action_label='', sent_by=None):
    """إشعار في جرس العميل — best-effort: عمره ما يوقّع الإجراء الأساسي."""
    if customer is None or (getattr(customer, 'phone', '') or '').startswith('+B2B'):
        return None   # حسابات التجار الوسيطة مالهاش صندوق إشعارات
    from clients.models import CustomerNotification
    try:
        return CustomerNotification.objects.create(
            customer=customer, title=title[:200], body=body, level=level, icon=icon[:50],
            action_url=action_url[:300], action_label=action_label[:80],
            sent_by=sent_by if getattr(sent_by, 'is_authenticated', False) else None,
        )
    except Exception:
        logger.exception("[NOTIFY] customer notification failed for %s", getattr(customer, 'pk', None))
        return None


# صفحة العميل اللي يروحلها من إشعار الإيصال حسب نوع الشراء
_RECEIPT_PAGES = {
    'design': ('/marketplace/design-store/', 'متجر التصاميم'),
    'parts': ('/marketplace/parts/orders/', 'طلباتي'),
    'diagnostics': ('/marketplace/diagnostics/scan/', 'افتح التشخيص'),
}


def notify_receipt_outcome(receipt, *, by_user=None):
    """إيصال فودافون كاش/إنستاباي اتأكد أو اترفض ⇒ العميل يعرف (والسبب لو اترفض)."""
    if not receipt.customer_id:
        return None
    url, label = _RECEIPT_PAGES.get(receipt.purchase_type, ('/marketplace/dashboard/', 'حسابي'))
    what = receipt.get_purchase_type_display()
    amount = f'{receipt.amount:,.2f}'
    if receipt.status == 'confirmed':
        return notify_customer(
            receipt.customer, title='✅ تحويلك اتأكد',
            body=f'إيصال «{what}» بمبلغ {amount} اتراجع واتأكد، والخدمة اتفعّلت.',
            level='success', icon='fa-circle-check', action_url=url, action_label=label, sent_by=by_user)
    if receipt.status == 'rejected':
        return notify_customer(
            receipt.customer, title='❌ إيصال التحويل اترفض',
            body=(f'إيصال «{what}» بمبلغ {amount} اترفض: {receipt.review_notes or "لم يتم العثور على التحويل"}. '
                  'لو حوّلت فعلاً ابعتلنا رقم العملية من الشات أو الدعم.'),
            level='danger', icon='fa-circle-xmark', action_url=url, action_label=label, sent_by=by_user)
    return None
