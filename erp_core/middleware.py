"""
Mouss Tec — Middleware Layer
============================
AuditIPMiddleware: يحفظ IP والمستخدم في thread-local لاستخدامها في Audit Trail signals.
CSRFCookieCleanupMiddleware: ينظف كوكيز CSRF القديمة بعد تغيير اسم الكوكي.
"""
import threading
from django.conf import settings

_audit_thread_local = threading.local()


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
