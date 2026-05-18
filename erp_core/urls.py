from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.core.cache import cache

import os
import time
import logging

from clients import views as client_views

# تهيئة نظام المراقبة لتسجيل الاختراقات والأنشطة السيبرانية
logger = logging.getLogger('mouss_tec_router')

# مسار لوحة التحكم المشفر المستخرج من البيئة الآمنة
# تغيير الاسم ليكون عاماً لجميع المشتركين
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal') # أو 'system-portal'
# =====================================================================
# 🧠 1. الموجه السحابي الذكي والتكيفي (Smart Adaptive SaaS Router)
# =====================================================================
def smart_root_router(request):
    """
    توجيه حركة المرور بذكاء بناءً على النطاق والسياق التشغيلي (Tenant-Aware Routing).
    🚀 ابتكار: توجيه الفروع إلى الواجهة المخصصة الفخمة بدلاً من لوحة الأدمن الجافة.
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
        return client_views.mousstec_landing_page(request)
    
    # إذا كان الزائر يفتح فرعاً مخصصاً (Tenant Subdomain) يتم توجيهه فوراً للـ Dashboard الفخمة
    return redirect('/system/dashboard/')

# =====================================================================
# 🛠️ 2. الفحص العميق لصحة النظام وزمن الاستجابة (Enterprise Health Check)
# =====================================================================
def system_health_check(request):
    """
    نظام مراقبة متطور: يختبر الاتصال وزمن الاستجابة (Latency) للـ DB والـ Redis.
    🚀 ابتكار: مدمج بمؤشر Circuit Breaker لحماية الخادم عند تزايد الأحمال الفجائية.
    """
    health_status = 200
    db_status = "operational"
    redis_status = "operational"
    circuit_breaker = "closed (safe)"
    active_tenants = 0
    db_latency = 0

    # 1. فحص قاعدة البيانات وحساب زمن الاستجابة الدقيق (Latency)
    try:
        from django.db import connections
        from clients.models import Client
        start_time = time.time()
        connections['default'].cursor()
        active_tenants = Client.objects.filter(is_active=True).count()
        db_latency = round((time.time() - start_time) * 1000, 2) # بالمللي ثانية
        
        if db_latency > 500: 
            db_status = "degraded (high latency)"
            circuit_breaker = "open (graceful degradation active)"
    except Exception:
        db_status = "critical"
        health_status = 503 

    # 2. فحص كفاءة ذاكرة الكيش (Redis)
    try:
        cache.set('mouss_ping', 'pong', timeout=1)
        if cache.get('mouss_ping') != 'pong':
            redis_status = "degraded"
    except Exception:
        redis_status = "critical"
        health_status = 503

    return JsonResponse({
        "status": "operational" if health_status == 200 else "critical",
        "version": "3.0.0-Enterprise",
        "system": "Mouss Tec Enterprise Engine Core",
        "telemetry": {
            "active_tenants": active_tenants,
            "database_node": {
                "status": db_status, 
                "latency_ms": db_latency
            },
            "redis_cache": {"status": redis_status},
            "circuit_breaker_state": circuit_breaker
        }
    }, status=health_status)

# =====================================================================
# 🪤 3. نظام فخ الهاكرز النشط الشامل وحظر الـ IP (AI Cyber Honeypot)
# =====================================================================
def admin_honeypot(request, exception=None):
    """
    🚀 ابتكار سيبراني: أي محاولة للدخول على مسارات الاختراق الشائعة يتم التقاط الـ IP
    الخاص بها وحظره تلقائياً في Redis لمدة 24 ساعة لحماية السيستم (Auto Blacklisting).
    """
    ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    ban_cache_key = f"mousstec_cyber_ban_{ip}"
    
    # حظر الـ IP في الـ Cache فوراً لمدة يوم كامل لقطع الاتصال عن البوت المهاجم
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
# 🚦 شبكة المسارات الرئيسية الموحدة (Global Routing Matrix)
# =====================================================================
urlpatterns = [
    # 0. 🌐 الموجه التكيفي الذكي (بوابة الإمبراطورية السحابية)
    path('', smart_root_router, name='smart_root'),

    # 🏢 ابتكار: بوابة الاشتراك والإنشاء الآلي للشركات الجديدة لربطها بالـ Landing Page
    path('connect/signup/', client_views.register_new_tenant_saas, name='saas_customer_signup'),

    # 1. 👑 لوحة تحكم الإدارة الآمنة (Central Admin Dashboard)
    path(f'{ADMIN_URL}/', admin.site.urls),

    # 2. 🪤 فخاخ الهاكرز المتقدمة وحراس المنافذ السيبرانية
    path('admin/', admin_honeypot),
    re_path(r'^(wp-admin|wp-login\.php|administrator|phpmyadmin|\.env|\.git|laravel|swagger-ui)/?$', admin_honeypot),

    # 3. 🩺 رادار فحص حالة الخادم وسلامة الاتصال السحابي (Datadog/AWS Ready)
    path('system/health/', system_health_check, name='system_health'),

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
    
    # 🚀 النواة التشغيلية للفروع (الكاشير، الفحص، الفواتير، وعقود الأساطيل)
    path('system/', include('inventory.urls')), 
]

# =====================================================================
# 📁 معالجة الأصول الرقمية والملفات الثابتة في بيئة التطوير والـ Sandbox
# =====================================================================
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)