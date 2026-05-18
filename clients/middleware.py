import re
import logging
from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin
from django.core.cache import caches
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger('mouss_tec_core')

class TenantQuotaMiddleware(MiddlewareMixin):
    """
    🛡️ حارس الباقات الديناميكي من الجيل الثالث (SaaS Enterprise Guard):
    يراقب المرور، يمنع التخطي، يوجه للمدفوعات، ويحظر المحتالين بتقنية Zero-DB-Hit.
    """
    
    # 🚀 المسارات المستثناة (Smart Bypass): 
    EXEMPT_URLS = [
        re.compile(r'^/static/'),
        re.compile(r'^/media/'),
        re.compile(r'^/api/auth/'),        # السماح بتسجيل الدخول
        re.compile(r'^/subscription/'),    # مسار الاشتراكات
        re.compile(r'^/billing/'),         # مسار الفواتير
        re.compile(r'^/admin/'),           # لوحة تحكم النظام
        re.compile(r'^/pricing/'),         # صفحة الباقات المفتوحة
    ]

    def _get_tenant_status(self, tenant):
        """
        🚀 ابتكار: استخدام الكاش المحلي (LocMem) لقراءة حالة الـ Tenant في أجزاء من المايكروثانية.
        """
        cache = caches['local_tier'] if 'local_tier' in caches else caches['default']
        cache_key = f"tenant_guard_status_{tenant.schema_name}"
        
        status_data = cache.get(cache_key)
        if status_data is None:
            # تقييم الحالة من قاعدة البيانات إذا لم تكن مكيشة
            is_valid = tenant.is_valid_subscription
            is_fraud = getattr(tenant, 'is_fraud_flagged', False)
            
            # حساب أيام التجربة المتبقية للـ Headers
            days_left = 0
            if tenant.status == 'trial':
                days_left = (tenant.trial_ends_at - timezone.now().date()).days
            
            status_data = {
                'is_valid': is_valid,
                'is_fraud': is_fraud,
                'plan': tenant.plan,
                'days_left': max(days_left, 0)
            }
            # كيش النتيجة لمدة 60 ثانية لتخفيف الضغط
            cache.set(cache_key, status_data, 60)
            
        return status_data

    def process_request(self, request):
        tenant = getattr(request, 'tenant', None)
        
        if not tenant or tenant.schema_name == 'public':
            return None
            
        path = request.path_info
        
        if any(m.match(path) for m in self.EXEMPT_URLS):
            return None
            
        # جلب حالة العميل من الكاش
        status_data = self._get_tenant_status(tenant)
        
        # 🚨 1. الحظر النهائي (AI Fraud Shield) - لا ترحم النصابين
        if status_data['is_fraud']:
            logger.critical(f"🛑 [FRAUD LOCK] Blocked fraudster tenant: {tenant.schema_name}")
            return HttpResponseForbidden(
                "<h1>🛑 حظر أمني شامل (Security Lockdown)</h1>"
                "<p>تم حظر هذا الحساب نهائياً بناءً على تقييمات نظام الثقة والأمان المركزي.</p>"
            )
            
        # 💰 2. الفحص الصارم للاشتراك (The Gold Trap Redirect)
        if not status_data['is_valid']:
            logger.warning(f"🔒 [QUOTA GUARD] Expired access for Tenant: {tenant.schema_name}. Redirecting to billing.")
            
            # إذا كان الطلب من تطبيق الموبايل أو الـ API
            if request.path.startswith('/api/'):
                # 🚀 استخدام 402 Payment Required كمعيار عالمي للـ SaaS
                return JsonResponse({
                    "error": "subscription_required",
                    "message": "انتهت الفترة التجريبية أو الاشتراك. يرجى ترقية الباقة لاستعادة الوصول.",
                    "code": 402,
                    "upgrade_url": f"https://mousstec.com/pricing/?shop={tenant.schema_name}"
                }, status=402)
            
            # إذا كان تصفح ويب، وجهه فوراً لصفحة الباقات في الدومين الرئيسي ليدفع
            # نمرر اسم الورشة في الرابط (shop=...) لكي يعرف الدومين الرئيسي من سيدفع!
            target_url = f"https://mousstec.com/pricing/?shop={tenant.schema_name}"
            return redirect(target_url)
            
        # تمرير الطلب وحقن الداتا في الـ request لاستخدامها في الرد
        request._tenant_telemetry = status_data
        return None

    def process_response(self, request, response):
        """
        🚀 ابتكار: حقن رؤوس البيانات (Telemetry Headers) للفرونت إند.
        يسمح لواجهة المستخدم بإظهار شريط تحذيري (مثال: باقي يوم واحد على انتهاء التجربة) دون استعلامات إضافية.
        """
        telemetry = getattr(request, '_tenant_telemetry', None)
        if telemetry:
            response['X-Mousstec-Plan'] = telemetry.get('plan', 'unknown')
            response['X-Mousstec-Trial-Days-Left'] = str(telemetry.get('days_left', 0))
            
        return response