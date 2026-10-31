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
            'name': (p.name_en or p.name) if is_en else p.name, 'slug': p.slug,
            'price': aed if (is_ae and aed > 0) else p.monthly_price,
            'max_users': p.max_users, 'max_branches': p.max_branches, 'max_treasuries': p.max_treasuries,
            'ai_quota': p.monthly_ai_designs_quota,
            'is_popular': len(plans) >= 3 and idx == 1,
        })
    return rows
