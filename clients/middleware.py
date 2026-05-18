import re
import logging
from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger('mouss_tec_core')

class TenantQuotaMiddleware(MiddlewareMixin):
    """
    🛡️ حارس الباقات الديناميكي (SaaS Quota & Subscription Guard):
    يراقب كل حركة مرور (Traffic) لضمان عدم تخطي العملاء لصلاحياتهم أو فتراتهم التجريبية.
    يعتمد على كاش سريع لتقليل الضغط على قاعدة البيانات.
    """
    
    # 🚀 المسارات المستثناة (Smart Bypass): 
    # السماح بالمرور لملفات التصميم، تسجيل الدخول، وبوابات الدفع حتى لو الحساب موقوف!
    EXEMPT_URLS = [
        re.compile(r'^/static/'),
        re.compile(r'^/media/'),
        re.compile(r'^/api/auth/'),        # السماح بتسجيل الدخول/جلب التوكن
        re.compile(r'^/subscription/'),    # مسار صفحة الترقية (سنبنيه لاحقاً)
        re.compile(r'^/billing/'),         # مسار الدفع والفواتير
        re.compile(r'^/admin/'),           # لوحة تحكم الجانجو للضرورة
    ]

    def process_request(self, request):
        tenant = getattr(request, 'tenant', None)
        
        # 1. تخطي الـ Public Schema (الشركة الأم والريسبشن المركزي)
        if not tenant or tenant.schema_name == 'public':
            return None
            
        path = request.path_info
        
        # 2. التحقق من المسارات المستثناة (عشان العميل ميعلقش في Infinite Loop)
        if any(m.match(path) for m in self.EXEMPT_URLS):
            return None
            
        # 3. 🚨 الفحص الصارم لحالة الاشتراك (تطبيق الفخ الذهبي)
        if not tenant.is_valid_subscription:
            logger.warning(f"🔒 [QUOTA GUARD] Blocked access for Tenant: {tenant.schema_name} - Reason: Expired or Suspended.")
            
            # إذا كان الطلب جاي من تطبيق الموبايل أو SPA (API Request)
            if request.path.startswith('/api/'):
                return JsonResponse({
                    "error": "subscription_required",
                    "message": "انتهت الفترة التجريبية أو الاشتراك. يرجى ترقية الباقة لاستعادة الوصول.",
                    "code": 403
                }, status=403)
            
            # إذا كان تصفح ويب عادي من المتصفح 
            # (مؤقتاً سنعرض رسالة، ولاحقاً سنحوله لصفحة الفواتير return redirect('billing_dashboard'))
            return HttpResponseForbidden(
                "<h1>🛑 نأسف، تم إيقاف حسابك مؤقتاً.</h1>"
                "<h3>انتهت الفترة المجانية (باقة جولد) أو موعد تجديد الاشتراك.</h3>"
                "<p>يرجى التواصل مع الإدارة أو التوجه لصفحة الفواتير لتجديد باقتك واستعادة كافة بياناتك.</p>"
            )
            
        return None