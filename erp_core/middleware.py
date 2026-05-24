"""
Mouss Tec — Middleware Layer
============================
IndustryPortalMiddleware: يوجه بوابات القطاعات (auto.*/print.*) قبل django-tenants.
IndustryRoutingMiddleware: يمنع الوصول العابر للقطاعات (automotive ↔ printing).
AuditIPMiddleware: يحفظ IP والمستخدم في thread-local لاستخدامها في Audit Trail signals.
CSRFCookieCleanupMiddleware: ينظف كوكيز CSRF القديمة بعد تغيير اسم الكوكي.
"""
import os
import re
import threading
from django.conf import settings
from django.db import connection
from django.http import HttpResponseNotFound
from django.shortcuts import redirect

_audit_thread_local = threading.local()

_BASE_DOMAIN = os.getenv('BASE_DOMAIN', 'mousstec.com')

_PORTAL_MAP = {
    f'auto.{_BASE_DOMAIN}': 'automotive',
    f'print.{_BASE_DOMAIN}': 'printing',
}


class IndustryPortalMiddleware:
    """
    يعترض بوابات القطاعات (auto.mousstec.com / print.mousstec.com) قبل django-tenants.
    هذه ليست مستأجرين حقيقيين — بل بوابات توجه لصفحات هبوط مخصصة لكل قطاع.
    يجب أن يكون قبل TenantMainMiddleware في MIDDLEWARE.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(':')[0].lower()
        industry = _PORTAL_MAP.get(host)
        if not industry:
            return self.get_response(request)

        path = request.path_info

        # السماح بمرور الملفات الثابتة بدون تدخل
        if path.startswith('/static/') or path.startswith('/media/'):
            return self.get_response(request)

        protocol = 'https' if request.is_secure() else 'http'
        base = f'{protocol}://{_BASE_DOMAIN}'

        # الصفحة الرئيسية → صفحة هبوط القطاع المخصصة
        if path == '/' or path == '':
            from django.shortcuts import render as _render
            template = 'clients/auto_landing.html' if industry == 'automotive' else 'clients/print_landing.html'
            return _render(request, template, {
                'industry': industry,
                'base_domain': _BASE_DOMAIN,
                'signup_url': f'{base}/connect/signup/?industry={industry}',
                'pricing_url': f'{base}/pricing/',
                'login_url': f'{base}/login/',
            })

        # صفحات التسجيل والأسعار → إعادة توجيه للدومين الرئيسي مع حفظ القطاع
        if path.startswith('/connect/signup'):
            qs = request.META.get('QUERY_STRING', '')
            if 'industry=' not in qs:
                sep = '&' if qs else ''
                return redirect(f'{base}{path}?{qs}{sep}industry={industry}')
            return redirect(f'{base}{path}?{qs}')

        if path in ('/pricing/', '/features/', '/login/'):
            return redirect(f'{base}{path}')

        # أي مسار آخر → صفحة الهبوط
        return redirect(f'/')

_ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')

_AUTOMOTIVE_BLOCKED = re.compile(
    r'^/' + re.escape(_ADMIN_URL) + r'/printing/'
)

_PRINTING_BLOCKED = re.compile(
    r'^(/system/(?!health/)|/' + re.escape(_ADMIN_URL) + r'/inventory/|/api/v1/b2b/)'
)

_INDUSTRY_EXEMPT = re.compile(
    r'^/(static|media|api/webhooks|login|account|auth|connect|pricing|features|superadmin|sw\.js|manifest\.json|offline|i18n)(/|$)'
)


class IndustryRoutingMiddleware:
    """
    يمنع الوصول العابر للقطاعات:
    - مستأجر سيارات → لا يمكنه الوصول لنماذج الطباعة في الأدمن
    - مستأجر طباعة → لا يمكنه الوصول لمسارات /system/* (الورشة)
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if connection.schema_name == 'public':
            return self.get_response(request)

        tenant = getattr(request, 'tenant', None)
        if not tenant:
            return self.get_response(request)

        path = request.path_info
        if _INDUSTRY_EXEMPT.match(path):
            return self.get_response(request)

        industry = getattr(tenant, 'industry', 'automotive')

        if industry == 'automotive' and _AUTOMOTIVE_BLOCKED.match(path):
            return HttpResponseNotFound(
                '<h1>404 — الصفحة غير موجودة</h1>'
                '<p>هذا القسم غير متوفر لحسابك.</p>'
            )

        if industry == 'printing' and _PRINTING_BLOCKED.match(path):
            return HttpResponseNotFound(
                '<h1>404 — الصفحة غير موجودة</h1>'
                '<p>هذا القسم غير متوفر لحسابك.</p>'
            )

        return self.get_response(request)


class CSRFCookieCleanupMiddleware:
    """
    🛡️ ينظف كوكي csrftoken القديم بعد الانتقال إلى mt_csrf.
    يعمل مرة واحدة لكل متصفح ثم يتوقف (لا overhead مستمر).
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # إذا المتصفح لا يزال يرسل الكوكي القديم، احذفه
        if 'csrftoken' in request.COOKIES:
            response.delete_cookie('csrftoken', path='/', domain=None)
            # أيضاً احذف نسخة الدومين الجذر إن وُجدت
            csrf_domain = getattr(settings, 'CSRF_COOKIE_DOMAIN', None)
            if csrf_domain:
                response.delete_cookie('csrftoken', path='/', domain=csrf_domain)
        return response


class AuditIPMiddleware:
    """
    يخزن IP المستخدم والمستخدم الحالي في thread-local
    حتى تستطيع signals الـ Audit Trail الوصول إليها بدون الحاجة لـ request.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # استخراج IP الحقيقي (يدعم reverse proxy)
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')

        _audit_thread_local.ip = ip
        _audit_thread_local.user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None

        response = self.get_response(request)

        # تنظيف بعد الطلب
        _audit_thread_local.ip = None
        _audit_thread_local.user = None

        return response
