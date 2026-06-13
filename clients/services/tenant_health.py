"""
🩺 Tenant Health Score
======================
حساب درجة صحة كل شركة من 0 إلى 100 بناءً على 5 إشارات:

  1. Subscription health (35%) — الحالة + الأيام المتبقية
  2. Recent activity      (25%) — VisitorLog آخر 7/30 يوم
  3. Account age          (15%) — كل ما الشركة أقدم، كل ما ثبتت
  4. Support burden       (15%) — تذاكر دعم مفتوحة
  5. Marketplace trust    (10%) — fraud flag + ai_trust_score

API:
    health = compute_tenant_health(tenant)            # single
    healths = bulk_tenant_health(tenants)             # dict {tenant_id: health}

كل health = {
    'score': int 0-100,
    'grade': 'A'|'B'|'C'|'D'|'F',
    'risk':  'low'|'medium'|'high'|'critical',
    'signals': {
        'subscription': int, 'activity': int, 'tenure': int,
        'support': int, 'trust': int,
    },
    'reasons': [str, ...],   # أسباب نزول الدرجة (للعرض)
}
"""
from __future__ import annotations

from datetime import timedelta
from django.utils import timezone

WEIGHTS = {
    'subscription': 0.35,
    'activity':     0.25,
    'tenure':       0.15,
    'support':      0.15,
    'trust':        0.10,
}


def _grade(score: int) -> str:
    if score >= 80: return 'A'
    if score >= 60: return 'B'
    if score >= 40: return 'C'
    if score >= 20: return 'D'
    return 'F'


def _risk(score: int) -> str:
    if score >= 70: return 'low'
    if score >= 45: return 'medium'
    if score >= 25: return 'high'
    return 'critical'


def _subscription_score(tenant, today):
    """0-100 بناء على status + الأيام المتبقية."""
    if getattr(tenant, 'is_fraud_flagged', False):
        return 0, 'حساب مُعَلَّم كاحتيال'
    status = getattr(tenant, 'status', '')
    if status == 'suspended':
        return 10, 'الحساب موقوف (suspended)'
    if status == 'active':
        end = getattr(tenant, 'subscription_end_date', None)
        if not end:
            return 80, 'مشترك بدون تاريخ انتهاء محدد'
        days = (end - today).days
        if days > 30:  return 100, None
        if days > 7:   return 80,  f'يتبقى {days} يوم على الاشتراك'
        if days >= 0:  return 50,  f'⚠️ الاشتراك ينتهي خلال {days} يوم'
        return 30, f'⛔ الاشتراك منتهي منذ {-days} يوم'
    if status == 'trial':
        trial_end = getattr(tenant, 'trial_ends_at', None)
        if trial_end and trial_end >= today:
            return 60, f'تجريبي — متبقي {(trial_end - today).days} يوم'
        return 20, '⛔ الفترة التجريبية انتهت'
    return 30, f'حالة غير معروفة: {status}'


def _activity_score(visits_7d, visits_30d):
    """0-100 بناء على عدد الزيارات."""
    if visits_7d > 100: return 100, None
    if visits_7d > 20:  return 80,  None
    if visits_7d > 0:   return 60,  None
    if visits_30d > 0:  return 35,  f'نشاط ضعيف ({visits_30d} زيارة آخر شهر)'
    return 5, '🔕 لا يوجد نشاط منذ 30 يوم'


def _tenure_score(created_on, today):
    """شركات قديمة = أقل احتمال للـ churn."""
    if not created_on:
        return 50, None
    days = (today - created_on).days
    if days > 180: return 100, None
    if days > 90:  return 85,  None
    if days > 30:  return 70,  None
    if days > 7:   return 50,  'حساب حديث (< شهر)'
    return 35, 'حساب جديد جداً (< أسبوع) — في فترة onboarding'


