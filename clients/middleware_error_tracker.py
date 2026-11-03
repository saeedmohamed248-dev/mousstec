"""
🚨 ErrorTrackingMiddleware — يصطاد أي 4xx/5xx واستثناءات عبر كل المستأجرين
ويسجلها في public schema (SystemErrorLog) لتظهر في لوحة الـ Super Admin.

ملاحظات معمارية:
- بنكتب دائماً في public schema (schema_context) حتى لو الـ request جاي من tenant،
  عشان الـ super admin يلاقي كل حاجة في مكان واحد.
- بنتفادى عمل log لمسارات static/media لتقليل الضوضاء.
- في حالة فشل التسجيل ذاته، بنـ log في الـ python logger ومش بنرمي exception
  عشان مانكسرش الـ request flow.
"""

import logging
import traceback

from django.db import connection
from django_tenants.utils import schema_context

logger = logging.getLogger(__name__)

_IGNORE_PREFIXES = ('/static/', '/media/', '/favicon.ico', '/ws/')


# 🐛 [FIX]: X-Forwarded-For مش IP كان بيوقّع الـ INSERT (عمود inet) والخطأ نفسه بيضيع من السجل.
from erp_core.client_ip import client_ip as _client_ip  # noqa: E402


class ErrorTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            # 🐛 [FIX]: الاستثناء بيتسجّل في process_exception (بالـ traceback) وبعدها جانجو بيرجّع 500
            #    فكان بيتسجّل تاني من غير traceback — كل عطل كان بيظهر مرتين.
            if getattr(request, '_error_tracked', False):
                return response
            if response.status_code >= 500 or response.status_code == 400 or response.status_code == 403 or response.status_code == 404:
                if not any(request.path.startswith(p) for p in _IGNORE_PREFIXES):
                    self._record(request, response.status_code, exc=None, tb='')
        except Exception:
            logger.exception("ErrorTrackingMiddleware response hook failed")
        return response

    def process_exception(self, request, exception):
        try:
            request._error_tracked = True
            self._record(request, 500, exc=exception, tb=traceback.format_exc())
        except Exception:
            logger.exception("ErrorTrackingMiddleware exception hook failed")
        return None  # let Django continue normal handling

    def _record(self, request, status, exc, tb):
        from clients.models import SystemErrorLog

        # حدد الـ level
        if status >= 500:
            level = 'critical'
        elif status in (400, 403):
            level = 'error'
        else:
            level = 'warning'

        schema = getattr(connection, 'schema_name', 'public') or 'public'
        tenant = getattr(request, 'tenant', None)
        tenant_name = getattr(tenant, 'name', '') if tenant else ''

        user = getattr(request, 'user', None)
        user_id = user.id if (user is not None and getattr(user, 'is_authenticated', False)) else None
        username = getattr(user, 'username', '') if user_id else ''

        # حجم محدود للـ payload
        get_data = {k: v[:200] for k, v in request.GET.items()} if request.method == 'GET' else {}
        req_data = {
            'GET': get_data,
            'UA': request.META.get('HTTP_USER_AGENT', '')[:200],
            'REF': request.META.get('HTTP_REFERER', '')[:200],
        }

        # 🤖 ضوضاء البوتات (/.env، معاينة لينك واتساب، بوت فحص…) بتتسجّل مقفولة لوحدها —
        #    مركز الأخطاء يفضل فيه اللي محتاج حد يتصرف بس، والبيانات موجودة لو حد عايز يراجع.
        from clients.services.error_insight import AUTO_RESOLVED_KINDS, classify
        kind = classify(status, request.path, user_id, exc.__class__.__name__ if exc else '',
                        req_data['UA'], req_data['REF'], request.method,
                        hosts=(request.get_host().split(':')[0],))
        auto_closed = kind in AUTO_RESOLVED_KINDS
        from django.utils import timezone

        with schema_context('public'):
            err = SystemErrorLog.objects.create(
                tenant_schema=schema[:63],
                tenant_name=tenant_name[:100],
                user_id=user_id,
                username=username[:150],
                path=request.path[:500],
                method=request.method[:10],
                status_code=status,
                exception_class=(exc.__class__.__name__ if exc else '')[:200],
                message=(str(exc)[:2000] if exc else ''),
                traceback=(tb or '')[:8000],
                request_data=req_data,
                ip_address=_client_ip(request),
                level=level,
                is_resolved=auto_closed,
                resolved_at=timezone.now() if auto_closed else None,
            )
        if not auto_closed:
            # 🩺 المستوى ٢: عطل جديد/بيتكرر كتير/عند كذا شركة ⇒ إيميل للمالك (+ بوت التصليح للجديد)
            try:
                from clients.services.error_autoheal import on_error_recorded
                on_error_recorded(err, kind)
            except Exception:
                logger.exception("ErrorTrackingMiddleware: autoheal hook failed")
