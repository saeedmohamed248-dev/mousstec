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
    data, bt_options, import_req, can_send = None, None, None, False
    request = context.get('request')
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        profile = getattr(user, 'employee_profile', None)
        is_owner = user.is_superuser or bool(getattr(profile, 'is_manager_or_above', False))
        if is_owner:
            try:
                from django.db import connection
                from clients.services.onboarding_steps import onboarding_steps
                tenant = getattr(connection, 'tenant', None)
                data = onboarding_steps(tenant)
                can_send = bool(user.is_superuser)        # الإرسال والاختيار للمالك
                # 💎 نوع النشاط اتشال من التسجيل ⇒ يختاره هنا (بيرتّب أقسام الرئيسية)
                if can_send and tenant is not None and not getattr(tenant, 'business_type_confirmed', True):
                    from clients.forms import TenantSignupForm
                    labels = dict(TenantSignupForm.base_fields['business_type'].choices)
                    bt_options = [(bt, labels.get(bt, bt)) for bt in
                                  TenantSignupForm.INDUSTRY_BUSINESS_TYPES.get(tenant.industry, ())]
                # 📦 آخر طلب «ارفعولي بضاعتي» (لو موجود) عشان نقوله حالته
                if data and can_send and tenant is not None:
                    from django_tenants.utils import get_public_schema_name, schema_context
                    from clients.models import ImportAssistRequest
                    with schema_context(get_public_schema_name()):
                        import_req = (ImportAssistRequest.objects.filter(tenant_id=tenant.pk)
                                      .order_by('-created_at').first())
            except Exception:   # noqa: BLE001 — الكارت مايوقّعش الصفحة
                logger.exception('[ONBOARDING] checklist failed')
    return {'ob': data, 'style': style, 'bt_options': bt_options, 'import_req': import_req,
            'can_send': can_send, 'request': request, 'csrf_token': context.get('csrf_token')}


@register.inclusion_tag('clients/_trial_offer_banner.html', takes_context=True)
def trial_offer_banner(context):
    """⏳ «اشترك قبل ما التجربة تخلص» — آخر يومين في التجربة، للمالك بس (clients/services/trial_offer.py)."""
    offer = None
    request = context.get('request')
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and user.is_superuser:
        try:
            from django.db import connection
            from clients.services.trial_offer import offer_for
            tenant = getattr(connection, 'tenant', None)
            if tenant is not None and getattr(tenant, 'schema_name', 'public') != 'public':
                offer = offer_for(tenant)
        except Exception:   # noqa: BLE001
            logger.exception('[TRIAL OFFER] banner failed')
    return {'offer': offer}
