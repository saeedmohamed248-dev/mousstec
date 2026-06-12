"""
🔍 Diagnostics Quota Service — 2026 Relaunch

Single source of truth for "can this tenant run a diagnostics scan / ask the
bot?". Consumption order:

    1. Plan's monthly allowance (resets on 1st of each calendar month).
    2. Top-up balance (one-time purchases of DiagnosticsTopUpPack rows).

Top-up balance is never auto-reset; it only goes down when the tenant uses
it after exhausting the monthly allowance.

The "kind" parameter chooses which counter is checked:
    - 'scan' → diagnostics page scans
    - 'bot'  → diagnostics chat-bot turns

Both share the same top-up pool (a 150 EGP / 30 uses pack covers either).

Usage:

    from clients.services.diagnostics_quota import (
        check_quota, consume_quota, QuotaResult,
    )

    res = check_quota(tenant, kind='scan')
    if not res.allowed:
        return JsonResponse({"error": "quota_exhausted",
                             "reason": res.reason,
                             "upgrade_url": "/subscription/topup/diagnostics/"},
                            status=402)
    consume_quota(tenant, kind='scan')   # → deducts and persists
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

from django.db import transaction
from django.db.models import F

KindT = Literal['scan', 'bot']


@dataclass
class QuotaResult:
    allowed: bool
    reason: str
    plan_remaining: int           # remaining in monthly allowance
    topup_remaining: int          # remaining in top-up pool
    plan_limit: int               # plan's monthly limit (0 = none)
    used_this_period: int         # consumed this period

    @property
    def total_remaining(self) -> int:
        return self.plan_remaining + self.topup_remaining


def _today() -> date:
    from django.utils import timezone
    return timezone.localdate()


def _period_start_for(d: date) -> date:
    return d.replace(day=1)


def _ensure_period_current(sub) -> bool:
    """Roll the period counters if we crossed into a new calendar month.
    Returns True if a reset happened. Caller must save when True.
    """
    today = _today()
    expected_start = _period_start_for(today)
    if sub.diag_period_start != expected_start:
        sub.diag_period_start = expected_start
        sub.diag_scans_used_this_period = 0
        sub.diag_bot_used_this_period = 0
        return True
    return False


def _plan_limit(sub, kind: KindT) -> int:
    if not sub.plan:
        return 0
    if kind == 'scan':
        return sub.plan.monthly_diagnostics_scans_quota or 0
    return sub.plan.monthly_diagnostics_bot_quota or 0


def _used(sub, kind: KindT) -> int:
    return (sub.diag_scans_used_this_period if kind == 'scan'
            else sub.diag_bot_used_this_period)


def _build_result(sub, kind: KindT, allowed: bool, reason: str) -> QuotaResult:
    limit = _plan_limit(sub, kind)
    used = _used(sub, kind)
    return QuotaResult(
        allowed=allowed,
        reason=reason,
        plan_remaining=max(0, limit - used),
        topup_remaining=sub.diag_topup_balance or 0,
        plan_limit=limit,
        used_this_period=used,
    )


def _get_subscription(tenant):
    sub = getattr(tenant, 'subscription', None)
    if sub is None:
        # Lazy fetch — tenant might not have prefetched the related object.
        from clients.models import TenantSubscription
        sub = TenantSubscription.objects.filter(tenant=tenant).first()
    return sub


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
def check_quota(tenant, *, kind: KindT) -> QuotaResult:
    """Read-only check. Does NOT consume. Auto-rolls the monthly period."""
    sub = _get_subscription(tenant)
    if sub is None:
        return QuotaResult(False, 'لا يوجد اشتراك نشط', 0, 0, 0, 0)

    if _ensure_period_current(sub):
        sub.save(update_fields=[
            'diag_period_start',
            'diag_scans_used_this_period',
            'diag_bot_used_this_period',
        ])

    limit = _plan_limit(sub, kind)
    used = _used(sub, kind)

    if used < limit:
        return _build_result(sub, kind, True, 'الحصة الشهرية متاحة')

    if (sub.diag_topup_balance or 0) > 0:
        return _build_result(sub, kind, True, 'استخدام من رصيد الشحن')

    reason = (
        f"انتهت حصة {'الفحوصات' if kind == 'scan' else 'البوت'} الشهرية "
        f"({limit}/شهر). اشحن باقة 30 استخدام بـ 150 ج.م للاستمرار."
    )
    return _build_result(sub, kind, False, reason)


@transaction.atomic
def consume_quota(tenant, *, kind: KindT) -> QuotaResult:
    """Atomically consume 1 unit. Re-checks quota under row lock so two
    parallel requests can't both pass when only 1 unit is left.

    Returns the result *after* consumption. If allowed=False, nothing was
    deducted.
    """
    from clients.models import TenantSubscription

    sub = _get_subscription(tenant)
    if sub is None:
        return QuotaResult(False, 'لا يوجد اشتراك نشط', 0, 0, 0, 0)

    # Lock the subscription row for the duration of this transaction.
    sub = TenantSubscription.objects.select_for_update().get(pk=sub.pk)

    _ensure_period_current(sub)

    limit = _plan_limit(sub, kind)
    used = _used(sub, kind)

    use_field = ('diag_scans_used_this_period' if kind == 'scan'
                 else 'diag_bot_used_this_period')

    if used < limit:
        setattr(sub, use_field, used + 1)
        sub.save(update_fields=[
            use_field, 'diag_period_start',
            'diag_scans_used_this_period', 'diag_bot_used_this_period',
        ])
        return _build_result(sub, kind, True, 'تم خصم 1 من الحصة الشهرية')

    if (sub.diag_topup_balance or 0) > 0:
        sub.diag_topup_balance = F('diag_topup_balance') - 1
        sub.save(update_fields=['diag_topup_balance'])
        sub.refresh_from_db(fields=['diag_topup_balance'])
        return _build_result(sub, kind, True, 'تم خصم 1 من رصيد الشحن')

    return _build_result(sub, kind, False, 'الحصة منتهية ولا يوجد رصيد شحن')


@transaction.atomic
def add_topup(tenant, uses: int) -> int:
    """Grant top-up uses to the tenant (typically called after a successful
    purchase of a DiagnosticsTopUpPack). Returns the new balance."""
    from clients.models import TenantSubscription
    sub = _get_subscription(tenant)
    if sub is None:
        return 0
    sub = TenantSubscription.objects.select_for_update().get(pk=sub.pk)
    sub.diag_topup_balance = (sub.diag_topup_balance or 0) + max(0, int(uses))
    sub.save(update_fields=['diag_topup_balance'])
    return sub.diag_topup_balance
