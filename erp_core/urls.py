from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseNotFound, HttpResponseServerError
from django.shortcuts import redirect, render
from django.core.cache import cache
from django.views.decorators.cache import cache_page
from django.db import connection

import os
import time
import logging

from clients import views as client_views
from clients.views import saas_admin_views as saas_admin_views
from clients.views import support_views as support_views
from clients.views import chat_views as chat_views
from django.http import FileResponse
from erp_core.ai import advisor_views as advisor_views
from erp_core.ai import copilot_views as copilot_views
from erp_core.ai import design_views as design_views
from clients import admin_god_mode as _god
from erp_core.ai import diagnostic_views as diagnostic_views
from inventory import views_feedback as _feedback

# =====================================================================
# 🏭 فلترة التطبيقات حسب قطاع المستأجر (Industry-Aware Admin)
# سيارات → inventory فقط | طباعة → printing فقط
# =====================================================================
_original_get_app_list = admin.AdminSite.get_app_list

# Apps that live ONLY in TENANT_APPS — their tables don't exist in the public schema.
# Hide them from the admin sidebar when the request is served from public.
_TENANT_ONLY_APPS = {'inventory', 'printing', 'hr', 'import_export', 'smart_diagnostics'}

def _industry_filtered_get_app_list(self, request, app_label=None):
    app_list = _original_get_app_list(self, request, app_label=app_label)
    if connection.schema_name == 'public':
        # 🛡️ Hide tenant-only apps from the public-schema admin (their tables are
        # not present in public and any model query would raise ProgrammingError).
        return [app for app in app_list if app.get('app_label') not in _TENANT_ONLY_APPS]
    tenant = getattr(request, 'tenant', None)
    if not tenant:
        return app_list
    industry = getattr(tenant, 'industry', 'automotive')
    if industry == 'printing':
        # شركات الطباعة: إخفاء كل ما يتعلق بالسيارات/التشخيص + أدوات الـ admin
        # العامة اللي مش بتفيد المستخدم النهائي (Messenger Bot internals، Auth raw).
        hidden_apps = {
            'inventory', 'smart_diagnostics', 'diagnostics_catalog',
            'messenger_bot',  # Conversation logs + System updates internals
        }
        # موديلات داخل تطبيقات shared لازم تتخفى لأنها automotive-only أو إدارية.
        hidden_models = {
            'diagnosticsaddon', 'obddevice', 'obddevicenonce',
        }
    else:
        hidden_apps = {'printing', 'messenger_bot'}
        hidden_models = set()

    filtered = []
    for app in app_list:
        if app.get('app_label') in hidden_apps:
            continue
        models = [m for m in app.get('models', [])
                  if m.get('object_name', '').lower() not in hidden_models]
        if not models:
            continue
        new_app = dict(app)
        new_app['models'] = models
        filtered.append(new_app)
    return filtered

admin.AdminSite.get_app_list = _industry_filtered_get_app_list


