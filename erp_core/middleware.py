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

        # 🆕 الـ middleware ده مخصّص لتوجيه الـ landing pages للـ portal
        # subdomains (auto./print.) — مش supposed يـ intercept الـ admin أو
        # الـ tenant API calls.
        # ─────────────────────────────────────────────────────────────────
        # قبل الـ fix ده كان أي مسار غير معروف بيـ redirect لـ `/` (سطر 75)،
        # واللي خلّى الـ fetch من dashboard على subdomain portal يرجع redirected
        # response → الـ JS يـ throw "Error: HTTP 200" والـ AI Studio modal
        # ميـ openش (Phase N.6 smoke-test bug).
        _admin_url = getattr(settings, 'ADMIN_URL', _ADMIN_URL)
        if (
            path.startswith(f'/{_admin_url}/')   # Django admin (per-tenant)
            or path.startswith('/superadmin/')   # Super admin
            or path.startswith('/printing/')     # Printing tenant APIs (incl. /printing/ai/)
            or path.startswith('/system/')       # Automotive tenant APIs
            or path.startswith('/api/')          # REST APIs
            or path.startswith('/marketplace/')  # Customer marketplace
        ):
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
    r'^(/system/(?!health/)'
    r'|/' + re.escape(_ADMIN_URL) + r'/inventory/'
    r'|/' + re.escape(_ADMIN_URL) + r'/smart_diagnostics/'
    r'|/' + re.escape(_ADMIN_URL) + r'/diagnostics_catalog/'
    r'|/api/v1/b2b/'
    r'|/api/diagnostics/'
    r')'
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


class PWAInjectorMiddleware:
    """
    🔌 يحقن manifest + PWA bootstrap في <head> أي HTML response.
    المشروع مفيهوش base.html مشترك — صفحات كتير مش بتعمل include للـ
    pwa-init.js، فالـ Service Worker مش بيتسجّل والـ install prompt مش
    بيظهر، وأي صفحة مش متخزّنة offline.

    Idempotent: مش بيحقن لو الـ tag موجود فعلاً.
    Skips: غير HTML, status != 200, الصفحات المخصّصة للـ admin/honeypot.
    """
    SCRIPT_TAG = b'<script src="/static/js/pwa-init.js" defer data-pwa-injector="1"></script>'
    MANIFEST_TAG = b'<link rel="manifest" href="/manifest.json">'
    THEME_TAG = b'<meta name="theme-color" content="#7c3aed">'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # إستثناءات مبكرة — مش HTML / مش 200 / مفيش body
        if response.status_code != 200:
            return response
        ctype = response.get('Content-Type', '')
        if 'text/html' not in ctype:
            return response
        if not hasattr(response, 'content') or not response.content:
            return response

        # تجاهل honeypot + APIs
        path = request.path_info
        if path.startswith('/api/') or path.startswith('/wp-') or 'honeypot' in path:
            return response

        try:
            body = response.content
            # Idempotency: pwa-init.js موجود فعلاً
            if b'pwa-init.js' in body:
                return response
            if b'</head>' not in body:
                return response

            injection = self.MANIFEST_TAG + self.THEME_TAG + self.SCRIPT_TAG
            new_body = body.replace(b'</head>', injection + b'</head>', 1)
            response.content = new_body
            if response.has_header('Content-Length'):
                response['Content-Length'] = str(len(new_body))
        except Exception:
            # ميكسرش الصفحة لو حصل أي مشكلة في الـ injection
            pass

        return response


