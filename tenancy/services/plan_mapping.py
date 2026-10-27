"""
🗺️ Legacy plan slug mapping — shared constant
==============================================
الـ Client.plan CharField عنده choices قديمة (silver/gold/empire/print_basic/
print_pro/print_enterprise). الـ Plan model عنده slugs مختلفة (auto-silver/
auto-gold/...). الـ mapping ده بيوصلهم.

⚠️ نفس القيم اللي في migration 0028 (Phase 0a backfill). لو غيرت هنا، لازم
تشغل migration جديدة تـ resync.

استخدامات:
  - Paymob webhook (subscription_views.py): يـ resolve الـ legacy plan string
    إلى Plan FK.
  - أي callsite تاني عاوز يـ translate.
"""
from __future__ import annotations

from typing import Optional


LEGACY_TO_PLAN_SLUG = {
    'silver':           'auto-silver',
    'gold':             'auto-gold',
    'empire':           'auto-empire',
    'print_basic':      'print-starter',
    'print_pro':        'print-pro',
    'print_enterprise': 'print-enterprise',
    # 🔧 Smart Diagnostics Premium tier — identity mapping (no legacy alias)
    'premium_diagnostics': 'premium_diagnostics',
}


def resolve_plan_slug(legacy_value: str) -> Optional[str]:
    """ترجع الـ Plan.slug المرتبط بالـ legacy Client.plan value، أو None."""
    if not legacy_value:
        return None
    return LEGACY_TO_PLAN_SLUG.get(legacy_value.strip())


def legacy_plan_value(plan_slug: str) -> str:
    """العكس: Plan.slug ⇒ قيمة Client.plan القديمة ('auto-gold' ⇒ 'gold').

    🐛 [FIX]: تفعيل الاشتراك من لوحة المالك كان بيكتب الـ slug نفسه في
    Client.plan ('auto-gold' — مش من اختياراته) وPaymob بيكتب 'gold'؛ فصفحة
    الأسعار كانت بتعرض «الباقة الحالية» غلط حسب طريقة الدفع."""
    for legacy, slug in LEGACY_TO_PLAN_SLUG.items():
        if slug == plan_slug:
            return legacy
    return plan_slug
