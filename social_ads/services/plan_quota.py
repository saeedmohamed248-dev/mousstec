"""
💎 حد البوستات الشهري لاستوديو التسويق حسب الباقة (نوفمبر ٢٠٢٦)
================================================================
    لايت 4 · سيلفر/ستارتر 20 · جولد/برو 60 · إمباير/إنتربرايز من غير حد   (social_studio.monthly_limit)

* اللي دافع الإضافة لوحدها مالوش حد.
* البوست المحسوب = بوست اتكتب بالذكاء الاصطناعي في الشهر ده (طيار آلي/توليد) — المستورد من الصفحة
  والمكتوب باليد مش محسوبين.
"""
from __future__ import annotations

from django.utils import timezone


def _month_start():
    now = timezone.localtime()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def monthly_limit(config):
    if config.paid_addon_is_live:
        return None
    from tenancy.services.entitlements import EntitlementService
    return EntitlementService.limit(config.tenant, "social_studio")


def posts_used(config) -> int:
    from social_ads.models import SocialPost
    return SocialPost.objects.filter(tenant_id=config.tenant_id, source=SocialPost.Source.AUTOPILOT,
                                     created_at__gte=_month_start()).count()


def posts_left(config):
    """None = غير محدود، وإلا العدد الباقي (ممكن صفر)."""
    limit = monthly_limit(config)
    if limit is None:
        return None
    return max(limit - posts_used(config), 0)


def clamp(config, wanted: int) -> int:
    """العدد اللي ينفع يتولّد من ``wanted`` جوه حد الشهر."""
    left = posts_left(config)
    return wanted if left is None else max(min(wanted, left), 0)


def status(config) -> dict:
    limit = monthly_limit(config)
    used = posts_used(config) if config.tenant_id else 0
    return {"limit": limit, "used": used,
            "left": None if limit is None else max(limit - used, 0),
            "exhausted": limit is not None and used >= limit}
