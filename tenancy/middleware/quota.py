import re
import logging
from django.conf import settings
from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin
from erp_core.http_utils import safe_back as _safe_back
from django.core.cache import caches
from django.contrib import messages as django_messages
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger('mouss_tec_core')


class TenantQuotaMiddleware(MiddlewareMixin):
    """
    🛡️ حارس الباقات الديناميكي من الجيل الرابع (SaaS Enterprise Guard):
    يراقب المرور، يمنع التخطي، يدير فترات السماح، يطبق الخنق التلقائي (Rate Limit)، ويحظر المحتالين بتقنية Zero-DB-Hit.
    """
    
    # 🚀 المسارات المستثناة (Smart Bypass): 
    EXEMPT_URLS = [
        re.compile(r'^/static/'),
        re.compile(r'^/media/'),
        re.compile(r'^/api/auth/'),
        re.compile(r'^/api/webhooks/'),
        re.compile(r'^/subscription/'),
        re.compile(r'^/billing/'),
        re.compile(r'^/admin/'),
        re.compile(r'^/pricing/'),
        re.compile(r'^/payment/'),         # الدفع والإيصالات — الشركة المنتهية لازم تقدر تجدد من دومينها
        re.compile(r'^/login/'),           # صفحة جد حسابك
        re.compile(r'^/account/recovery/'), # استرجاع كلمة المرور
        re.compile(r'^/account/workspace/'), # المالك يقدر يمسح حسابه حتى بعد ما التجربة تخلص
        re.compile(r'^/auth/redirect/'),   # التوجيه الذكي بعد تسجيل الدخول
        re.compile(r'^/connect/signup/'),  # تسجيل عميل جديد
        re.compile(r'^/superadmin/'),      # لوحة السوبر أدمن
        re.compile(r'^/impersonate-login/'), # دخول السوبر أدمن على الشركات (لازم يشتغل حتى لو الاشتراك منتهي)
        re.compile(r'^/system/health/'),   # فحص صحة النظام
        re.compile(r'^/system/api/v1/ai/'),
        re.compile(r'^/printing/copilot/'),   # Smart Business Copilot
        re.compile(r'^/printing/ai/'),        # AI Studio API
        re.compile(r'^/marketplace/'),        # 🛍️ Public Customer Marketplace (sector pages, registration, dashboard)
        re.compile(r'^/features/'),           # صفحة المميزات العامة
        re.compile(r'^/automotive/'),         # صفحة هبوط السيارات
        re.compile(r'^/printing/$'),          # صفحة هبوط الطباعة (root only)
        re.compile(r'^/sw\.js$'),             # PWA service worker
        re.compile(r'^/favicon\.ico$'),       # المتصفح بيطلبها لوحده في كل صفحة
        re.compile(r'^/manifest\.json$'),     # PWA manifest
        re.compile(r'^/offline/'),            # PWA offline page
    ]

    _cached_exempt_urls = None

    @classmethod
    def _get_exempt_urls(cls):
        """بناء القائمة الكاملة مع مسار الأدمن الديناميكي"""
        if cls._cached_exempt_urls is None:
            admin_url = getattr(settings, 'ADMIN_URL', 'secure-portal')
            cls._cached_exempt_urls = cls.EXEMPT_URLS + [
                re.compile(r'^/' + re.escape(admin_url) + r'/'),
            ]
        return cls._cached_exempt_urls

    # 🚦 سرعات الباقات (طلبات لكل دقيقة)
    # Keys cover BOTH the legacy Tenant.plan CharField values
    # ('silver'/'gold'/'empire') AND the current Plan.slug values
    # ('auto-*' / 'print-*'). _resolve_plan_key() normalizes whichever
    # source we have before lookup, so neither vocabulary leaks the
    # mapping into the caller.
    # 🐛 [FIX]: الحد بيتحسب على الشركة كلها (كل المستخدمين مع بعض) — و60
    #    طلب/دقيقة لسيلفر كانت بتخلص من كاشير واحد بيدوّر في الـ POS (طلب مع
    #    كل حرف)، فالصفحة تتقلب رسالة JSON في نص البيعة. الحد للحماية من
    #    الإساءة مش لتقنين الشغل العادي.
    TIER_RATE_LIMITS = {
        # Automotive
        'silver': 300,
        'gold': 600,
        'empire': 1500,
        # Printing
        'starter': 300,
        'pro': 600,
        'enterprise': 1500,
        # Safety net
        'unknown': 200,
    }

    @staticmethod
    def _resolve_plan_key(tenant) -> str:
        """Resolve the tier key for rate-limit lookup.

        Priority:
          1. The active TenantSubscription's Plan.slug, stripped of its
             industry prefix ('auto-silver' → 'silver'). This is the
             current source of truth set by the relaunch 2026 pricing.
          2. The legacy Tenant.plan CharField. Drifts from #1 are common
             because nothing keeps them in sync; we only fall back to it
             when no subscription/plan row exists.
          3. 'unknown' — safe minimum rate.
        """
        try:
            sub = getattr(tenant, 'subscription', None)
            slug = getattr(getattr(sub, 'plan', None), 'slug', None) if sub else None
            if slug:
                key = slug.split('-', 1)[-1].lower()  # 'auto-empire' → 'empire'
                if key in TenantQuotaMiddleware.TIER_RATE_LIMITS:
                    return key
        except Exception:
            # Tenant w/o subscription relation, lazy-load errors, etc.
            # Fall through to the legacy field rather than 500.
            pass
        legacy = (getattr(tenant, 'plan', None) or '').lower()
        if legacy in TenantQuotaMiddleware.TIER_RATE_LIMITS:
            return legacy
        return 'unknown'

    def _get_tenant_status(self, tenant):
        """
        🚀 ابتكار: استخدام الكاش المحلي (LocMem) مع حساب فترات السماح (Grace Period).
        """
        cache = _get_cache()
        cache_key = f"tenant_guard_status_{tenant.schema_name}"
        
        status_data = cache.get(cache_key)
        if status_data is None:
            today = timezone.now().date()
            is_valid = tenant.is_valid_subscription
            is_fraud = getattr(tenant, 'is_fraud_flagged', False)
            
            # 🛡️ ابتكار: حساب فترة السماح (Grace Period)
            in_grace_period = False
            days_left = 0
            
            if tenant.status == 'trial':
                days_left = (tenant.trial_ends_at - today).days
            elif tenant.subscription_end_date:
                days_left = (tenant.subscription_end_date - today).days
                # إذا انتهى الاشتراك منذ أقل من 3 أيام، ندخله في فترة السماح (Read-Only)
                if not is_valid and -3 <= days_left < 0:
                    in_grace_period = True

            status_data = {
                'is_valid': is_valid,
                'is_fraud': is_fraud,
                # Source of truth: subscription.plan.slug, not the
                # legacy Tenant.plan CharField that drifts silently.
                'plan': self._resolve_plan_key(tenant),
                'days_left': max(days_left, 0),
                'in_grace_period': in_grace_period,
                'schema': tenant.schema_name
            }
            # كيش النتيجة لمدة 60 ثانية 
            cache.set(cache_key, status_data, 60)
            
        return status_data

    def _apply_rate_limit(self, status_data):
        """
        🚦 ابتكار: حماية السيرفر عن طريق خنق الطلبات المفرطة (Token Bucket Throttle)
        """
        cache = _get_cache()
        plan = status_data['plan']
        limit = self.TIER_RATE_LIMITS.get(plan, 30)
        
        # إنشاء مفتاح كاش يعتمد على الدقيقة الحالية
        current_minute = timezone.now().strftime('%Y%m%d%H%M')
        rate_key = f"rate_limit_{status_data['schema']}_{current_minute}"
        
        try:
            requests_this_minute = cache.incr(rate_key)
        except ValueError:
            # إذا لم يكن المفتاح موجوداً، ننشئه بقيمة 1 ونعطيه عمر دقيقة واحدة
            cache.set(rate_key, 1, timeout=60)
            requests_this_minute = 1
            
        return requests_this_minute <= limit

    def process_request(self, request):
        tenant = getattr(request, 'tenant', None)
        
        if not tenant or tenant.schema_name == 'public':
            return None
            
        path = request.path_info
        
        if any(m.match(path) for m in self._get_exempt_urls()):
            return None
            
        # جلب حالة العميل من الكاش
        status_data = self._get_tenant_status(tenant)
        
        # 🚨 1. الحظر النهائي (AI Fraud Shield) - لا ترحم النصابين
        if status_data['is_fraud']:
            logger.critical(f"🛑 [FRAUD LOCK] Blocked fraudster tenant: {tenant.schema_name}")
            return HttpResponseForbidden(
                "<h1>🛑 حظر أمني شامل (Security Lockdown)</h1>"
                "<p>تم حظر هذا الحساب نهائياً بناءً على تقييمات نظام الثقة والأمان المركزي. للتظلم، اتصل بالدعم الفني.</p>"
            )

        # 🚦 2. جدار السرعة (Rate Limiting)
        if not self._apply_rate_limit(status_data):
            logger.warning(f"⚠️ [RATE LIMIT HIT] Tenant {tenant.schema_name} exceeded limit for plan {status_data['plan']}.")
            msg = "تم تجاوز الحد الأقصى للطلبات لباقة اشتراكك. استنى دقيقة أو رقّي الباقة لأداء أعلى."
            wants_json = (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                          or 'application/json' in request.headers.get('Accept', '')
                          or request.content_type == 'application/json')
            if not wants_json:
                # صفحة عادية: رسالة مفهومة بدل JSON خام في نص الشاشة
                from django.http import HttpResponse
                resp = HttpResponse(
                    '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<meta http-equiv="refresh" content="20"><title>استنى لحظة</title></head>'
                    '<body style="font-family:Cairo,Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;'
                    'display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">'
                    '<div style="max-width:420px;padding:24px;text-align:center">'
                    '<div style="font-size:42px">⏳</div>'
                    f'<p style="font-size:16px;font-weight:700">{msg}</p>'
                    '<p style="color:#94a3b8;font-size:13px">الصفحة هتتحدّث لوحدها خلال ثواني.</p>'
                    '</div></body></html>', status=429)
                resp['Retry-After'] = '20'
                return resp
            return JsonResponse({
                "error": "rate_limit_exceeded",
                "message": msg,
                "code": 429
            }, status=429)
            
        # 🛡️ 3. الفحص الصارم وفترة السماح (Grace Period & Soft Lock)
        if not status_data['is_valid']:
            # إذا كان في فترة السماح، نسمح بعرض البيانات (GET) ونمنع التعديل (POST/PUT/DELETE)
            if status_data['in_grace_period']:
                if request.method in ['POST', 'PUT', 'DELETE', 'PATCH']:
                    # API calls get JSON error
                    if request.path.startswith('/api/') or request.path.startswith('/system/api/'):
                        return JsonResponse({
                            "error": "read_only_mode",
                            "message": "انتهى اشتراكك. النظام حالياً في وضع 'القراءة فقط'. يرجى التجديد لتتمكن من إضافة فواتير أو تعديل البيانات.",
                            "code": 402
                        }, status=402)
                    # Admin/browser POST: show friendly message and redirect back
                    base_domain = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
                    protocol = "http" if getattr(settings, 'DEBUG', False) else "https"
                    pricing_url = f"/pricing/?shop={tenant.schema_name}"   # دومين الشركة نفسه — المالك داخل ويقدر يدفع
                    try:
                        django_messages.error(request,
                            f'⚠️ انتهى اشتراكك! النظام في وضع "القراءة فقط" — لا يمكنك إضافة أو تعديل البيانات. '
                            f'<a href="{pricing_url}" style="color:#fff;text-decoration:underline;font-weight:bold;">جدّد الآن</a>',
                            extra_tags='safe'
                        )
                    except Exception:
                        pass
                    return redirect(_safe_back(request))
                # GET requests: inject warning banner via Django messages (once per session)
                # ⚠️ هذا الـ middleware يعمل قبل SessionMiddleware؛ نتحقق من وجود الجلسة
                grace_msg_key = f'_grace_warned_{tenant.schema_name}'
                if not hasattr(request, 'session') or not request.session.get(grace_msg_key):
                    try:
                        base_domain = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
                        protocol = "http" if getattr(settings, 'DEBUG', False) else "https"
                        pricing_url = f"/pricing/?shop={tenant.schema_name}"   # دومين الشركة نفسه — المالك داخل ويقدر يدفع
                        django_messages.warning(request,
                            f'⚠️ انتهى اشتراكك! أمامك 3 أيام فترة سماح (قراءة فقط). '
                            f'<a href="{pricing_url}" style="color:#fff;text-decoration:underline;font-weight:bold;">جدّد اشتراكك الآن</a>',
                            extra_tags='safe'
                        )
                        request.session[grace_msg_key] = True
                    except Exception:
                        pass
            else:
                # حظر كامل (Hard Lock) لأن فترة السماح انتهت
                logger.warning(f"🔒 [QUOTA GUARD] Expired access for Tenant: {tenant.schema_name}.")
                
                # استخدام النطاق الديناميكي للبيئة
                base_domain = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
                protocol = "http" if getattr(settings, 'DEBUG', False) else "https"
                # 🐛 [FIX]: كان بيحوّل للدومين الرئيسي والمالك مش داخل هناك — مايقدرش يدفع ويجدد
                target_url = f"/pricing/?shop={tenant.schema_name}"
                
                if request.path.startswith('/api/') or request.path.startswith('/system/api/'):
                    return JsonResponse({
                        "error": "subscription_required",
                        "message": "انتهى الاشتراك وفترة السماح. يرجى التجديد لاستعادة الوصول.",
                        "code": 402,
                        "upgrade_url": target_url
                    }, status=402)
                
                return redirect(target_url)
            
        # تمرير الطلب وحقن الداتا في الـ request لاستخدامها في الرد
        request._tenant_telemetry = status_data
        return None

    def process_response(self, request, response):
        """
        🚀 ابتكار: حقن رؤوس البيانات (Telemetry Headers) للفرونت إند.
        يسمح لواجهة المستخدم بإظهار شريط تحذيري ديناميكي دون استعلامات إضافية.
        """
        telemetry = getattr(request, '_tenant_telemetry', None)
        if telemetry:
            response['X-Mousstec-Plan'] = str(telemetry.get('plan', 'unknown'))
            response['X-Mousstec-Trial-Days-Left'] = str(telemetry.get('days_left', 0))
            response['X-Mousstec-Read-Only'] = "true" if telemetry.get('in_grace_period') else "false"
            
        return response

def _get_cache():
    """مساعد صغير لجلب الكاش المحلي بمرونة وتفادي أخطاء الاستدعاء"""
    return caches['local_tier'] if 'local_tier' in caches else caches['default']