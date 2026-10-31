"""
💎 باقات صفحات الهبوط من جدول Plan

🐛 [FIX]: صفحة هبوط المطابع كانت بتعرض أسعار مكتوبة في القالب (550 / 880 / 2,000) و«مستخدمين
غير محدود» — والباقات الفعلية 875 / 1,250 / 2,000 بحدود معروفة. العميل يشوف سعر ويتطلب منه
سعر تاني. دلوقتي الأسعار والحدود من نفس جدول صفحة الأسعار (أي تعديل من لوحة المالك يظهر فوراً).

    {% load landing_tags %}
    {% landing_plans 'printing' as plans %}
"""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def landing_plans(context, industry):
    from django.utils.translation import get_language
    from django_tenants.utils import get_public_schema_name, schema_context

    from clients.models import Plan
    from clients.services.plan_compare import plan_display_name
    from erp_core.regions import region_from_request

    request = context.get('request')
    is_ae = bool(request) and region_from_request(request)['country'] == 'AE'
    with schema_context(get_public_schema_name()):
        plans = list(Plan.objects.filter(industry=industry, is_active=True).order_by('sort_order', 'monthly_price'))
    is_en = (get_language() or '').startswith('en')
    rows = []
    for idx, p in enumerate(plans):
        aed = p.monthly_price_aed or 0
        rows.append({
            # 🐛 [FIX]: name_en فاضي ⇒ «Starter — ستارتر» كان بيظهر في الموقع الإنجليزي — نفس منطق صفحة الأسعار
            'name': plan_display_name(p, is_en), 'slug': p.slug,
            'price': aed if (is_ae and aed > 0) else p.monthly_price,
            'max_users': p.max_users, 'max_branches': p.max_branches, 'max_treasuries': p.max_treasuries,
            'ai_quota': p.monthly_ai_designs_quota,
            'is_popular': len(plans) >= 3 and idx == 1,
        })
    return rows


@register.inclusion_tag('clients/_sector_features.html', takes_context=True)
def sector_features(context, industry):
    """📚 كل مميزات القطاع + اللي مشترك + جدول مقارنة باقاته — من نفس كتالوج صفحة «المميزات».

    🐛 [FIX]: صفحات القطاعات كانت بتعرض 8–10 كروت مكتوبة بإيدها — مفيش موارد بشرية ولا مرتبات
    ولا صلاحيات ولا زرار الدعم ولا حدود الباقات، فالزائر مش عارف السيستم فيه إيه فعلاً."""
    from django.utils.translation import get_language
    from django_tenants.utils import get_public_schema_name, schema_context

    from clients.models import Plan
    from clients.services.feature_catalog import build_sections
    from clients.services.plan_compare import compare_tables

    is_en = (get_language() or '').startswith('en')
    sections, table = [], None
    try:
        with schema_context(get_public_schema_name()):
            plans = list(Plan.objects.filter(is_active=True).order_by('industry', 'sort_order'))
            sector = [p for p in plans if p.industry == industry]
            # باقات القطاع بس ⇒ «من باقة Gold» مش «Gold / Pro» في صفحة الورش
            sections = [s for s in build_sections(is_en=is_en, plans=sector)
                        if s['industry'] in (None, industry)]
            table = compare_tables(sector, is_en=is_en).get(industry)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('[LANDING] sector features failed industry=%s', industry)
    return {
        'industry': industry,
        'sector_section': next((s for s in sections if s['industry'] == industry), None),
        'shared_sections': [s for s in sections if s['industry'] is None],
        'table': table,
        'LANGUAGE_BIDI': context.get('LANGUAGE_BIDI', not is_en),
    }
