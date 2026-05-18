from django.urls import path, include
from . import views

# 🆕 ابتكار تنظيمي: تحديد اسم التطبيق لتجنب تداخل المسارات (Namespacing)
app_name = 'inventory'

urlpatterns = [
    # =====================================================================
    # 📊 1. واجهات المستخدم الرئيسية للفرع (Dashboards & Product Tour)
    # =====================================================================
    path('dashboard/', views.branch_dashboard, name='dashboard'),
    
    # 🔗 مسار جولة المنتج والحلول السحابية الموحدة (White-Label Product Tour)
    path('solutions-tour/', views.solutions_tour, name='solutions_tour'),
    
    # 🚀 واجهة الكاشير السريعة (Point of Sale) للعمل السريع بدون تعقيد
    path('pos/', views.pos_interface, name='pos_interface'),

    # 👨‍🔧 واجهة كشك الفنيين (شاشة تابلت للورشة لضبط الوقت والمهام)
    path('mechanic-bay/', views.mechanic_kiosk_interface, name='mechanic_kiosk'),

    # =====================================================================
    # 🖨️ 2. محرك الطباعة، المشاركة، والتوثيق (Printing & Docs Engine)
    # =====================================================================
    # طباعة مفصلة للصيانة (ورق A4)
    path('invoice/<int:invoice_id>/print/a4/', views.print_invoice_a4, name='print_invoice_a4'),
    
    # 🚀 طباعة ريسيت سريع للكاشير (ورق حراري 80mm)
    path('invoice/<int:invoice_id>/print/thermal/', views.print_invoice_thermal, name='print_invoice_thermal'),
    
    # 🚀 توليد رابط أو إرسال الفاتورة عبر WhatsApp API مباشرة
    path('invoice/<int:invoice_id>/share/whatsapp/', views.share_invoice_whatsapp, name='share_invoice_whatsapp'),

    # ✍️ حفظ التوقيع الإلكتروني للعميل على الفاتورة لإقرار الاستلام والضمان
    path('invoice/<int:invoice_id>/sign/', views.capture_digital_signature, name='capture_signature'),

    # =====================================================================
    # 🚗 3. جواز السفر الرقمي للمركبات (Vehicle Digital Twin)
    # =====================================================================
    # عرض تاريخ الصيانة الكامل للسيارة برقم الشاسيه لتعزيز موثوقية إعادة البيع
    path('vehicle/<str:chassis_number>/history/', views.vehicle_history, name='vehicle_history'),
    
    # 🚀 توليد QR Code لكل سيارة ليتم طباعته ولصقه لتسهيل الفحص المستقبلي
    path('vehicle/<str:chassis_number>/qr/', views.generate_vehicle_qr, name='vehicle_qr'),

    # =====================================================================
    # 🌐 4. الربط الخارجي والتكامل الإقليمي (Webhooks & Regional Sync)
    # =====================================================================
    # استقبال طلبات Shopify (لبيع قطع الغيار أونلاين ومزامنة المخزن لايف)
    path('webhooks/shopify/', views.shopify_webhook_receiver, name='shopify_webhook'),
    
    # استقبال إشعارات بوابات الدفع الدولية والمحلية لتأكيد التحصيلات المادية
    path('webhooks/payment/callback/', views.payment_gateway_callback, name='payment_callback'),

    # 📉 استقبال تنبيهات هبوط أسعار السوق المركزي لضبط تسعير المخزن الآلي
    path('webhooks/mousstec/price-drop/', views.market_price_sync_webhook, name='market_price_sync'),
    
    # 💸 بوابة تحديث أسعار الصرف والضرائب الإقليمية (مصر والخليج)
    path('webhooks/regional/tax-forex-sync/', views.regional_tax_forex_sync_webhook, name='tax_forex_sync'),

    # =====================================================================
    # 🔗 5. واجهات البرمجة الموحدة (Enterprise RESTful API Gateway - v1)
    # =====================================================================
    
    # 📚 ابتكار: بوابة توثيق الـ API الآلية للمطورين (Swagger/OpenAPI Ready)
    path('api/v1/docs/', views.api_documentation_view, name='v1_api_docs'),
    
    # 🌍 ابتكار: بوابة GraphQL المتقدمة لتطبيقات الموبايل الحديثة
    path('graphql/', views.graphql_gateway_view, name='graphql_endpoint'),

    path('api/v1/', include([
        
        # 🏎️ مسارات الفحص الذكية (Automotive Telemetry & IoT)
        path('telemetry/diagnostic-report/', views.receive_diagnostic_report, name='v1_diagnostic_report_receiver'),
        
        # ⏱️ ابتكار: مسار تسجيل حضور وإنتاجية الفنيين (Shift Management)
        path('mechanic/shift/<str:action>/', views.tech_shift_manager_api, name='v1_tech_shift_manager'),

        # 🏢 ابتكار: بوابة عقود أساطيل الشركات (B2B Fleet Contracts)
        path('fleet/contracts/<str:contract_code>/balance/', views.fleet_contract_balance_api, name='v1_fleet_contract_balance'),

        # 📦 محرك بحث سريع للباركود (يستخدمه الموبايل أو مسدس الباركود بالمخازن)
        path('barcode-lookup/', views.barcode_lookup_api, name='v1_barcode_lookup'),
        
        # 📱 مسار الجرد السريع بالموبايل وأمناء المخازن (Cycle Counting)
        path('inventory/cycle-count/', views.mobile_cycle_count_api, name='v1_cycle_count'),
        
        # 🔄 مسار البحث المتقاطع للقطع البديلة والمطابقة (OES Cross-Reference)
        path('inventory/parts-cross-match/', views.parts_cross_reference_api, name='v1_parts_cross_match'),

        # ⚡ مسار مزامنة الفواتير والمبيعات عند انقطاع الإنترنت (Offline POS Sync Handler)
        path('inventory/offline-sync/', views.offline_pos_sync_api, name='v1_offline_pos_sync'),
        
        # 📊 ابتكار: محرك التقارير السحابي غير المتزامن (Async Large Data Export)
        path('reports/export/request/', views.request_async_report_api, name='v1_request_async_report'),
        path('reports/export/download/<str:task_id>/', views.download_async_report_api, name='v1_download_async_report'),

        # 👑 محركات سحابة Mouss Tec (سوق التجار، المشتريات الآمنة، الذكاء الاصطناعي)
        
        # ♻️ مسار إرجاع التوالف واسترداد رسوم الأمانة (Core Charge Auto-Refund)
        path('core-charge/return/<int:item_id>/', views.return_core_charge_api, name='v1_return_core_charge'),
        
        # ⚖️ مسار إنشاء مزاد عكسي (Blind Bidding RFQ) من داخل الورشة لجلب النواقص بأقل سعر
        path('b2b/bidding/create/', views.create_blind_bid_api, name='v1_create_blind_bid'),
        
        # 🚢 مسار حاسبة تقطيع السيارات لتجار الاستيراد (Scrap Dismantling Distributor)
        path('scrap/dismantle/<int:job_id>/', views.distribute_scrap_cost_api, name='v1_distribute_scrap_cost'),
        
        # 🤖 مسار مستشار الذكاء الاصطناعي (AI Damage Predictor) يتوقع القطع من كود العطل عبر Gemini
        path('ai/estimate-repair/', views.ai_repair_estimator_api, name='v1_ai_repair_estimator'),

        # 👁️ محرك قراءة وفك نصوص فواتير الموردين بصرياً (AI Vision OCR Scanner) 
        path('ai/scan-invoice/', views.ai_ocr_invoice_scanner_api, name='v1_ai_invoice_scanner'),
        
        # 🪪 ماسح التراخيص والوثائق المرورية الذكي (AI Vehicle ID Document Extractor)
        path('ai/scan-vehicle-docs/', views.ai_vehicle_docs_scanner_api, name='v1_ai_vehicle_docs_scanner'),

    ])),
]