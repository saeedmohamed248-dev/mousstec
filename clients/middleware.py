import re
import logging
from django.conf import settings
from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin
from django.core.cache import caches
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
        re.compile(r'^/login/'),           # صفحة جد حسابك
        re.compile(r'^/account/recovery/'), # استرجاع كلمة المرور
        re.compile(r'^/auth/redirect/'),   # التوجيه الذكي بعد تسجيل الدخول
        re.compile(r'^/connect/signup/'),  # تسجيل عميل جديد
        re.compile(r'^/superadmin/'),      # لوحة السوبر أدمن
        re.compile(r'^/system/health/'),   # فحص صحة النظام
        re.compile(r'^/system/api/v1/ai/'),
    ]

    @classmethod
    def _get_exempt_urls(cls):
        """بناء القائمة الكاملة مع مسار الأدمن الديناميكي"""
        if not hasattr(cls, '_cached_exempt_urls'):
            admin_url = getattr(settings, 'ADMIN_URL', 'secure-portal')
            cls._cached_exempt_urls = cls.EXEMPT_URLS + [
                re.compile(r'^/' + re.escape(admin_url) + r'/'),
            ]
        return cls._cached_exempt_urls

    # 🚦 سرعات الباقات (طلبات لكل دقيقة)
    TIER_RATE_LIMITS = {
        'silver': 60,
        'gold': 300,
        'empire': 1000,
        'unknown': 30
    }

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
                'plan': tenant.plan or 'unknown',
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
            return JsonResponse({
                "error": "rate_limit_exceeded",
                "message": "تم تجاوز الحد الأقصى للطلبات لباقة اشتراكك. يرجى الترقية للحصول على أداء أعلى.",
                "code": 429
            }, status=429)
            
        # 🛡️ 3. الفحص الصارم وفترة السماح (Grace Period & Soft Lock)
        if not status_data['is_valid']:
            # إذا كان في فترة السماح، نسمح بعرض البيانات (GET) ونمنع التعديل (POST/PUT/DELETE)
            if status_data['in_grace_period']:
                if request.method in ['POST', 'PUT', 'DELETE', 'PATCH']:
                    return JsonResponse({
                        "error": "read_only_mode",
                        "message": "انتهى اشتراكك. النظام حالياً في وضع 'القراءة فقط'. يرجى التجديد لتتمكن من إضافة فواتير أو تعديل البيانات.",
                        "code": 402
                    }, status=402)
                # إذا كان GET، نعبره بسلاسة ولكن مع حقن بيانات التحذير (التي ستظهر شريطاً أحمر في الفرونت إند)
            else:
                # حظر كامل (Hard Lock) لأن فترة السماح انتهت
                logger.warning(f"🔒 [QUOTA GUARD] Expired access for Tenant: {tenant.schema_name}.")
                
                # استخدام النطاق الديناميكي للبيئة
                base_domain = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
                protocol = "http" if getattr(settings, 'DEBUG', False) else "https"
                target_url = f"{protocol}://{base_domain}/pricing/?shop={tenant.schema_name}"
                
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