def _serve_sw(request):
    """Serve Service Worker from root with correct scope and content-type headers."""
    import os as _os
    # Try staticfiles first (production after collectstatic), then source static (dev)
    sw_candidates = [
        settings.BASE_DIR / 'staticfiles' / 'sw.js',
        settings.BASE_DIR / 'static' / 'sw.js',
    ]
    sw_path = None
    for candidate in sw_candidates:
        if _os.path.isfile(candidate):
            sw_path = candidate
            break
    if not sw_path:
        return JsonResponse({'error': 'SW not found'}, status=404)
    response = FileResponse(open(sw_path, 'rb'), content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response

# تهيئة نظام المراقبة لتسجيل الاختراقات والأنشطة السيبرانية
logger = logging.getLogger('mouss_tec_router')

# مسار لوحة التحكم المشفر المستخرج من البيئة الآمنة
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')

# =====================================================================
# 🧠 1. الموجه السحابي الذكي والتكيفي (Smart Adaptive SaaS Router)
# =====================================================================
def smart_root_router(request):
    """
    توجيه حركة المرور بذكاء بناءً على النطاق والسياق التشغيلي والصناعة.

    سلوك النطاق العام (public): صفحة الهبوط التسويقية.
    سلوك نطاق المستأجر (tenant): تطبيق داخلي بحت — يتم تجاوز صفحة الهبوط
    تماماً وتوجيه الموظف مباشرة إلى لوحة التحكم أو شاشة تسجيل الدخول.
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
        return client_views.mousstec_landing_page(request)

    # المطابع → لوحة الأدمن مباشرة (لا يوجد لها dashboard مستقل)
    industry = getattr(request.tenant, 'industry', 'automotive')
    if industry == 'printing':
        if not request.user.is_authenticated:
            return redirect(f'/login/?next=/{ADMIN_URL}/')
        return redirect(f'/{ADMIN_URL}/')

    # السيارات (وأي صناعة أخرى) → Dashboard مع فرض المصادقة
    if not request.user.is_authenticated:
        return redirect('/login/?next=/system/dashboard/')
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
    """يمنع تسريب بنية الروابط عند حدوث خطأ ويوجه حسب الصناعة.

    ⚠️ مهم: قبل ما نـ redirect للـ landing، نشوف الـ request ده AJAX/Fragment ولا لأ.
    لو AJAX (مثلاً جوه modal بيـ fetch) — نرجع 404 صريح بفراجمنت HTML بسيط،
    عشان مايحطش الـ landing page HTML داخل الـ modal.
    """
    # 1. API JSON endpoints
    if request.path.startswith('/api/'):
        return JsonResponse({"error": "endpoint_not_found", "message": "المسار المطلوب غير متوفر."}, status=404)

    # 2. AJAX / Fragment endpoints + file downloads — يرجع 404 status حقيقي بدل
    #    ما يـ redirect للـ landing (اللي بيخلي الـ user يحس إن الزر اتعطل وودّاه home).
    is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    is_fetch_partial = (
        request.path.startswith('/superadmin/')
        or request.path.startswith('/ai/')
        or request.path.startswith('/marketplace/')  # gallery/store URLs ميـ redirectش لـ home
        or '/api/' in request.path
        or '/detail/' in request.path
        or '/download/' in request.path  # أي download endpoint
        or 'application/json' in request.headers.get('Accept', '')
        # file downloads — لو في .pdf/.png/.jpg في الـ URL، الـ user قاصد ملف
        or any(request.path.endswith(ext) for ext in ('.pdf', '.png', '.jpg', '.jpeg', '.csv', '.xlsx'))
    )
    if is_xhr or is_fetch_partial:
        from django.http import HttpResponseNotFound
        return HttpResponseNotFound(
            '<div class="text-center text-red-400 py-8" style="font-family:Cairo,sans-serif;">'
            '<i class="fas fa-exclamation-triangle ml-2"></i> '
            'العنصر المطلوب غير موجود أو تم حذفه.'
            '</div>'
        )

    # 3. Full-page 404 — توجيه حسب tenant/landing
    if hasattr(request, 'tenant') and request.tenant.schema_name != 'public':
        industry = getattr(request.tenant, 'industry', 'automotive')
        if industry == 'printing':
            return redirect(f'/{ADMIN_URL}/')
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

    # 🔑 صفحة "جد حسابك" — يُعيد توجيه العميل لـ Subdomain الخاص به
    path('login/', client_views.client_login_finder, name='client_login_finder'),

    # 🔑 استرجاع كلمة السر والحساب (Password Recovery)
    path('account/recovery/', client_views.account_recovery, name='account_recovery'),

    # 🔐 تغيير كلمة السر للمستخدم المسجل دخوله (works on tenant + public)
    path('account/change-password/', client_views.change_password, name='change_password'),

    # ✉️ تأكيد الإيميل بعد التسجيل + إعادة الإرسال
    path('account/verify-email/', client_views.verify_email, name='verify_email'),
    path('account/verify-email/resend/', client_views.resend_verification, name='resend_verification'),

    # 🔐 Two-Factor Authentication (TOTP)
    path('account/mfa/', client_views.mfa_setup, name='mfa_setup'),
    path('account/mfa/disable/', client_views.mfa_disable, name='mfa_disable'),
    path('account/mfa/challenge/', client_views.mfa_challenge, name='mfa_challenge'),

    # 🚦 التوجيه الذكي بعد تسجيل الدخول (Superuser → superadmin, Tenant → dashboard)
    path('auth/redirect/', client_views.smart_post_login_redirect, name='smart_post_login_redirect'),

    # 🚗 صفحة قطاع السيارات التعريفية (Automotive Landing Page)
    path('automotive/', client_views.automotive_landing_page, name='automotive_landing'),

    # 🎨 صفحة قطاع المطابع التعريفية (Printing Landing Page)
    path('printing/', client_views.printing_landing_page, name='printing_landing'),

    # 🏢 بوابة الاشتراك والإنشاء الآلي للشركات (Automated SaaS Onboarding)
    path('connect/signup/', client_views.register_new_tenant_saas, name='saas_customer_signup'),

    # 💳 مسار صفحة الباقات المركزية وتجديد الاشتراكات (SaaS Pricing Engine)
    path('pricing/', client_views.saas_pricing_page, name='saas_pricing'),

    # 📚 صفحة المميزات الكاملة
    path('features/', client_views.features_page, name='features_page'),

    # 🤖 AI Assistant API endpoint
    path('api/ai-assistant/', client_views.ai_assistant_api, name='ai_assistant_api'),

    # 🎨 Printing AI Studio endpoints
    path('printing/', include('printing.urls')),

    # 🧠 المستشار الذكي (Cognitive Advisor Agent) — Two-Stage Pipeline
    # Phase 1: Function-calling agent مع 4 tools متخصصة (cash flow, dead stock,
    # inventory sim, report links). كل tool محصور في tenant schema الحالي.
    path('advisor/printing/', advisor_views.advisor_page_printing, name='advisor_printing'),
    path('advisor/automotive/', advisor_views.advisor_page_automotive, name='advisor_automotive'),
    path('advisor/api/chat/', advisor_views.advisor_chat_api, name='advisor_chat_api'),
    path('advisor/api/reset/', advisor_views.advisor_reset_api, name='advisor_reset_api'),

    # 🎨 Premium AI Printing Copilot (Two-Stage Flux Pipeline)
    # Phase 2: Refiner Arabic→English print prompt + Flux.1 image generation.
    # Used by both Merchant AI Studio and Customer AI Studio.
    path('printing-copilot/api/generate/', copilot_views.copilot_generate, name='copilot_generate'),
    path('printing-copilot/api/send-to-print/', copilot_views.copilot_send_to_print, name='copilot_send_to_print'),
    path('printing-copilot/api/customer-search/', copilot_views.copilot_customer_search, name='copilot_customer_search'),
    # 💳 Credit balance + Top-up storefront
    path('printing-copilot/api/balance/', copilot_views.copilot_balance, name='copilot_balance'),
    path('printing-copilot/api/topup/catalog/', copilot_views.copilot_topup_catalog, name='copilot_topup_catalog'),
    path('printing-copilot/api/topup/purchase/', copilot_views.copilot_topup_purchase, name='copilot_topup_purchase'),

    # 🧠 Universal AI Design Engine (Data Flywheel) — unified for Customers & Tenants
    # Stage 1: analyze raw idea → dynamic JSON schema (any design domain).
    # Stage 2: idea + selections → mega prompt → FLUX image + flywheel log.
    # Stage 3: user feedback (is_successful) to build proprietary fine-tune dataset.
    path('ai/design/analyze/', design_views.design_analyze, name='design_analyze'),
    path('ai/design/generate/', design_views.design_generate, name='design_generate'),
    path('ai/design/feedback/', design_views.design_feedback, name='design_feedback'),
    # 📄 Print-ready spec PDF — للتحميل وإرساله للمطبعة
    path('ai/design/<int:log_id>/print-spec.pdf', design_views.design_print_spec_pdf, name='design_print_spec_pdf'),
    # 📄 Same PDF but by CustomerDesign.design_code (UUID) — للـ gallery cards
    path('marketplace/design-store/<uuid:design_code>/print-spec.pdf',
         design_views.design_print_spec_pdf_by_code, name='design_print_spec_pdf_by_code'),

    # 🛡️ God Mode — Super admin tools (impersonation + system health radar)
    path('admin-tools/impersonate/<int:customer_id>/',
         _god.admin_impersonate_customer, name='admin_impersonate_customer'),
    path('admin-tools/impersonate/exit/',
         _god.admin_impersonate_exit, name='admin_impersonate_exit'),
    path('admin-tools/system-health/',
         _god.admin_system_health, name='admin_system_health'),

    # 🚗 Auto Diagnostic Expert (BMW/MINI N13/N20/N52/N54...)
    # Phase 3: Two-stage refiner → BMW expert with torque specs & spatial accuracy.
    path('diagnostic/shop/', diagnostic_views.diagnostic_page_shop, name='diagnostic_shop'),
    path('diagnostic/customer/', diagnostic_views.diagnostic_page_customer, name='diagnostic_customer'),
    path('diagnostic/api/chat/', diagnostic_views.diagnostic_chat_api, name='diagnostic_chat_api'),
    path('diagnostic/api/reset/', diagnostic_views.diagnostic_reset_api, name='diagnostic_reset_api'),

    # 💳 بوابة الدفع عبر Paymob (Visa/Mastercard)
    path('payment/paymob/checkout/', client_views.paymob_checkout, name='paymob_checkout'),
    path('payment/paymob/callback/', client_views.paymob_callback, name='paymob_callback'),
    path('payment/success/', client_views.payment_success, name='payment_success'),
    path('payment/failed/',  client_views.payment_failed,  name='payment_failed'),

    # 💵 Manual Vodafone Cash / InstaPay — unified flow
    path('payment/manual/upload/<uuid:receipt_code>/',
         client_views.manual_payment_upload, name='manual_payment_upload'),
    path('payment/manual/subscription/start/',
         client_views.manual_pay_subscription_start, name='manual_pay_subscription_start'),
    path('payment/manual/parts/<uuid:listing_code>/start/',
         client_views.manual_pay_parts_start, name='manual_pay_parts_start'),
    path('payment/manual/design/<slug:package_slug>/start/',
         client_views.manual_pay_design_start, name='manual_pay_design_start'),
    path('payment/manual/diagnostics/<str:tier>/start/',
         client_views.manual_pay_diagnostics_start, name='manual_pay_diagnostics_start'),
    path('payment/manual/admin/review/<uuid:receipt_code>/',
         client_views.admin_review_receipt, name='admin_review_receipt'),

    # 🧩 إدارة الاشتراك وشراء الإضافات (Pro-Rated Addon Engine)
    path('subscription/manage/', client_views.manage_subscription, name='manage_subscription'),
    path('api/v1/subscription/addon/', client_views.purchase_addon_api, name='api_purchase_addon'),

    # 🔍 شحن استخدامات التشخيص (Top-up — 30 use pack)
    path('subscription/topup/diagnostics/', client_views.diag_topup_purchase, name='diag_topup_purchase'),

    # 👑 لوحة المشرف الأعلى — إدارة كل الشركات
    path('superadmin/', client_views.super_admin_dashboard, name='super_admin_dashboard'),
    path('superadmin/enter/<str:schema_name>/', client_views.enter_tenant, name='enter_tenant'),
    path('superadmin/customer/<int:customer_id>/detail/', client_views.super_admin_customer_detail, name='super_admin_customer_detail'),
    path('superadmin/customer/<int:customer_id>/delete/', client_views.super_admin_customer_delete, name='super_admin_customer_delete'),
    path('superadmin/customer/<int:customer_id>/gift/',   client_views.super_admin_customer_gift,   name='super_admin_customer_gift'),
    path('superadmin/customer/<int:customer_id>/notify/', client_views.super_admin_customer_notify, name='super_admin_customer_notify'),
    path('superadmin/tenant/<int:tenant_id>/grants/', client_views.super_admin_tenant_grants, name='super_admin_tenant_grants'),
    path('superadmin/gift-diagnostics/', client_views.super_admin_gift_diagnostics, name='super_admin_gift_diagnostics'),
    path('superadmin/tenant/<int:tenant_id>/obd-grant/', client_views.super_admin_obd_quick_grant, name='super_admin_obd_quick_grant'),

    # 🔔 Marketplace customer notifications (customer-facing)
    path('marketplace/notifications/',                       client_views.customer_notifications_list, name='customer_notifications_list'),
    path('marketplace/notifications/<int:notification_id>/read/', client_views.customer_notification_read, name='customer_notification_read'),

    # 🚗 P2P Car-Parts Marketplace
    path('marketplace/parts/',                                client_views.parts_feed,             name='parts_feed'),
    path('marketplace/parts/sell/',                           client_views.parts_create,           name='parts_create'),
    path('marketplace/parts/wanted/new/',                     client_views.parts_wanted_create,    name='parts_wanted_create'),
    path('marketplace/parts/wanted/sellers/',                 client_views.parts_wanted_seller_feed, name='parts_wanted_seller_feed'),
    path('marketplace/parts/order/<uuid:order_code>/dispute/', client_views.parts_open_dispute,    name='parts_open_dispute'),
    path('marketplace/parts/orders/',                         client_views.parts_my_orders,        name='parts_my_orders'),
    path('marketplace/parts/sales/',                          client_views.parts_my_sales,         name='parts_my_sales'),
    path('marketplace/parts/paymob-callback/',                client_views.parts_paymob_callback,  name='parts_paymob_callback'),
    path('marketplace/parts/<uuid:listing_code>/',            client_views.parts_detail,           name='parts_detail'),
    path('marketplace/parts/<uuid:listing_code>/checkout/',   client_views.parts_checkout,         name='parts_checkout'),
    path('marketplace/parts/order/<uuid:order_code>/shipped/',  client_views.parts_mark_shipped,      name='parts_mark_shipped'),
    path('marketplace/parts/order/<uuid:order_code>/delivered/', client_views.parts_confirm_delivery, name='parts_confirm_delivery'),
    path('marketplace/parts/order/<uuid:order_code>/refund/',    client_views.parts_request_refund,    name='parts_request_refund'),

    # 🚗 Admin dispute resolution
    path('superadmin/parts/order/<uuid:order_code>/refund/approve/', client_views.super_admin_parts_refund_approve, name='super_admin_parts_refund_approve'),
    path('superadmin/parts/order/<uuid:order_code>/refund/reject/',  client_views.super_admin_parts_refund_reject,  name='super_admin_parts_refund_reject'),

    # 🎛️ Phase 5: SaaS Control Center — Plan/Entitlement management + Revenue
    path('superadmin/plans/', saas_admin_views.plan_management_list, name='saas_plan_list'),
    path('superadmin/plans/<int:plan_id>/edit/', saas_admin_views.plan_management_edit, name='saas_plan_edit'),
    path('superadmin/revenue/', saas_admin_views.revenue_dashboard, name='saas_revenue_dashboard'),
    path('superadmin/diagnostics-spend/', saas_admin_views.diagnostics_spend_dashboard, name='saas_diag_spend'),

    # 🏢 Tenants management (Soft Delete / Restore / Force Delete)
    path('superadmin/tenants/', saas_admin_views.tenants_list, name='saas_tenants_list'),
    path('superadmin/tenants/<int:tenant_id>/soft-delete/', saas_admin_views.tenant_soft_delete, name='saas_tenant_soft_delete'),
    path('superadmin/tenants/<int:tenant_id>/restore/',     saas_admin_views.tenant_restore,     name='saas_tenant_restore'),
    path('superadmin/tenants/<int:tenant_id>/force-delete/',saas_admin_views.tenant_force_delete,name='saas_tenant_force_delete'),

    # 🚨 System Error Log (live)
    path('superadmin/errors/', saas_admin_views.system_errors_list, name='saas_system_errors'),
    path('superadmin/errors/<int:error_id>/resolve/', saas_admin_views.system_error_resolve, name='saas_system_error_resolve'),

    # 🛡️ Parts marketplace — moderation queue (admin approves listings before they go live)
    path('superadmin/parts/moderation/', saas_admin_views.parts_moderation_queue, name='saas_parts_moderation_queue'),
    path('superadmin/parts/moderation/<int:listing_id>/approve/', saas_admin_views.parts_moderation_approve, name='saas_parts_moderation_approve'),
    path('superadmin/parts/moderation/<int:listing_id>/reject/',  saas_admin_views.parts_moderation_reject,  name='saas_parts_moderation_reject'),

    # ⚖️ Dispute centre
    path('superadmin/disputes/', saas_admin_views.disputes_queue, name='saas_disputes_queue'),
    path('superadmin/disputes/<int:ticket_id>/resolve/', saas_admin_views.dispute_resolve, name='saas_dispute_resolve'),

    # 🛒 Parts marketplace — live/active listings control (Phase 3 #1)
    path('superadmin/parts/active/', saas_admin_views.parts_active_listings, name='saas_parts_active_listings'),
    path('superadmin/parts/active/<int:listing_id>/edit/',    saas_admin_views.parts_listing_edit,        name='saas_parts_listing_edit'),
    path('superadmin/parts/active/<int:listing_id>/suspend/', saas_admin_views.parts_listing_suspend,     name='saas_parts_listing_suspend'),
    path('superadmin/parts/active/<int:listing_id>/delete/',  saas_admin_views.parts_listing_soft_delete, name='saas_parts_listing_soft_delete'),

    # 🔧 OBD / Diagnostics-Room paid add-on (Phase 3 #3)
    path('superadmin/obd-access/',                            saas_admin_views.obd_access_list,   name='saas_obd_access_list'),
    path('superadmin/obd-access/<int:tenant_id>/grant/',      saas_admin_views.obd_access_grant,  name='saas_obd_access_grant'),
    path('superadmin/obd-access/<int:tenant_id>/revoke/',     saas_admin_views.obd_access_revoke, name='saas_obd_access_revoke'),

    # 🛍️ Marketplace requests — full management (all statuses, search, actions)
    path('superadmin/marketplace/requests/',                  saas_admin_views.marketplace_requests_list, name='saas_marketplace_requests'),

    # 📨 Support tickets
    path('support/submit/', support_views.submit_help_form, name='support_submit'),
    path('superadmin/support/', support_views.support_inbox, name='saas_support_inbox'),
    path('superadmin/support/<int:ticket_id>/', support_views.support_ticket_detail, name='saas_support_ticket_detail'),

    # 💬 Live Chat (public endpoints + admin inbox)
    path('chat/status/',                  chat_views.chat_status,         name='chat_status'),
    path('chat/open/',                    chat_views.chat_open,           name='chat_open'),
    path('chat/<int:session_id>/messages/', chat_views.chat_messages,     name='chat_messages'),
    path('chat/<int:session_id>/send/',   chat_views.chat_send,           name='chat_send'),
    path('chat/<int:session_id>/close/',  chat_views.chat_close,          name='chat_close'),
    path('superadmin/chat/',              chat_views.chat_inbox,          name='saas_chat_inbox'),
    path('superadmin/chat/<int:session_id>/', chat_views.chat_session_detail, name='saas_chat_session_detail'),
    path('superadmin/chat/<int:session_id>/close/', chat_views.chat_admin_close, name='saas_chat_admin_close'),

    # 🔐 Impersonation login (tenant-side, receives token from super admin)
    path('impersonate-login/', client_views.impersonate_login, name='impersonate_login'),

    # 🚪 Universal auto-login (tenant-side, receives token from public login)
    path('auto-login/', client_views.tenant_auto_login, name='tenant_auto_login'),

    # 1. 👑 لوحة تحكم الإدارة الآمنة (Central Admin Dashboard)
    path(f'{ADMIN_URL}/', admin.site.urls),

    # 2. 🪤 فخاخ الهاكرز المتقدمة وحراس المنافذ السيبرانية
    path('admin/', admin_honeypot),
    re_path(r'^(wp-admin|wp-login\.php|administrator|phpmyadmin|\.env|\.git|laravel|swagger-ui)/?$', admin_honeypot),

    # 3. 🩺 رادار فحص حالة الخادم وسلامة الاتصال السحابي (Datadog/AWS Ready)
    # 🛡️ محمي بـ staff_member_required — منع كشف معلومات البنية التحتية للعامة
    path('system/health/', staff_member_required(system_health_check), name='system_health'),

    # 🌐 Service Worker & PWA (Offline-First)
    # SW must be served from root (/) with correct scope header
    path('sw.js', lambda r: _serve_sw(r), name='service_worker'),
    path('offline/', lambda r: render(r, 'offline.html'), name='offline_page'),
    path('manifest.json', lambda r: JsonResponse({
        'name': 'Mouss Tec ERP',
        'short_name': 'Mouss Tec',
        'description': 'نظام إدارة الورش ومراكز الصيانة والمطابع المتكامل بالذكاء الاصطناعي',
        'start_url': '/',
        'scope': '/',
        'id': '/',
        'display': 'standalone',
        'orientation': 'any',
        'background_color': '#0f172a',
        'theme_color': '#7c3aed',
        'lang': 'ar',
        'dir': 'rtl',
        'categories': ['business', 'productivity', 'finance'],
        'icons': [
            {'src': '/static/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': '/static/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
        'shortcuts': [
            {'name': 'نقطة البيع', 'short_name': 'POS', 'url': '/system/pos/',
             'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192'}]},
            {'name': 'AI Studio', 'short_name': 'AI', 'url': '/printing/ai/history/',
             'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192'}]},
            {'name': 'السوق المفتوح', 'short_name': 'السوق', 'url': '/marketplace/merchant/feed/',
             'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192'}]},
            {'name': 'لوحة التحكم', 'short_name': 'الرئيسية', 'url': f'/{ADMIN_URL}/',
             'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192'}]},
        ],
        'prefer_related_applications': False,
    }), name='pwa_manifest'),
    
    # 🚀 ابتكار: استقبال إشعارات بوابات الدفع اللحظية وحقنها في الـ Escrow Ledger أوتوماتيكياً
    path('api/webhooks/fintech/universal/', client_views.universal_webhook_multiplexer, name='fintech_webhook_multiplexer'),

    # 💬 Mousstec Facebook Messenger bot (Meta verification + inbound messages)
    path('api/webhooks/', include('messenger_bot.urls', namespace='messenger_bot')),

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
    # 🛍️ سوق العملاء والمناقصات المجهولة (Customer Marketplace)
    # ==============================================================
    path('marketplace/', client_views.marketplace_home, name='marketplace_home'),
    path('marketplace/automotive/', client_views.marketplace_automotive, name='marketplace_automotive'),
    path('marketplace/printing/', client_views.marketplace_printing, name='marketplace_printing'),
    path('marketplace/register/', client_views.marketplace_register, name='marketplace_register'),
    path('marketplace/verify-otp/', client_views.marketplace_verify_otp, name='marketplace_verify_otp'),
    path('marketplace/login/', client_views.marketplace_login, name='marketplace_login_api'),
    path('marketplace/logout/', client_views.marketplace_logout, name='marketplace_logout'),
    path('marketplace/dashboard/', client_views.marketplace_dashboard, name='marketplace_dashboard'),
    path('marketplace/request/create/', client_views.marketplace_create_request, name='marketplace_create_request'),
    path('marketplace/request/<uuid:request_code>/', client_views.marketplace_request_detail, name='marketplace_request_detail'),
    path('marketplace/offer/<uuid:offer_code>/accept/', client_views.marketplace_accept_offer, name='marketplace_accept_offer'),
    path('marketplace/offer/<uuid:offer_code>/rate/', client_views.marketplace_rate_offer, name='marketplace_rate_offer'),
    path('marketplace/request/<uuid:request_code>/edit/', client_views.marketplace_edit_request, name='marketplace_edit_request'),

    # ✅ Super Admin: approve/reject marketplace requests
    path('marketplace/admin/approve/<int:request_id>/', client_views.marketplace_admin_approve, name='marketplace_admin_approve'),
    path('marketplace/admin/reject/<int:request_id>/', client_views.marketplace_admin_reject, name='marketplace_admin_reject'),

    # 💎 Customer-tier diagnostics — 7-day free trial → 3 paid tiers
    path('marketplace/diagnostics/',                        client_views.diagnostics_landing,         name='customer_diagnostics_landing'),
    path('marketplace/diagnostics/pricing/',                client_views.diagnostics_pricing,         name='customer_diagnostics_pricing'),
    path('marketplace/diagnostics/upgrade/<str:tier>/',     client_views.diagnostics_upgrade,         name='customer_diagnostics_upgrade'),
    path('marketplace/diagnostics/scan/',                   client_views.diagnostics_scan,            name='customer_diagnostics_scan'),
    path('marketplace/diagnostics/chat/',                   client_views.diagnostics_chat,            name='customer_diagnostics_chat'),
    path('marketplace/diagnostics/chat/reset/',             client_views.diagnostics_chat_reset,      name='customer_diagnostics_chat_reset'),

    # 🧠 Customer Advisor (sector-aware: brand for printing, vehicle for automotive)
    path('marketplace/advisor/',                            client_views.customer_advisor_page,       name='customer_advisor_page'),
    path('marketplace/advisor/chat/',                       client_views.customer_advisor_chat,       name='customer_advisor_chat'),
    path('marketplace/advisor/reset/',                      client_views.customer_advisor_reset,      name='customer_advisor_reset'),
    path('marketplace/diagnostics/paymob-callback/',        client_views.diagnostics_paymob_callback, name='customer_diagnostics_paymob_callback'),

    # 🎨 AI Designs Store (instant generation marketplace)
    path('marketplace/design-store/', client_views.design_store_home, name='design_store_home'),
    path('marketplace/design-store/buy/<slug:package_slug>/', client_views.design_store_buy, name='design_store_buy'),
    path('marketplace/design-store/payment/<uuid:purchase_code>/', client_views.design_store_payment, name='design_store_payment'),
    path('marketplace/design-store/confirm-payment/<int:purchase_id>/', client_views.design_store_confirm_payment, name='design_store_confirm_payment'),
    path('marketplace/design-store/my-designs/', client_views.design_store_my_designs, name='design_store_my_designs'),
    path('marketplace/design-store/print-orders/', client_views.design_store_my_print_orders, name='design_store_my_print_orders'),
    path('marketplace/design-store/generate/', client_views.design_store_generate, name='design_store_generate'),
    path('marketplace/design-store/<uuid:design_code>/whatsapp/', client_views.design_store_send_whatsapp, name='design_store_whatsapp'),
    path('marketplace/design-store/<uuid:design_code>/regenerate/', client_views.design_store_regenerate, name='design_store_regenerate'),
    path('marketplace/design-store/<uuid:design_code>/print-request/', client_views.design_store_print_request, name='design_store_print_request'),
    path('marketplace/design-store/<uuid:design_code>/download/<str:fmt>/', client_views.design_store_download, name='design_store_download'),
    path('marketplace/design-store/<uuid:design_code>/send-to-marketplace/', client_views.design_store_send_to_marketplace, name='design_store_send_to_marketplace'),
    path('marketplace/design-store/<uuid:design_code>/watermark/', client_views.design_store_watermark, name='design_store_watermark'),
    path('marketplace/design-store/<uuid:design_code>/chat/', client_views.design_store_chat_history, name='design_store_chat'),
    path('marketplace/design-store/<uuid:design_code>/refine/', client_views.design_store_refine, name='design_store_refine'),

    # 🎨 Brand Memory — Asset Library (Phase 5)
    path('marketplace/brand-profile/', client_views.brand_profile_view, name='brand_profile'),
    path('marketplace/brand-profile/edit/', client_views.brand_profile_page, name='brand_profile_page'),
    path('marketplace/brand-profile/logo/<str:slot>/delete/', client_views.brand_profile_delete_logo, name='brand_profile_delete_logo'),

    # 💬 Conversational Design Builder (Phase N) — feature-flagged
    path('marketplace/design-chat/', client_views.design_chat_page, name='design_chat_page'),
    path('marketplace/design-chat/start/', client_views.design_chat_start, name='design_chat_start'),
    path('marketplace/design-chat/<uuid:conversation_code>/', client_views.design_chat_state, name='design_chat_state'),
    path('marketplace/design-chat/<uuid:conversation_code>/message/', client_views.design_chat_message, name='design_chat_message'),
    path('marketplace/design-chat/<uuid:conversation_code>/undo/', client_views.design_chat_undo, name='design_chat_undo'),
    path('marketplace/design-chat/<uuid:conversation_code>/finalize/', client_views.design_chat_finalize, name='design_chat_finalize'),

    # Merchant-side marketplace
    path('marketplace/merchant/feed/', client_views.marketplace_merchant_feed, name='marketplace_merchant_feed'),
    path('marketplace/merchant/feed/count/', client_views.marketplace_merchant_feed_count, name='marketplace_merchant_feed_count'),
    path('marketplace/merchant/offer/<uuid:request_code>/', client_views.marketplace_submit_offer, name='marketplace_submit_offer'),
    path('marketplace/merchant/request/create/', client_views.marketplace_merchant_create_request, name='marketplace_merchant_create_request'),

    # ==============================================================
    # 5. 🌍 مسارات الترجمة العالمية، والسيستم الداخلي للورش والفروع
    # ==============================================================
    path('i18n/', include('django.conf.urls.i18n')),
    
    # 🚀 النواة التشغيلية للورش (الكاشير، الفحص الذكي، الفواتير، وعقود الأساطيل)
    path('system/', include('inventory.urls')),

    # ⭐ Pillar 4 — Public Customer Feedback (UUID-keyed, no login, tenant-scoped)
    path('feedback/<uuid:public_token>/',
         _feedback.customer_feedback_page,
         name='customer_feedback_page'),
    path('feedback/<uuid:public_token>/submit/',
         _feedback.customer_feedback_submit,
         name='customer_feedback_submit'),

    # 👥 الموارد البشرية (حضور/رواتب/سلف/تصميم)
    path('hr/', include('hr.urls')),

    # 🔧 Smart Diagnostics & Telematics (Premium SaaS)
    path('api/diagnostics/', include('smart_diagnostics.urls')),
]

# =====================================================================
# 📁 معالجة الأصول الرقمية والملفات الثابتة في بيئة التطوير والـ Sandbox
# =====================================================================
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)