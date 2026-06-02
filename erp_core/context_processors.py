"""
Template context processors for Mouss Tec ERP.
"""
from django.db import connection


def tenant_context(request):
    """Inject tenant-related context into all templates."""
    ctx = {
        'is_public_schema': connection.schema_name == 'public',
    }

    # 🎁 اعرض هدايا الـ AI النشطة للـ tenant (تظهر في admin index)
    # نشغّلها فقط للمستخدمين المسجّلين على tenant — مش على public
    try:
        if (
            connection.schema_name != 'public'
            and hasattr(request, 'tenant')
            and getattr(request, 'user', None)
            and request.user.is_authenticated
            # 🎨 الهدية بتاعت AI Studio خاصة بقطاع التصميم/المطابع فقط —
            # متبقاش في صفحات السيارات/قطع الغيار (automotive)
            and getattr(request.tenant, 'industry', 'automotive') == 'printing'
        ):
            from clients.models import AIBonusGrant
            grants = list(
                AIBonusGrant.objects.filter(tenant=request.tenant, is_active=True)
                .order_by('-granted_at')[:5]
            )
            active_grants = [g for g in grants if g.is_valid]
            if active_grants:
                total_d = sum(g.remaining_designs for g in active_grants)
                total_w = sum(g.remaining_whatsapp for g in active_grants)
                total_m = sum(g.remaining_watermarks for g in active_grants)
                ctx['active_ai_bonus_grants'] = active_grants
                ctx['active_ai_bonus_totals'] = {
                    'designs': total_d,
                    'whatsapp': total_w,
                    'watermarks': total_m,
                    'has_any': (total_d + total_w + total_m) > 0,
                }
    except Exception:
        # context processors يجب ألا يكسروا الـ template — تجاهل أي خطأ بصمت
        pass

    return ctx
