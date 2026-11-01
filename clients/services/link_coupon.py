"""
🎟️ كود الخصم في رابط الإعلان — mousstec.com/?coupon=CODE
==========================================================
قبل كده الكود كان لازم العميل يكتبه بإيده في خانة صغيرة جوه نافذة الدفع — اللي جاي من إعلان
«خصم 50% بالكود X» بيفتح صفحة الأسعار يلاقي الأسعار كاملة ومفيش أي أثر للعرض.

دلوقتي الرابط نفسه بيشيل الكود (``?coupon=`` أو ``?ref=``):
- شريط فوق الصفحة فيه الكود ونسبة الخصم وعدّاد لحد ما العرض يخلص.
- كروت الأسعار بتشطب السعر القديم وتعرض السعر بعد الكود (نفس ``Coupon.discount_on`` اللي بيتحصّل).
- الكود بيتحفظ في كوكي (لحد ما يخلص) عشان يفضل مع الزائر لو لفّ في الموقع، وبيتكتب لوحده
  في نافذة الدفع وفي التسجيل.

التحقق النهائي (مرة لكل شركة، أول اشتراك بس، عدد الاستخدامات…) بيفضل وقت الدفع في
``platform_billing.resolve_coupon`` — هنا بنعرض بس كود شغّال دلوقتي.
"""
from __future__ import annotations

COOKIE = 'mt_coupon'


def clean_code(raw) -> str:
    """نفس تنضيف كود التسجيل (حروف وأرقام و - _ بس، كابيتال، 40 حرف)."""
    return ''.join(ch for ch in (raw or '') if ch.isalnum() or ch in '-_').upper()[:40]


def code_from_request(request) -> tuple[str, bool]:
    """(الكود، جه من الرابط؟) — الرابط الأول، وإلا الكوكي."""
    if request is None:
        return '', False
    url_code = clean_code(request.GET.get('coupon') or request.GET.get('ref'))
    if url_code:
        return url_code, True
    return clean_code(request.COOKIES.get(COOKIE)), False


def live_coupon(code):
    """الكود مفعّل وشغّال دلوقتي ولسه مخلصش عدد استخداماته؟ ⇒ Coupon أو None."""
    if not code:
        return None
    from django.db.models import Q
    from django.utils import timezone
    from django_tenants.utils import get_public_schema_name, schema_context

    from clients.models import Coupon
    now = timezone.now()
    with schema_context(get_public_schema_name()):
        c = (Coupon.objects.filter(code=code, is_active=True, starts_at__lte=now)
             .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
             .select_related('plan').first())
        if c is not None and c.max_redemptions and c.invoices.filter(status='paid').count() >= c.max_redemptions:
            return None
    return c


def applies_to(coupon, plan) -> bool:
    return not ((coupon.plan_id and coupon.plan_id != plan.id)
                or (coupon.industry and coupon.industry != plan.industry))


def target_label(coupon, is_en=False) -> str:
    if coupon.plan_id:
        name = (coupon.plan.name_en or coupon.plan.name) if is_en else coupon.plan.name
        return name.split(' — ')[0].strip() if is_en else name
    if coupon.industry == 'automotive':
        return 'all car-service plans' if is_en else 'كل باقات السيارات'
    if coupon.industry == 'printing':
        return 'all print-shop plans' if is_en else 'كل باقات المطابع'
    return 'all plans' if is_en else 'كل الباقات'


def request_coupon(request):
    """الكود اللي الزائر جاي بيه (رابط/كوكي) لو شغّال — على الموقع المصري بس: الدفع أونلاين
    بالجنيه، والزائر الإماراتي بيشترك عن طريق الدعم."""
    code, from_url = code_from_request(request)
    if not code:
        return None, False
    from erp_core.regions import region_from_request
    if region_from_request(request)['country'] == 'AE':
        return None, from_url
    return live_coupon(code), from_url