def _support_score(open_tickets):
    if open_tickets == 0:     return 100, None
    if open_tickets <= 2:     return 70,  f'{open_tickets} تذاكر دعم مفتوحة'
    if open_tickets <= 5:     return 40,  f'⚠️ {open_tickets} تذاكر دعم مفتوحة'
    return 10, f'🚨 {open_tickets} تذكرة دعم مفتوحة'


def _trust_score(tenant):
    if getattr(tenant, 'is_fraud_flagged', False):
        return 0, 'fraud flag مرفوع'
    base = int(getattr(tenant, 'ai_trust_score', 100) or 100)
    dispute_rate = float(getattr(tenant, 'dispute_rate', 0) or 0)
    reasons = []
    if dispute_rate > 10:
        base = min(base, 40)
        reasons.append(f'نسبة نزاعات مرتفعة ({dispute_rate:.1f}%)')
    elif dispute_rate > 3:
        base = min(base, 70)
    return max(0, min(100, base)), '؛ '.join(reasons) if reasons else None


def _combine(signals: dict) -> int:
    total = sum(signals[k] * WEIGHTS[k] for k in WEIGHTS)
    return int(round(total))


def compute_tenant_health(tenant, *, visits_7d=0, visits_30d=0, open_tickets=0, today=None):
    """احسب health لشركة واحدة. الأرقام الثلاثة الأخيرة تُحقن من bulk fetch لتفادي N+1."""
    today = today or timezone.localdate()
    signals = {}
    reasons = []
    for name, fn, args in (
        ('subscription', _subscription_score, (tenant, today)),
        ('activity',     _activity_score,     (visits_7d, visits_30d)),
        ('tenure',       _tenure_score,       (getattr(tenant, 'created_on', None), today)),
        ('support',      _support_score,      (open_tickets,)),
        ('trust',        _trust_score,        (tenant,)),
    ):
        score, reason = fn(*args)
        signals[name] = score
        if reason:
            reasons.append(reason)

    score = _combine(signals)
    return {
        'score':   score,
        'grade':   _grade(score),
        'risk':    _risk(score),
        'signals': signals,
        'reasons': reasons,
    }


def bulk_tenant_health(tenants):
    """
    احسب health لقائمة شركات في query واحد لكل إشارة (يتجنب N+1).
    Returns: {tenant.id: health_dict}
    """
    from clients.models import VisitorLog, SupportTicket
    from django.db.models import Count

    today = timezone.localdate()
    now = timezone.now()
    schemas = [t.schema_name for t in tenants if getattr(t, 'schema_name', None)]
    tenant_ids = [t.id for t in tenants]

    # 1) عدد الزيارات في آخر 7 و 30 يوم لكل tenant_schema (query واحد كل واحد)
    visits_7d_map = dict(
        VisitorLog.objects
        .filter(tenant_schema__in=schemas, timestamp__gte=now - timedelta(days=7))
        .values_list('tenant_schema')
        .annotate(n=Count('id'))
        .values_list('tenant_schema', 'n')
    )
    visits_30d_map = dict(
        VisitorLog.objects
        .filter(tenant_schema__in=schemas, timestamp__gte=now - timedelta(days=30))
        .values_list('tenant_schema')
        .annotate(n=Count('id'))
        .values_list('tenant_schema', 'n')
    )

    # 2) تذاكر الدعم المفتوحة لكل tenant
    open_tickets_map = dict(
        SupportTicket.objects
        .filter(tenant_id__in=tenant_ids, is_deleted=False)
        .exclude(status='closed')
        .values_list('tenant_id')
        .annotate(n=Count('id'))
        .values_list('tenant_id', 'n')
    )

    out = {}
    for t in tenants:
        out[t.id] = compute_tenant_health(
            t,
            visits_7d=visits_7d_map.get(t.schema_name, 0),
            visits_30d=visits_30d_map.get(t.schema_name, 0),
            open_tickets=open_tickets_map.get(t.id, 0),
            today=today,
        )
    return out
