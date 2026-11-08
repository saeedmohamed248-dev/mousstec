"""
⏳ عرض «اشترك قبل ما التجربة تخلص» — قبل نهاية التجربة بيومين
================================================================
الزبون اللي جرّب واتعلّق محتاج دفعة أخيرة بمهلة حقيقية:
* كوبون شخصي للشركة (استخدام واحد، للعملاء الجداد، بيخلص آخر يوم بعد نهاية التجربة) بنسبة
  TRIAL_END_OFFER_PERCENT (افتراضي 20%، صفر = من غير خصم — التذكير بس).
* بانر جوه السيستم للمالك + إيميل (وواتساب لو متظبط) مرة واحدة قبل النهاية بيومين.
الكوبون بيتعمل مرة واحدة ويتعاد استخدامه لو البانر اتعرض كذا مرة.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

OFFER_DAYS = 2


def _percent():
    try:
        return max(0, min(int(getattr(settings, 'TRIAL_END_OFFER_PERCENT', 20)), 90))
    except (TypeError, ValueError):
        return 20


def offer_code(tenant):
    """كود ثابت لكل شركة (مش متخمّن): TRIAL-<id>-<4 حروف من hash>."""
    h = hashlib.sha256(f"{tenant.pk}:{tenant.schema_name}:{settings.SECRET_KEY}".encode()).hexdigest()[:4]
    return f"TRIAL-{tenant.pk}-{h}".upper()


def days_left(tenant):
    if getattr(tenant, 'status', '') != 'trial' or not getattr(tenant, 'trial_ends_at', None):
        return None
    return (tenant.trial_ends_at - timezone.localdate()).days


def ensure_coupon(tenant):
    """الكوبون الشخصي (بيتعمل أول مرة بس) — None لو الخصم صفر."""
    pct = _percent()
    if not pct:
        return None
    from django_tenants.utils import get_public_schema_name, schema_context
    from clients.models import Coupon
    tz = timezone.get_current_timezone()
    ends = timezone.make_aware(datetime.combine(tenant.trial_ends_at + timedelta(days=1), time.max), tz)
    with schema_context(get_public_schema_name()):
        coupon, _created = Coupon.objects.get_or_create(code=offer_code(tenant), defaults=dict(
            label='عرض نهاية التجربة', percent_off=pct, industry=tenant.industry, ends_at=ends,
            max_redemptions=1, new_customers_only=True))
    return coupon


def offer_for(tenant):
    """dict العرض لو التجربة فاضل فيها يومين أو أقل، وإلا None."""
    left = days_left(tenant)
    if left is None or left < 0 or left > OFFER_DAYS:
        return None
    coupon = ensure_coupon(tenant)
    url = f"/pricing/?shop={tenant.schema_name}" + (f"&coupon={coupon.code}" if coupon else '')
    return {'days_left': left, 'percent': coupon.percent_off if coupon else 0,
            'code': coupon.code if coupon else '', 'ends_at': coupon.ends_at if coupon else None, 'url': url}
