"""
💸 Platform billing helpers used by the owner's control panel.

Manual activations and renewals from the super-admin used to only push
``Client.subscription_end_date`` forward: no ``PlatformInvoice``, the
``TenantSubscription`` period/entitlements left behind. Revenue pages built
on invoices then disagreed with MRR built on active subscriptions, and the
tenant's own billing history had a hole.

Everything here goes through the same path as a Paymob payment:
PlatformInvoice → ``mark_paid()`` (extends the subscription, snapshots the
plan revision's entitlements/price, re-activates the tenant).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

PERIOD_DAYS = {1: 30, 3: 90, 6: 180, 12: 365}


def _revision_for(plan):
    from clients.models import PlanRevision
    rev = PlanRevision.objects.filter(plan=plan).order_by('-effective_from').first()
    if rev is None:
        rev = PlanRevision.create_from_plan(plan, change_reason='Auto-created on admin billing action')
    return rev


@transaction.atomic
def issue_subscription_invoice(tenant, plan, months=1, *, paid=True, gift=False,
                               provider='manual_admin', reference='', notes='', user=None):
    """Issue (and optionally settle) a subscription invoice for ``months``.

    * ``paid``  → ``mark_paid()``: extends the subscription from the later of
      today / current end date, activates the tenant.
    * ``gift``  → total 0 (100% discount), still paid → extends for free but
      shows up as a gift in the billing history instead of a silent date bump.
    * ``paid=False`` → an *issued* invoice the tenant owes (no extension).
    """
    from clients.models import PlatformInvoice, TenantSubscription

    months = int(months or 1)
    if months not in PERIOD_DAYS:
        months = 1
    sub, _ = TenantSubscription.objects.get_or_create(tenant=tenant, defaults={'plan': plan})
    today = timezone.localdate()
    start = max(tenant.subscription_end_date or today, today)
    end = start + timedelta(days=PERIOD_DAYS[months])

    subtotal = (Decimal(str(plan.monthly_price)) * months).quantize(Decimal('0.01'))
    total = Decimal('0.00') if gift else plan.price_for_period(months)
    discount_amount = (subtotal - total).quantize(Decimal('0.01'))
    discount_percent = int(round((discount_amount / subtotal) * 100)) if subtotal else 0

    inv = PlatformInvoice.objects.create(
        tenant=tenant, subscription=sub, plan_revision=_revision_for(plan),
        period_start=start, period_end=end, billing_cycle_months=months,
        subtotal=subtotal, discount_percent=discount_percent, discount_amount=discount_amount,
        total=total, entitlements_snapshot=dict(plan.entitlements or {}),
        status='issued', notes=(('🎁 هدية — ' if gift else '') + (notes or ''))[:1000],
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    if paid or gift:
        inv.mark_paid(payment_provider='gift' if gift else provider,
                      payment_reference=reference or None)
        # Legacy CharField (still read by middleware/pricing)
        tenant.plan = plan.slug
        tenant.save(update_fields=['plan'])
    return inv


def void_invoice(invoice, *, reason='', user=None):
    """Cancel an unpaid invoice (e.g. its manual receipt was rejected)."""
    if invoice.status != 'issued':
        return False
    invoice.status = 'void'
    stamp = f"[void {timezone.now():%Y-%m-%d} {getattr(user, 'username', '') or ''}] {reason}".strip()
    invoice.notes = ((invoice.notes + '\n') if invoice.notes else '') + stamp
    invoice.save(update_fields=['status', 'notes'])
    return True


@transaction.atomic
def refund_invoice(invoice, *, reason='', user=None, cut_service=False):
    """Mark a paid invoice refunded. ``cut_service`` ends the subscription today
    (the money went back) — otherwise access stays until the period end."""
    if invoice.status != 'paid':
        return False
    invoice.status = 'refunded'
    stamp = f"[refund {timezone.now():%Y-%m-%d} {getattr(user, 'username', '') or ''}] {reason}".strip()
    invoice.notes = ((invoice.notes + '\n') if invoice.notes else '') + stamp
    invoice.save(update_fields=['status', 'notes'])
    if cut_service:
        tenant = invoice.tenant
        today = timezone.localdate()
        if tenant.subscription_end_date and tenant.subscription_end_date > today:
            tenant.subscription_end_date = today
            tenant.save(update_fields=['subscription_end_date'])
        sub = invoice.subscription
        if sub and sub.current_period_end and sub.current_period_end > today:
            sub.current_period_end = today
            sub.save(update_fields=['current_period_end'])
    return True
