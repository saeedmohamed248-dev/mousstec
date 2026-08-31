"""
💱 قوالب عرض المبالغ — تحلّ محل "ج.م" الثابتة في الـ templates.

الاستخدام:
    {% load money_tags %}
    {{ invoice.total|money }}            → يقرأ عملة المستأجر الحالي
    {{ amount|money:"AED" }}             → عملة صريحة
    {{ amount|money_en }}                → رمز إنجليزي (للفواتير الدولية)
    {{ "AE"|currency_of_country }}       → AED

المصدر الوحيد للحقيقة: erp_core.localization
"""
from django import template
from django.db import connection

from erp_core.localization import (
    format_money,
    currency_symbol as _currency_symbol,
    currency_for_country,
    resolve_tenant_localization,
    DEFAULT_COUNTRY,
    country_config,
)

register = template.Library()


def _current_currency():
    """عملة المستأجر الحالي من الـ connection (django-tenants)، أو الافتراضي."""
    tenant = getattr(connection, 'tenant', None)
    if tenant is not None and getattr(tenant, 'schema_name', 'public') != 'public':
        return resolve_tenant_localization(tenant)['currency']
    return country_config(DEFAULT_COUNTRY)['currency']


@register.filter(name='money')
def money(amount, currency=None):
    """نسّق مبلغ بعملة المستأجر (أو عملة صريحة) بالرمز العربي."""
    cur = currency or _current_currency()
    return format_money(amount, currency=cur, lang='ar')


@register.filter(name='money_en')
def money_en(amount, currency=None):
    cur = currency or _current_currency()
    return format_money(amount, currency=cur, lang='en')


@register.filter(name='money_plain')
def money_plain(amount, currency=None):
    """الرقم منسّقاً بدون رمز العملة."""
    cur = currency or _current_currency()
    return format_money(amount, currency=cur, symbol=False)


@register.filter(name='currency_symbol')
def currency_symbol_filter(currency, lang='ar'):
    return _currency_symbol(currency, lang)


@register.filter(name='currency_of_country')
def currency_of_country(country):
    return currency_for_country(country)
