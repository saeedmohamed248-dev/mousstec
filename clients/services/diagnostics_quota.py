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
from erp_core.localization import current_tenant_symbol as _sym

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


# 🧪 التجربة المجانية: التشخيص مفتوح لـ 5 عربيات (فحوصات) طول التجربة — مش حصة الباقة الشهرية
TRIAL_DIAGNOSTICS_SCANS = 5


def in_free_trial(tenant) -> bool:
    """الشركة في التجربة المجانية (لسه مادفعتش) والتجربة ماخلصتش."""
    if tenant is None or getattr(tenant, 'status', '') != 'trial':
        return False
    ends = getattr(tenant, 'trial_ends_at', None)
    return ends is None or ends >= _today()


def _ensure_period_current(sub) -> bool:
    """Roll the period counters if we crossed into a new calendar month.
    Returns True if a reset happened. Caller must save when True.
    """
    if not sub.is_active and in_free_trial(getattr(sub, 'tenant', None)):
        return False        # عدّاد التجربة مايتصفّرش أول الشهر (5 للتجربة كلها)
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
    if kind == 'scan' and not sub.is_active and in_free_trial(getattr(sub, 'tenant', None)):
        return min(TRIAL_DIAGNOSTICS_SCANS, sub.plan.monthly_diagnostics_scans_quota or TRIAL_DIAGNOSTICS_SCANS)
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
    # 🐛 [FIX]: tenant.subscription ممكن تكون نسخة قديمة متخزّنة على نفس الطلب (قبل consume_quota) —
    #    كانت بتتحفظ تاني وتصفّر عدّاد الشهر. بنقرا العدادات من الداتابيز الأول.
    if sub.pk:
        sub.refresh_from_db(fields=['diag_period_start', 'diag_scans_used_this_period',
                                    'diag_bot_used_this_period', 'diag_topup_balance'])

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
        f"({limit}/شهر). اشحن باقة 30 استخدام بـ 150 {_sym()} للاستمرار."
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


# ─────────────────────────────────────────────────────────────────────
# 🚗 الحصة بالعربية مش بالفتحة (نوفمبر ٢٠٢٦)
# ─────────────────────────────────────────────────────────────────────
# الباقات بقت «كشف أعطال: 20/60/200/600 عربية في الشهر» — نفس العربية اللي اتكشف عليها أكتر من مرة
# في نفس الشهر بتتحسب مرة واحدة. مفتاح العربية: رقم الشاسيه (VIN) أو اللوحة لو معروفين، وإلا
# «جلسة» الفني في اليوم (فتح الغرفة كذا مرة لنفس العربية في نفس اليوم مايتحسبش أكتر من مرة).

def normalize_vehicle_key(vin: str = '', plate: str = '', session_key: str = '') -> str:
    import re
    vin = re.sub(r'[^A-Za-z0-9]', '', vin or '').upper()
    if len(vin) >= 11:
        return f'vin:{vin}'
    plate = re.sub(r'\s+', '', plate or '')
    if plate:
        return f'plate:{plate}'
    if session_key:
        return f'session:{session_key}:{_today().isoformat()}'
    return ''


def _vehicle_counted(tenant, key: str) -> bool:
    from clients.models import AILimitTracker
    start = _period_start_for(_today())
    return AILimitTracker.objects.filter(tenant=tenant, action_type='diag_vehicle',
                                         used_at__date__gte=start, metadata__key=key).exists()


def consume_vehicle(tenant, key: str) -> QuotaResult:
    """بيحسب العربية مرة في الشهر: لو اتحسبت قبل كده ⇒ مسموح من غير خصم، وإلا خصم وحدة من الحصة
    (أو رصيد الشحن) وتسجيل المفتاح. من غير مفتاح ⇒ زي consume_quota العادي."""
    if tenant is not None and key and _vehicle_counted(tenant, key):
        res = check_quota(tenant, kind='scan')
        return QuotaResult(True, 'العربية دي اتحسبت الشهر ده', res.plan_remaining, res.topup_remaining,
                           res.plan_limit, res.used_this_period)
    res = consume_quota(tenant, kind='scan')
    if res.allowed and key and tenant is not None:
        from clients.models import AILimitTracker
        AILimitTracker.objects.create(tenant=tenant, action_type='diag_vehicle', metadata={'key': key})
    return res


@transaction.atomic
def link_vin_to_session(tenant, session_key: str, vin: str) -> QuotaResult:
    """🚗 الغرفة اتفتحت من غير شاسيه (اتحسبت «جلسة اليوم») وبعدين جهاز OBD قرا رقم الشاسيه:

    * العربية دي اتحسبت قبل كده الشهر ده ⇒ الوحدة اللي اتخصمت على الجلسة ترجع (مكررة).
    * أول عربية في الجلسة ⇒ الوحدة اللي اتخصمت تبقى بتاعتها (ولو رجعت لها تاني الشهر ده ببلاش).
    * الجلسة اتربطت بعربية تانية قبل كده (الفني نقل الجهاز لعربية جديدة) ⇒ عربية جديدة بتتحسب.
    """
    from clients.models import AILimitTracker, TenantSubscription
    vkey = normalize_vehicle_key(vin=vin)
    if tenant is None or not vkey.startswith('vin:'):
        return QuotaResult(False, 'رقم شاسيه مش صالح', 0, 0, 0, 0)
    skey = normalize_vehicle_key(session_key=session_key)
    start = _period_start_for(_today())
    session_row = (AILimitTracker.objects.select_for_update()
                   .filter(tenant=tenant, action_type='diag_vehicle', used_at__date__gte=start,
                           metadata__key=skey).first() if skey else None)
    unlinked = session_row is not None and not (session_row.metadata or {}).get('vin')

    if _vehicle_counted(tenant, vkey):
        if unlinked:
            sub = _get_subscription(tenant)
            if sub is not None:
                sub = TenantSubscription.objects.select_for_update().get(pk=sub.pk)
                _ensure_period_current(sub)
                if (sub.diag_scans_used_this_period or 0) > 0:
                    sub.diag_scans_used_this_period -= 1
                    sub.save(update_fields=['diag_scans_used_this_period'])
                else:
                    sub.diag_topup_balance = (sub.diag_topup_balance or 0) + 1
                    sub.save(update_fields=['diag_topup_balance'])
            session_row.metadata = {**(session_row.metadata or {}), 'vin': vkey[4:], 'refunded': True}
            session_row.save(update_fields=['metadata'])
            res = check_quota(tenant, kind='scan')
            return QuotaResult(True, 'العربية دي اتحسبت الشهر ده — رجّعنا الوحدة', res.plan_remaining,
                               res.topup_remaining, res.plan_limit, res.used_this_period)
        res = check_quota(tenant, kind='scan')
        return QuotaResult(True, 'العربية دي اتحسبت الشهر ده', res.plan_remaining, res.topup_remaining,
                           res.plan_limit, res.used_this_period)

    if unlinked:
        AILimitTracker.objects.create(tenant=tenant, action_type='diag_vehicle', metadata={'key': vkey})
        session_row.metadata = {**(session_row.metadata or {}), 'vin': vkey[4:]}
        session_row.save(update_fields=['metadata'])
        res = check_quota(tenant, kind='scan')
        return QuotaResult(True, 'العربية اتسجّلت على الوحدة اللي اتخصمت', res.plan_remaining,
                           res.topup_remaining, res.plan_limit, res.used_this_period)

    return consume_vehicle(tenant, vkey)
