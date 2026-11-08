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


@register.inclusion_tag('clients/_onboarding_steps.html', takes_context=True)
def onboarding_checklist(context, style='portal'):
    """🧭 «أول خطوات» للشركة الجديدة (clients/services/onboarding_steps.py) — للمالك/المدير بس."""
    data = None
    request = context.get('request')
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        profile = getattr(user, 'employee_profile', None)
        is_owner = user.is_superuser or bool(getattr(profile, 'is_manager_or_above', False))
        if is_owner:
            try:
                from django.db import connection
                from clients.services.onboarding_steps import onboarding_steps
                data = onboarding_steps(getattr(connection, 'tenant', None))
            except Exception:   # noqa: BLE001 — الكارت مايوقّعش الصفحة
                logger.exception('[ONBOARDING] checklist failed')
    return {'ob': data, 'style': style}
