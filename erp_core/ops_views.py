"""
🔒 نقاط تشغيلية للبنية التحتية (Ops endpoints).

caddy_tls_check: يستخدمه Caddy (on-demand TLS) قبل إصدار شهادة HTTPS
لأي سَبدومين فرع — بنأكد إن الدومين ده فعلاً فرع مسجّل عندنا (أو الدومين
الأساسي) قبل ما نسمح بإصدار شهادة، عشان نمنع إساءة الاستخدام.
"""
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger('mouss_tec_core')


@csrf_exempt
def caddy_tls_check(request):
    domain = (request.GET.get('domain') or '').strip().lower().rstrip('.')
    if not domain:
        return HttpResponseNotFound('no domain')

    base = str(getattr(settings, 'BASE_DOMAIN', '') or '').lower()
    # الدومين الأساسي و www مسموحين دايماً
    if base and (domain == base or domain == f'www.{base}'):
        return HttpResponse('ok')

    # غير كده لازم يكون فرع مسجّل في جدول الدومينات (على السكيمة العامة)
    try:
        from django_tenants.utils import schema_context
        from clients.models import Domain
        with schema_context('public'):
            if Domain.objects.filter(domain=domain).exists():
                return HttpResponse('ok')
    except Exception as exc:  # أي خطأ = رفض آمن (مفيش إصدار شهادة)
        logger.warning("tls-check failed for %s: %s", domain, exc)

    return HttpResponseNotFound('unknown host')
