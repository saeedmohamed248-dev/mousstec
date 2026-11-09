"""
💎 حد ردود الرد الآلي الشهري حسب الباقة (نوفمبر ٢٠٢٦)
====================================================
الرد الآلي بقى جوه الباقات بدل ما يتباع لوحده:

    لايت 100 · سيلفر/ستارتر 1,500 · جولد/برو 4,000 · إمباير/إنتربرايز 10,000   (ai_auto_reply.monthly_limit)

* اللي دافع الإضافة لوحدها (paid_addon_is_live) مالوش حد — اشتراكه زي ما هو لحد ما يخلص.
* الرد المحسوب = رد البوت اللي اتبعت فعلاً (REPLIED ومش بشري) في الشهر الحالي بتوقيت القاهرة.
* لما الحد يخلص: البوت مابيردش بالذكاء الاصطناعي — الزبون بياخد رسالة «هيتواصل معاك حد»
  وصاحب المحل بيتبلّغ مرة في اليوم إنه يرقّي أو يرد بنفسه. مفيش زبون بيتساب من غير رد.
"""
from __future__ import annotations

from django.utils import timezone

WARN_AT = 0.8            # تنبيه لما يوصل ٨٠٪ من الحد


def _month_start():
    now = timezone.localtime()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def monthly_limit(config):
    """None = غير محدود (إضافة مدفوعة أو الباقة من غير حد)."""
    if config.paid_addon_is_live:
        return None
    from tenancy.services.entitlements import EntitlementService
    return EntitlementService.limit(config.tenant, "ai_auto_reply")


def replies_used(config) -> int:
    from omnichannel.models import ChannelMessageLog
    return ChannelMessageLog.objects.filter(
        tenant_id=config.tenant_id, status=ChannelMessageLog.Status.REPLIED, is_human=False,
        created_at__gte=_month_start(),
    ).count()


def status(config) -> dict:
    """{'limit', 'used', 'left', 'percent', 'near', 'exhausted'} للواجهة والمهمة."""
    limit = monthly_limit(config)
    used = replies_used(config) if config.tenant_id else 0
    if limit is None:
        return {"limit": None, "used": used, "left": None, "percent": 0, "near": False, "exhausted": False}
    left = max(limit - used, 0)
    percent = min(int(used * 100 / limit), 100) if limit else 100
    return {"limit": limit, "used": used, "left": left, "percent": percent,
            "near": used >= limit * WARN_AT, "exhausted": used >= limit}


def can_reply(config) -> bool:
    limit = monthly_limit(config)
    return limit is None or replies_used(config) < limit
