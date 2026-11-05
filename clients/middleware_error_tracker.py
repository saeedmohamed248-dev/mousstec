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

import json
import logging
import traceback

from django.core.exceptions import BadRequest, PermissionDenied, SuspiciousOperation
from django.db import connection
from django.http import Http404
from django_tenants.utils import schema_context

logger = logging.getLogger(__name__)

_IGNORE_PREFIXES = ('/static/', '/media/', '/favicon.ico', '/ws/')


# 🐛 [FIX]: X-Forwarded-For مش IP كان بيوقّع الـ INSERT (عمود inet) والخطأ نفسه بيضيع من السجل.
from erp_core.client_ip import client_ip as _client_ip  # noqa: E402


def _response_detail(request, response):
    """🔎 «اترفض ليه؟» لأخطاء 400/403 — من غير ما نسجّل بيانات العميل نفسها.

    1) `request._error_detail` — أي view/معالج يقدر يحط فيه السبب (زي سبب فشل CSRF).
    2) ردّ JSON فيه error/errors/message (زي inventory:job_card_save — «المخزون غير كافٍ…»،
       أو form.errors) ⇒ بنسجّل رسالة الرفض نفسها.
    🐛 [FIX]: قبل كده الـ 400 كان بيتسجّل «طلب غير صالح» وبس — مستحيل تعرف الفورم ناقصه إيه.
    """
    detail = getattr(request, '_error_detail', '') or ''
    if detail or response.status_code not in (400, 403, 409, 422):
        return str(detail)[:2000]
    if 'json' not in (response.get('Content-Type') or '') or getattr(response, 'streaming', False):
        return ''
    try:
        body = json.loads(response.content[:20000] or b'{}')
    except (ValueError, TypeError):
        return ''
    if not isinstance(body, dict):
        return ''
    parts = []
    for key in ('error', 'errors', 'message', 'detail'):
        val = body.get(key)
        if val:
            parts.append(f"{key}: {json.dumps(val, ensure_ascii=False, default=str) if not isinstance(val, str) else val}")
    return ' | '.join(parts)[:2000]


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
            if response.status_code >= 500 or response.status_code in (400, 403, 404):
                if not any(request.path.startswith(p) for p in _IGNORE_PREFIXES):
                    self._record(request, response.status_code, exc=None,
                                 # الشاشات اللي بتمسك الاستثناء وترجّع 500 (inventory.views.utils
                                 # ._json_response_safe) بتلزق الرسالة الأصلية والـ traceback على الرد
                                 tb=getattr(response, '_error_tb', ''),
                                 detail=(getattr(response, '_error_detail', '')
                                         or _response_detail(request, response)),
                                 exc_class=getattr(response, '_error_class', ''))
        except Exception:
            logger.exception("ErrorTrackingMiddleware response hook failed")
        return response

    def process_exception(self, request, exception):
        try:
            request._error_tracked = True
            # 🐛 [FIX]: Http404/PermissionDenied/SuspiciousOperation كانوا بيتسجّلوا «500 — عطل في الكود»
            #    بالـ traceback، مع إن جانجو بيرجّعهم 404/403/400. بنسجّل الحالة الحقيقية من غير traceback.
            from clients.services.error_insight import EXPECTED_EXCEPTIONS
            status = EXPECTED_EXCEPTIONS.get(exception.__class__.__name__)
            if status is None and isinstance(exception, (Http404, PermissionDenied, SuspiciousOperation, BadRequest)):
                status = 404 if isinstance(exception, Http404) else 403 if isinstance(exception, PermissionDenied) else 400
            if status:
                if any(request.path.startswith(p) for p in _IGNORE_PREFIXES):
                    return None
                self._record(request, status, exc=exception, tb='')
                return None
            self._record(request, 500, exc=exception, tb=traceback.format_exc())
        except Exception:
            logger.exception("ErrorTrackingMiddleware exception hook failed")
        return None  # let Django continue normal handling

    def _record(self, request, status, exc, tb, detail='', exc_class=''):
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
        from clients.services.error_insight import AUTO_RESOLVED_KINDS, classify, fingerprint
        exc_class = exc.__class__.__name__ if exc else (exc_class or '')
        fp = fingerprint(status, request.path, exc_class, getattr(request, 'resolver_match', None))
        kind = classify(status, request.path, user_id, exc_class,
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
                exception_class=exc_class[:200],
                message=(str(exc) if exc else detail or '')[:2000],
                fingerprint=fp,
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
