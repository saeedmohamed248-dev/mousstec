"""💎 بانر «قربت من حد باقتك» — {% load plan_usage %}{% plan_usage_banner %}

بيظهر بس للباقات اللي ليها حد شهري (Lite) لما الاستخدام يعدّي 80%، وأحمر لما يخلص.
أي خطأ هنا مايوقّعش الصفحة (بانر معلوماتي)."""
import logging

from django import template

register = template.Library()
logger = logging.getLogger('mouss_tec_core')


@register.inclusion_tag('clients/_plan_usage_banner.html', takes_context=True)
def plan_usage_banner(context, style='portal'):
    warning = None
    request = context.get('request')
    if request is not None and getattr(request, 'user', None) and request.user.is_authenticated:
        try:
            from tenancy.services.plan_limits import usage_warning
            warning = usage_warning()
        except Exception:   # noqa: BLE001 — البانر مايوقّعش الصفحة
            logger.exception('[PLAN USAGE] banner failed')
    return {'w': warning, 'style': style}
