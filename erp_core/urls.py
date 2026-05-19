from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseNotFound, HttpResponseServerError
from django.shortcuts import redirect
from django.core.cache import cache

import os
import time
import logging

from clients import views as client_views

# تهيئة نظام المراقبة لتسجيل الاختراقات والأنشطة السيبرانية
logger = logging.getLogger('mouss_tec_router')

# مسار لوحة التحكم المشفر المستخرج من البيئة الآمنة
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')

# =====================================================================
# 🧠 1. الموجه السحابي الذكي والتكيفي (Smart Adaptive SaaS Router)
# =====================================================================
def smart_root_router(request):
    """
    توجيه حركة المرور بذكاء بناءً على النطاق والسياق التشغيلي.
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
        return client_views.mousstec_landing_page(request)
    
    # إذا كان الزائر يفتح فرعاً مخصصاً (Tenant Subdomain) يتم توجيهه للـ Dashboard الفخمة
    return redirect('/system/dashboard/')

# =====================================================================
# 🛠️ 2. الفحص العميق لصحة النظام وزمن الاستجابة (Enterprise Health Check)
# =====================================================================
def system_health_check(request):
    """
    نظام مراقبة متطور: يختبر الاتصال وزمن الاستجابة للـ DB والـ Redis.
    🚀 ابتكار: مُهيأ لتصدير البيانات لأنظمة مثل Prometheus أو Datadog.
    """
    health_status = 200
    db_status = "operational"
    redis_status = "operational"
    circuit_breaker = "closed (safe)"
    db_latency = 0

    try:
        from django.db import connections
        start_time = time.time()
        connections['default'].cursor()
        db_latency = round((time.time() - start_time) * 1000, 2)
        
        if db_latency > 500: 
            db_status = "degraded (high latency)"
            circuit_breaker = "open (graceful degradation active)"
    except Exception:
        db_status = "critical"
        health_status = 503 

    try:
        cache.set('mouss_ping', 'pong', timeout=1)
        if cache.get('mouss_ping') != 'pong':
            redis_status = "degraded"
    except Exception:
        redis_status = "critical"
        health_status = 503

    # MAS Agents Health (from Orchestrator)
    mas_health = {}
    dlq_size = -1
    open_circuits = []
    try:
        from erp_core.orchestrator import AgentHealthMonitor, AgentRegistry, DeadLetterQueue
        mas_health   = AgentHealthMonitor.get_summary()
        dlq_size     = DeadLetterQueue().size()
        open_circuits = AgentRegistry.list_open_circuits()
    except Exception:
        pass

    if mas_health.get('failed', 0) > 0 or open_circuits:
        health_status = max(health_status, 207)  # Multi-status — some agents degraded

    return JsonResponse({
        "status": "operational" if health_status == 200 else ("degraded" if health_status == 207 else "critical"),
        "version": "4.1.0-MAS-Enterprise",
        "system": "Mouss Tec Enterprise Engine Core",
        "metrics": {
            "db_latency_ms": db_latency,
            "db_status": db_status,
            "redis_status": redis_status,
            "circuit_breaker": circuit_breaker,
        },
        "mas_agents": {
            "total":          mas_health.get("total", 0),
            "alive":          mas_health.get("alive", 0),
            "failed":         mas_health.get("failed", 0),
            "unknown":        mas_health.get("unknown", 0),
            "open_circuits":  open_circuits,
            "dlq_size":       dlq_size,
        }
    }, status=health_status)

# =====================================================================
# 🪤 3. نظام فخ الهاكرز النشط الشامل وحظر الـ IP (AI Cyber Honeypot)
# =====================================================================
def admin_honeypot(request, exception=None):
    """
    🚀 ابتكار سيبراني: التقاط الـ IP للمخترق وحظره تلقائياً لمدة 24 ساعة.
    """
    ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    ban_cache_key = f"mousstec_cyber_ban_{ip}"
    
    try:
        cache.set(ban_cache_key, "banned", timeout=86400)
        logger.critical(f"🚨 [CYBER ATTACK DETECTED] IP: {ip} probed {request.path}. Auto-banned for 24 Hours.")
    except Exception as e:
        logger.error(f"🔴 [HONEYPOT CACHE ERROR] {e}")

    return HttpResponseForbidden(
        "<h1>403 Forbidden - Security Shield Active</h1>"
        "<p>Your connection footprint has been flagged, banned, and logged by Mouss Tec Cyber Defense Engine.</p>"
    )

# =====================================================================
# 🛡️ 4. حراس الأخطاء המخصصة (Custom Branded Error Handlers)
# =====================================================================
def custom_404_handler(request, exception=None):
    """🚀 ابتكار: يمنع تسريبโครงية الروابط (URL Structure Leakage) عند حدوث خطأ"""
    if request.path.startswith('/api/'):
        return JsonResponse({"error": "endpoint_not_found", "message": "المسار المطلوب غير متوفر."}, status=404)
    # إذا كان الزائر داخل نظام ورشة، يتم إرجاعه للوحة التحكم بأمان
    if hasattr(request, 'tenant') and request.tenant.schema_name != 'public':
        return redirect('/system/dashboard/')
    return redirect('smart_root')

def custom_500_handler(request):
    """🚀 ابتكار: يمنع تسريب تفاصيل كود السيرفر (Stack Trace) للعامة"""
    if request.path.startswith('/api/'):
        return JsonResponse({"error": "internal_server_error", "message": "حدث خطأ غير متوقع بالخادم، جاري معالجته."}, status=500)
    return HttpResponseServerError(
        "<h1>500 - Internal Server Error</h1><p>عذراً، حدث عطل تقني مفاجئ. فريق الصيانة الذكي يعمل على إصلاحه حالياً.</p>"
    )

# تخصيص دوال الأخطاء لتعمل أوتوماتيكياً في الـ Production
handler404 = custom_404_handler
handler500 = custom_500_handler

# =====================================================================
# 🚦 شبكة المسارات الرئيسية الموحدة (Global Routing Matrix)
# =====================================================================
urlpatterns = [
    # 0. 🌐 الموجه التكيفي الذكي (بوابة الإمبراطورية السحابية)
    path('', smart_root_router, name='smart_root'),

    # 🏢 بوابة الاشتراك والإنشاء الآلي للشركات (Automated SaaS Onboarding)
    path('connect/signup/', client_views.register_new_tenant_saas, name='saas_customer_signup'),

    # 💳 مسار صفحة الباقات المركزية وتجديد الاشتراكات (SaaS Pricing Engine)
    path('pricing/', client_views.saas_pricing_page, name='saas_pricing'),

    # 1. 👑 لوحة تحكم الإدارة الآمنة (Central Admin Dashboard)
    path(f'{ADMIN_URL}/', admin.site.urls),

    # 2. 🪤 فخاخ الهاكرز المتقدمة وحراس المنافذ السيبرانية
    path('admin/', admin_honeypot),
    re_path(r'^(wp-admin|wp-login\.php|administrator|phpmyadmin|\.env|\.git|laravel|swagger-ui)/?$', admin_honeypot),

    # 3. 🩺 رادار فحص حالة الخادم وسلامة الاتصال السحابي (Datadog/AWS Ready)
    path('system/health/', system_health_check, name='system_health'),
    
    # 🚀 ابتكار: استقبال إشعارات بوابات الدفع اللحظية وحقنها في الـ Escrow Ledger أوتوماتيكياً
    path('api/webhooks/fintech/universal/', client_views.universal_webhook_multiplexer, name='fintech_webhook_multiplexer'),

    # ==============================================================
    # 🤝 4. بوابة واجهات برمجة سوق Mouss Tec المركزي (B2B API Gateway)
    # ==============================================================
    path('api/v1/b2b/', include([
        # 🛒 محرك البحث اللحظي في سوق قطع الغيار المشترك
        path('market/search/', client_views.b2b_market_search_api, name='api_v1_market_search'),
        
        # 🤖 رادار الذكاء الاصطناعي للتنبؤ بحجم الطلب وعجز المخازن الإقليمي
        path('market/predict/', client_views.market_demand_predictor_api, name='api_v1_market_predict'),
        
        # ⚖️ صالة المزادات العكسية النشطة (Blind Bidding RFQs)
        path('bidding/active/', client_views.active_blind_bids_api, name='api_v1_active_bids'),
        
        # 🚀 محرك إرسال تسعير المظاريف المغلقة وحقنها في محافظ الضمان المالي
        path('bidding/submit-offer/', client_views.submit_bid_offer_api, name='api_v1_submit_bid_offer'),

        # 💳 محفظة الضمان المالي ودفتر الأستاذ الرقمي الموحد (Escrow Ledger)
        path('escrow/wallet/', client_views.my_escrow_wallet_api, name='api_v1_escrow_wallet'),
    ])),

    # ==============================================================
    # 5. 🌍 مسارات الترجمة العالمية، والسيستم الداخلي للورش والفروع
    # ==============================================================
    path('i18n/', include('django.conf.urls.i18n')),
    
    # 🚀 النواة التشغيلية للورش (الكاشير، الفحص الذكي، الفواتير، وعقود الأساطيل)
    path('system/', include('inventory.urls')), 
]

# =====================================================================
# 📁 معالجة الأصول الرقمية والملفات الثابتة في بيئة التطوير والـ Sandbox
# =====================================================================
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)