"""
🔁 التجديد التلقائي للاشتراك بالكارت المحفوظ (Paymob Card Tokenization).

الدورة:
  1. العميل بيدفع بالكارت ويختار «احفظ الكارت» ⇒ Paymob بيبعت ``type=TOKEN``
     ⇒ ``save_card_from_token_callback`` (الشركة بتتعرف من فاتورة الطلب).
  2. العميل بيفعّل «تجديد تلقائي» من صفحة الاشتراك (``set_auto_renew``) — مش
     تلقائي من نفسه: الخصم من غير ما العميل يدوس لازم يكون بموافقته الصريحة.
  3. كل يوم ``renew_due_subscriptions`` (Celery beat) بيخصم من الكارت قبل نهاية
     الاشتراك بيوم — بنفس مسار الدفع بالكارت: فاتورة «مصدرة» بمرجع رقم الطلب قبل
     الخصم ⇒ تتسوّى (``settle_gateway_invoice``) ⇒ الاشتراك يتمد.

الأمان:
  * محاولة واحدة في اليوم لكل شركة (``auto_renew_last_attempt`` بيتسجّل قبل الخصم)
    — حتى لو المهمة اشتغلت مرتين أو الـ worker اتعمله restart.
  * رد البوابة ضاع وسط الخصم (``pay_network_error``) ⇒ مابنعيدش المحاولة: الفاتورة
    بتفضل مستحقة والـ webhook بيسوّيها لو الخصم تم، والتجديد بيتقفل لحد ما العميل يشوف.
  * ٣ رفضات ⇒ التجديد التلقائي بيتقفل والعميل بيتبلّغ (بعدها فترة السماح العادية).
  * المفتاح ``PAYMOB_AUTO_RENEW`` مقفول افتراضياً: الكروت مابتتحفظش والمهمة مابتعملش حاجة.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

PROVIDER = 'paymob'
MAX_FAILURES = 3
RENEW_DAYS_BEFORE = 1          # الخصم قبل نهاية الاشتراك بيوم
GRACE_DAYS = 3                 # نفس فترة السماح في orchestrate_billing_and_suspensions


def _setting(name):
    return str(getattr(settings, name, '') or os.getenv(name, '') or '').strip()


def auto_renew_enabled() -> bool:
    """المفتاح + المفاتيح اللازمة للخصم والتحقق من إشعارات Paymob."""
    if not getattr(settings, 'PAYMOB_AUTO_RENEW', False):
        return False
    integration = _setting('PAYMOB_MOTO_INTEGRATION_ID') or _setting('PAYMOB_INTEGRATION_ID')
    return bool(_setting('PAYMOB_API_KEY') and _setting('PAYMOB_HMAC_SECRET') and integration.isdigit())


def active_card(tenant):
    from clients.models import SavedPaymentCard
    return SavedPaymentCard.objects.filter(tenant=tenant, provider=PROVIDER, is_active=True).first()


# ─────────────────────────────────────────────────────────────────────
# 1. حفظ الكارت (type=TOKEN — بعد التحقق من التوقيع)
# ─────────────────────────────────────────────────────────────────────
@transaction.atomic
def save_card_from_token_callback(obj: dict):
    """بيرجّع (الحالة، الكارت): saved | ignored | not_found | bad_payload."""
    from clients.models import PlatformInvoice, SavedPaymentCard
    if not auto_renew_enabled():
        return 'ignored', None
    token = str(obj.get('token') or '').strip()
    order_id = str(obj.get('order_id') or '').strip()
    if not token or not order_id:
        return 'bad_payload', None
    inv = (PlatformInvoice.objects.filter(payment_provider=PROVIDER, payment_reference=order_id)
           .select_related('tenant').first())
    if inv is None:
        return 'not_found', None        # طلب مش اشتراك (متجر التصاميم/قطع الغيار…)
    card, _ = SavedPaymentCard.objects.update_or_create(
        provider=PROVIDER, token=token,
        defaults={
            'tenant': inv.tenant, 'is_active': True, 'source_order': order_id,
            'masked_pan': str(obj.get('masked_pan') or '')[:32],
            'card_subtype': str(obj.get('card_subtype') or '')[:20],
        },
    )
    SavedPaymentCard.objects.filter(tenant=inv.tenant, is_active=True).exclude(pk=card.pk).update(is_active=False)
    logger.info("💳 [AUTO-RENEW] card •••• %s saved for %s", card.last4, inv.tenant.schema_name)
    return 'saved', card


# ─────────────────────────────────────────────────────────────────────
# 2. اختيار العميل (صفحة إدارة الاشتراك)
# ─────────────────────────────────────────────────────────────────────
def set_auto_renew(tenant, on: bool) -> bool:
    """تشغيل/إيقاف. التشغيل محتاج باقة وكارت محفوظ."""
    from clients.models import TenantSubscription
    sub = TenantSubscription.objects.filter(tenant=tenant).first()
    if sub is None:
        return False
    if on and (not auto_renew_enabled() or sub.plan_id is None or active_card(tenant) is None):
        return False
    sub.auto_renew = bool(on)
    if on:
        sub.auto_renew_failures = 0
    sub.save(update_fields=['auto_renew', 'auto_renew_failures', 'updated_at'])
    _event(tenant, 'subscription', 'تفعيل التجديد التلقائي' if on else 'إيقاف التجديد التلقائي')
    return True


def remove_card(tenant):
    """العميل شال الكارت ⇒ مفيش خصم تاني."""
    from clients.models import SavedPaymentCard, TenantSubscription
    SavedPaymentCard.objects.filter(tenant=tenant, is_active=True).update(is_active=False)
    TenantSubscription.objects.filter(tenant=tenant, auto_renew=True).update(auto_renew=False)
    _event(tenant, 'subscription', 'حذف الكارت المحفوظ وإيقاف التجديد التلقائي')


# ─────────────────────────────────────────────────────────────────────
# 3. الخصم اليومي
# ─────────────────────────────────────────────────────────────────────
def due_subscriptions(today=None):
    from clients.models import TenantSubscription
    today = today or timezone.localdate()
    return (TenantSubscription.objects
            .filter(auto_renew=True, plan__isnull=False, tenant__is_active=True,
                    tenant__status='active',
                    tenant__subscription_end_date__lte=today + timedelta(days=RENEW_DAYS_BEFORE),
                    tenant__subscription_end_date__gte=today - timedelta(days=GRACE_DAYS))
            .exclude(auto_renew_last_attempt=today)
            .select_related('tenant', 'plan'))


def renew_due_subscriptions(today=None) -> dict:
    """بيرجّع عدّاد النتايج — للـ log والاختبارات."""
    counts = {}
    if not auto_renew_enabled():
        return counts
    today = today or timezone.localdate()
    for sub in list(due_subscriptions(today)):
        try:
            outcome = renew_one(sub.pk, today)
        except Exception:
            logger.exception("🔴 [AUTO-RENEW] %s crashed", sub.tenant.schema_name)
            outcome = 'error'
        counts[outcome] = counts.get(outcome, 0) + 1
    if counts:
        logger.info("🔁 [AUTO-RENEW] %s", counts)
    return counts


def renew_one(sub_pk, today=None) -> str:
    """renewed | declined | disabled | unknown | no_card | skipped."""
    from billing.services.paymob import charge_with_saved_token
    from clients.models import TenantSubscription
    from clients.services.platform_billing import (
        PERIOD_DAYS, open_gateway_invoice, settle_gateway_invoice, void_invoice,
    )
    today = today or timezone.localdate()

    # المحاولة بتتسجّل وبتتقفل (commit) قبل أي اتصال بالبوابة — مفيش خصمين في يوم
    with transaction.atomic():
        sub = TenantSubscription.objects.select_for_update(of=('self',)).select_related('tenant', 'plan').get(pk=sub_pk)
        if not sub.auto_renew or sub.auto_renew_last_attempt == today:
            return 'skipped'
        sub.auto_renew_last_attempt = today
        sub.save(update_fields=['auto_renew_last_attempt'])

    tenant, plan = sub.tenant, sub.plan
    card = active_card(tenant)
    if card is None:
        _disable(sub, 'مفيش كارت محفوظ')
        return 'no_card'
    months = sub.billing_cycle_months if sub.billing_cycle_months in PERIOD_DAYS else 1
    amount = plan.price_for_period(months)
    if amount <= 0:
        return 'skipped'

    opened = {}

    def _open_invoice(order_id):
        opened['inv'] = open_gateway_invoice(tenant, plan, months, provider=PROVIDER, reference=order_id)

    ok, detail = charge_with_saved_token(
        card.token, amount, order_ref=f'autorenew_{tenant.schema_name}',
        item_name=f'Mouss Tec {plan.name} — {months}m auto-renewal',
        customer_email=(tenant.email or 'customer@mousstec.com'), on_order=_open_invoice,
    )
    inv = opened.get('inv')

    if ok and inv is not None:
        # رد البوابة نفسه (اتصال موثّق من السيرفر) — والـ webhook لما يوصل بيلاقيها «مدفوعة»
        outcome, inv = settle_gateway_invoice(PROVIDER, inv.payment_reference,
                                              paid_cents=int((inv.total * 100).to_integral_value()))
        TenantSubscription.objects.filter(pk=sub.pk).update(auto_renew_failures=0)
        _event(tenant, 'payment', f'تجديد تلقائي ناجح — فاتورة {inv.invoice_number} ({inv.total} ج.م، '
                                  f'كارت •••• {card.last4})', {'invoice': inv.invoice_number, 'txn': detail})
        _notify(tenant, 'تم تجديد اشتراكك تلقائياً — Mouss Tec',
                f'تم خصم {inv.total} ج.م من الكارت •••• {card.last4} وتجديد اشتراكك لحد {inv.period_end}.\n'
                f'رقم الفاتورة: {inv.invoice_number}')
        return 'renewed'

    if detail == 'pay_network_error':
        # الخصم ممكن يكون تم — الفاتورة بتفضل «مصدرة» والـ webhook يسوّيها. مفيش محاولة تانية.
        _disable(sub, 'رد بوابة الدفع ضاع وسط الخصم — راجع العملية على Paymob', event_type='error')
        return 'unknown'

    if inv is not None:
        void_invoice(inv, reason=f'تجديد تلقائي مرفوض: {detail}')
    failures = sub.auto_renew_failures + 1
    TenantSubscription.objects.filter(pk=sub.pk).update(auto_renew_failures=failures)
    logger.warning("⚠️ [AUTO-RENEW] %s declined (%s) — attempt %s/%s",
                   tenant.schema_name, detail, failures, MAX_FAILURES)
    if failures >= MAX_FAILURES:
        sub.auto_renew_failures = failures
        _disable(sub, f'الكارت اترفض {failures} مرات ({detail})')
        return 'disabled'
    return 'declined'


def _disable(sub, reason, event_type='subscription'):
    from clients.models import TenantSubscription
    TenantSubscription.objects.filter(pk=sub.pk).update(auto_renew=False)
    _event(sub.tenant, event_type, f'إيقاف التجديد التلقائي: {reason}')
    _notify(sub.tenant, 'التجديد التلقائي لاشتراكك اتوقف — Mouss Tec',
            f'ماقدرناش نجدد اشتراكك تلقائياً ({reason}).\n'
            f'اشتراكك بينتهي {sub.tenant.subscription_end_date} — جدّد من صفحة الاشتراك عشان الخدمة ماتقفش.')


def _event(tenant, event_type, description, metadata=None):
    try:
        from clients.models import PlatformEvent
        PlatformEvent.objects.create(event_type=event_type, tenant_schema=tenant.schema_name,
                                     tenant_name=tenant.name, user_name='system',
                                     description=description[:1000], metadata=metadata)
    except Exception:
        logger.exception("[AUTO-RENEW] audit event failed")


def _notify(tenant, subject, body):
    recipient = (tenant.email or '').strip()
    if not recipient or not getattr(settings, 'EMAIL_HOST', ''):
        return
    from django.core.mail import EmailMessage
    msg = EmailMessage(subject=subject, body=f'عزيزي {tenant.name}،\n\n{body}\n\nMouss Tec 🚀', to=[recipient])
    msg.encoding = 'utf-8'
    try:
        msg.send(fail_silently=True)
    except Exception:
        logger.warning("[AUTO-RENEW] email to %s failed", tenant.schema_name, exc_info=True)