class AttendanceReminderMiddleware:
    """
    👋 يحقن زرار عائم "سجّل حضورك" أسفل كل صفحة للموظفين اللي:
    - عندهم Employee record نشط
    - الـ HRSettings مفعّلة بصمة وجه أو GPS
    - مسجّلوش clock_in لليوم لسه

    الزرار بيوديهم على /hr/attendance/ مباشرة. Cached per (user, day)
    لمدة 5 دقايق عشان مفيش 3 queries على كل request.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._inject_if_needed(request, response)
        except Exception:
            pass
        return response

    def _inject_if_needed(self, request, response):
        # شروط مبكرة
        if response.status_code != 200:
            return
        if 'text/html' not in response.get('Content-Type', ''):
            return
        if not hasattr(response, 'content') or not response.content:
            return

        # تخطّي صفحة الحضور نفسها + APIs + public schema
        path = request.path_info
        if path.startswith('/hr/attendance') or path.startswith('/api/'):
            return
        if getattr(connection, 'schema_name', 'public') == 'public':
            return

        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return

        # كاش للموظف لمدة 5 دقايق
        from django.core.cache import cache
        from django.utils import timezone
        today = timezone.now().date()
        cache_key = f"attendance_reminder:{user.id}:{today}"
        cached = cache.get(cache_key)

        if cached == 'no_need':
            return  # متعرّف إنه مش محتاج

        if cached is None:
            try:
                from hr.models import Employee, AttendanceRecord, HRSettings
                emp = Employee.objects.filter(user=user, is_active=True).first()
                if not emp:
                    cache.set(cache_key, 'no_need', timeout=300)
                    return
                hr = HRSettings.get_settings()
                if not (hr.require_face_verification or hr.require_location):
                    cache.set(cache_key, 'no_need', timeout=300)
                    return
                already = AttendanceRecord.objects.filter(
                    employee=emp, date=today, clock_in__isnull=False,
                ).exists()
                if already:
                    cache.set(cache_key, 'no_need', timeout=300)
                    return
                # محتاج check-in
                cache.set(cache_key, {'name': emp.user.get_full_name() or emp.user.username}, timeout=300)
                cached = cache.get(cache_key)
            except Exception:
                return

        if not isinstance(cached, dict):
            return

        # حقن الزرار الـ floating
        button_html = (
            '<a href="/hr/attendance/" data-attendance-reminder="1" '
            'style="position:fixed;bottom:20px;left:20px;z-index:99998;'
            'background:linear-gradient(135deg,#8b5cf6,#ec4899);color:#fff;'
            'padding:12px 20px;border-radius:50px;font-weight:800;font-size:14px;'
            'text-decoration:none;box-shadow:0 8px 24px rgba(139,92,246,.4);'
            'font-family:Cairo,sans-serif;display:flex;align-items:center;gap:8px;'
            'direction:rtl;border:2px solid rgba(255,255,255,.2);">'
            '<span style="font-size:18px;">👋</span>'
            f'<span>سجّل حضورك يا {cached["name"]}</span>'
            '</a>'
        ).encode('utf-8')

        body = response.content
        # idempotency
        if b'data-attendance-reminder' in body:
            return
        if b'</body>' not in body:
            return

        new_body = body.replace(b'</body>', button_html + b'</body>', 1)
        response.content = new_body
        if response.has_header('Content-Length'):
            response['Content-Length'] = str(len(new_body))


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


class VisitorTrackingMiddleware:
    """
    تسجيل كل طلب HTTP في VisitorLog — يُستخدم في لوحة السوبر أدمن.
    يتجاهل: الأصول الثابتة، الـ healthcheck، وطلبات البوتات.
    يعمل بشكل غير مُعطِّل (non-blocking) عبر try/except.
    """

    IGNORE_PREFIXES = (
        '/static/', '/media/', '/favicon.', '/sw.js',
        '/system/health/', '/__debug__/', '/jsi18n/',
    )
    IGNORE_EXTENSIONS = ('.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.map')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import time
        start = time.time()

        response = self.get_response(request)

        # --- تتبع الزائر (non-blocking) ---
        try:
            path = request.path
            # تجاهل الأصول الثابتة
            if any(path.startswith(p) for p in self.IGNORE_PREFIXES):
                return response
            if any(path.endswith(ext) for ext in self.IGNORE_EXTENSIONS):
                return response

            elapsed_ms = int((time.time() - start) * 1000)

            # استخراج IP
            xff = request.META.get('HTTP_X_FORWARDED_FOR')
            ip = xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')

            # استخراج نوع الجهاز من User-Agent
            ua = request.META.get('HTTP_USER_AGENT', '')
            device = 'mobile' if any(k in ua.lower() for k in ['mobile', 'android', 'iphone']) else 'desktop'
            if 'bot' in ua.lower() or 'spider' in ua.lower() or 'crawler' in ua.lower():
                return response  # تجاهل البوتات

            schema = getattr(connection, 'schema_name', 'public')
            user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None

            from clients.models import VisitorLog
            VisitorLog.objects.using('default').create(
                ip_address=ip,
                path=path[:500],
                method=request.method,
                status_code=response.status_code,
                user=user,
                tenant_schema=schema,
                user_agent=ua[:1000],
                referer=(request.META.get('HTTP_REFERER', '') or '')[:1000],
                device_type=device,
                response_time_ms=elapsed_ms,
            )
        except Exception:
            pass  # Non-blocking — لا نسمح لخطأ التتبع بتعطيل الطلب

        return response
