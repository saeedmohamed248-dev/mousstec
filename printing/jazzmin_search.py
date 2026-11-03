"""🔎 مربع البحث اللي فوق في الأدمن — حسب مجال الشركة.

Jazzmin بيقرا search_model مرة واحدة لكل المنصة. المطابع كانت بتبحث في
inventory.Product (قطع غيار العربيات) والقسم ده مقفول عليها ⇒ 404. هنا بنلف
الـ template tag بتاع Jazzmin (get_jazzmin_settings) ونسيب مربع البحث اللي
يخص مجال الشركة بس — نفس فكرة _industry_filtered_get_app_list في erp_core/urls.py.
"""
from django.db import connection

_PRINTING_PREFIX = '/printing/'


def _keep(search_model, industry):
    url = str(search_model.get('search_url') or '')
    is_printing = _PRINTING_PREFIX in url
    return is_printing if industry == 'printing' else not is_printing


def install():
    from jazzmin.templatetags import jazzmin as jz

    original = jz.get_jazzmin_settings
    if getattr(original, '_mt_industry_aware', False):
        return

    def get_jazzmin_settings(request):
        settings = original(request)
        parsed = settings.get('search_models_parsed') or []
        if parsed and connection.schema_name != 'public':
            tenant = getattr(request, 'tenant', None)
            industry = getattr(tenant, 'industry', 'automotive') or 'automotive'
            settings['search_models_parsed'] = [m for m in parsed if _keep(m, industry)]
        elif parsed:
            settings['search_models_parsed'] = [m for m in parsed if _keep(m, 'automotive')]
        return settings

    get_jazzmin_settings._mt_industry_aware = True
    jz.get_jazzmin_settings = get_jazzmin_settings
    jz.register.simple_tag(get_jazzmin_settings, name='get_jazzmin_settings')
