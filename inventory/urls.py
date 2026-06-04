from django.urls import path, include
from django.views.decorators.cache import cache_page # 🚀 ابتكار: Edge Route Caching
from django.views.decorators.csrf import csrf_exempt
from . import views
from . import views_lightning

# 🆕 ابتكار تنظيمي موحد: تحديد اسم التطبيق لتجنب تداخل المسارات (Namespacing)
app_name = 'inventory'

urlpatterns = [
    # =====================================================================
    # 📊 1. واجهات المستخدم الرئيسية للفرع (Dashboards & Experiences)
    # =====================================================================
    path('dashboard/', views.branch_dashboard, name='dashboard'),

    # 🏦 Bank Reconciliation UI
    path('bank-reconciliation/', views.bank_reconciliation_dashboard, name='bank_reconciliation'),
    path('bank-reconciliation/<int:statement_id>/', views.bank_reconciliation_detail, name='bank_reconciliation_detail'),
    
    # 🔗 مسار الجولة السحابية (مُكيش لمدة 15 دقيقة لتخفيف الضغط - Zero-DB Hit)
    path('solutions-tour/', cache_page(60 * 15)(views.solutions_tour), name='solutions_tour'),

    # 🛒 سوق B2B التفاعلي — بحث حي في السوق المركزي للقطع والمزادات
    path('b2b-market/', views.b2b_marketplace, name='b2b_marketplace'),
    
    # 🚀 واجهة الكاشير السريعة (Point of Sale - Zero Latency)
    path('pos/', views.pos_interface, name='pos_interface'),

    # ⚡ Lightning POS — walk-in retail spare parts (no vehicle, no maintenance)
    path('lightning-pos/', views_lightning.lightning_pos, name='lightning_pos'),
    path('lightning-pos/search/', views_lightning.product_quick_search, name='lightning_pos_search'),
    path('lightning-pos/checkout/', views_lightning.lightning_pos_checkout, name='lightning_pos_checkout'),

    # 📦 Quick Product Entry — product + starting stock in one form
    path('quick-product/', views_lightning.quick_product_entry, name='quick_product'),
    path('quick-product/create/', views_lightning.quick_product_create, name='quick_product_create'),

    # 📋 Job Card (Repair Order) — single-screen customer + vehicle + parts + services + DVI
    path('job-card/', views_lightning.job_card_create, name='job_card_create'),
    path('job-card/customers/', views_lightning.customer_search, name='job_card_customer_search'),
    path('job-card/save/', views_lightning.job_card_save, name='job_card_save'),

    # 👨‍🔧 واجهة كشك الفنيين (Tablet UI) لضبط وقت المهام والإنتاجية
    path('mechanic-bay/', views.mechanic_kiosk_interface, name='mechanic_kiosk'),

    # =====================================================================
    # 🖨️ 2. محرك الطباعة، المشاركة، والتوثيق (Printing & Docs Engine)
    # =====================================================================
    path('invoice/<int:invoice_id>/print/a4/', views.print_invoice_a4, name='print_invoice_a4'),
    path('invoice/<int:invoice_id>/print/thermal/', views.print_invoice_thermal, name='print_invoice_thermal'),
    path('invoice/<int:invoice_id>/export/pdf/', views.export_invoice_pdf, name='export_invoice_pdf'),
    
    # 🚀 مشاركة الفاتورة عبر WhatsApp API 
    path('invoice/<int:invoice_id>/share/whatsapp/', views.share_invoice_whatsapp, name='share_invoice_whatsapp'),

    # ✍️ حفظ التوقيع الإلكتروني للإقرار بالضمان (Legal Compliance)
    path('invoice/<int:invoice_id>/sign/', views.capture_digital_signature, name='capture_signature'),

    # =====================================================================
    # 🚗 3. جواز السفر الرقمي للمركبات (Vehicle Digital Twin)
    # =====================================================================
    # عرض الـ History للسيارة عبر الـ VIN لرفع قيمتها السوقية عند إعادة البيع
    path('vehicle/<str:chassis_number>/history/', views.vehicle_history, name='vehicle_history'),
    
    # 🚀 توليد QR Code للسيارة (مُكيش لمدة 24 ساعة لتسريع الطباعة)
    path('vehicle/<str:chassis_number>/qr/', cache_page(60 * 60 * 24)(views.generate_vehicle_qr), name='vehicle_qr'),

    # =====================================================================
    # 🌐 4. الربط الخارجي والتكامل الإقليمي (Webhooks & Regional Sync)
    # 🛡️ ابتكار: إعفاء الـ CSRF هنا إلزامي مع تطبيق Hmac Validation داخل الـ View
    # =====================================================================
    path('webhooks/shopify/', csrf_exempt(views.shopify_webhook_receiver), name='shopify_webhook'),
    path('webhooks/payment/callback/', csrf_exempt(views.payment_gateway_callback), name='payment_callback'),
    
    # 📉 استقبال تنبيهات هبوط الأسعار من الرادار المركزي للـ B2B
    path('webhooks/mousstec/price-drop/', csrf_exempt(views.market_price_sync_webhook), name='market_price_sync'),
    
    # 💸 بوابة التكيف المالي لتحديث أسعار الصرف (Inflation Hedge)
    path('webhooks/regional/tax-forex-sync/', csrf_exempt(views.regional_tax_forex_sync_webhook), name='tax_forex_sync'),

    # =====================================================================
    # 🔗 5. واجهات البرمجة الموحدة (Enterprise RESTful API Gateway - v1)
    # =====================================================================
    
    # 📚 التوثيق الآلي للمطورين (مُكيش لتقليل تحميل السيرفر)
    path('api/v1/docs/', cache_page(60 * 60)(views.api_documentation_view), name='v1_api_docs'),
    path('graphql/', views.graphql_gateway_view, name='graphql_endpoint'),

    path('api/v1/', include([
        
        # 🏎️ مسارات الفحص الذكية واستقبال إشارات הـ IoT (Automotive Telemetry OBD2)
        path('telemetry/diagnostic-report/', views.receive_diagnostic_report, name='v1_diagnostic_report_receiver'),
        
        # ⏱️ إدارة الورديات وإنتاجية الفنيين
        path('mechanic/shift/<str:action>/', views.tech_shift_manager_api, name='v1_tech_shift_manager'),

        # 🏢 بوابة عقود الأساطيل (B2B Fleet SLAs)
        path('fleet/contracts/<str:contract_code>/balance/', views.fleet_contract_balance_api, name='v1_fleet_contract_balance'),

        # 📦 محرك الجرد السريع بالموبايل أو مسدس الباركود
        path('barcode-lookup/', views.barcode_lookup_api, name='v1_barcode_lookup'),
        path('inventory/cycle-count/', views.mobile_cycle_count_api, name='v1_cycle_count'),
        
        # 🔄 محرك المطابقة الهندسية والبدائل (OEM Cross-Reference)
        path('inventory/parts-cross-match/', views.parts_cross_reference_api, name='v1_parts_cross_match'),

        # ⚡ 🚀 ابتكار: مسار الدمج اللامركزي للأنظمة القديمة (Legacy System Integration)
        path('inventory/sync/decentralized/', views.legacy_system_sync_api, name='v1_legacy_system_sync'),

        # ⚡ مزامنة الـ POS عند عودة الإنترنت (Offline Resilience)
        path('inventory/offline-sync/', views.offline_pos_sync_api, name='v1_offline_pos_sync'),
        
        # 📊 التقارير غير المتزامنة لحماية الرامات من الانهيار
        path('reports/export/request/', views.request_async_report_api, name='v1_request_async_report'),
        path('reports/export/download/<str:task_id>/', views.download_async_report_api, name='v1_download_async_report'),

        # =================================================================
        # 👑 محركات السوق والمشتريات (B2B & Procurement)
        # =================================================================
        
        # 🛒 بحث حي في السوق المشترك
        path('b2b/market/search/', views.b2b_market_search_api, name='v1_b2b_market_search'),
        
        # ♻️ الاسترداد التلقائي لتأمين الكور
        path('core-charge/return/<int:item_id>/', views.return_core_charge_api, name='v1_return_core_charge'),
        
        # ⚖️ طلب مزاد عكسي لتوفير النواقص من السوق المركزي
        path('b2b/bidding/create/', views.create_blind_bid_api, name='v1_create_blind_bid'),
        
        # 🚢 حاسبة تقطيع السيارات الاستيراد وتوزيع التكلفة
        path('scrap/dismantle/<int:job_id>/', views.distribute_scrap_cost_api, name='v1_distribute_scrap_cost'),
        
        # =================================================================
        # 🧠 الطبقة الإدراكية للذكاء الاصطناعي (Cognitive AI Layer & MAS)
        # =================================================================

        # 🚀 🚀 الأوركسترا المجمعة (Multi-Agent Pipeline) - المسار المركزي الجديد
        path('ai/orchestrator/', views.unified_ai_agent_orchestrator_api, name='v1_ai_orchestrator'),

        # 🤖 وكيل التشخيص المستقل (DTC Prognostics)
        path('ai/estimate-repair/', views.ai_repair_estimator_api, name='v1_ai_repair_estimator'),

        # 👁️ وكيل الرؤية لفواتير الموردين (AI OCR Scanner)
        path('ai/scan-invoice/', views.ai_ocr_invoice_scanner_api, name='v1_ai_invoice_scanner'),
        
        # 🪪 وكيل مسح التراخيص והـ VIN (AI Vehicle ID Document)
        path('ai/scan-vehicle-docs/', views.ai_vehicle_docs_scanner_api, name='v1_ai_vehicle_docs_scanner'),

        # 🚀 وكيل الاستطلاع المبكر لتحليل أسعار المنافسين لايف
        path('ai/market-recon/', views.ai_competitor_recon_api, name='v1_ai_competitor_recon'),

        # =================================================================
        # 📊 التقارير المالية والأرباح والخسائر (P&L & Analytics)
        # =================================================================
        path('reports/profit-loss/', views.profit_loss_report_api, name='v1_profit_loss_report'),
        path('reports/inventory-forecast/', views.inventory_forecast_api, name='v1_inventory_forecast'),
        path('reports/product-profitability/', views.product_profitability_api, name='v1_product_profitability'),
        path('reports/inventory-movements/', views.inventory_movement_log_api, name='v1_inventory_movements'),
        path('reports/trial-balance/', views.trial_balance_api, name='v1_trial_balance'),
        path('reports/balance-sheet/', views.balance_sheet_api, name='v1_balance_sheet'),
        path('accounting/ledger/<int:account_id>/', views.account_ledger_api, name='v1_account_ledger'),

        # 🏦 Bank Reconciliation
        path('bank/upload/', views.bank_statement_upload, name='v1_bank_upload'),
        path('bank/<int:statement_id>/auto-match/', views.bank_reconciliation_auto_match, name='v1_bank_auto_match'),

        # =================================================================
        # 📥 الاستيراد الآمن (Safe Import System)
        # =================================================================
        path('import/upload/', views.import_upload_api, name='v1_import_upload'),
        path('import/<uuid:session_id>/preview/', views.import_preview_api, name='v1_import_preview'),
        path('import/<uuid:session_id>/confirm/', views.import_confirm_api, name='v1_import_confirm'),
        path('import/<uuid:session_id>/rollback/', views.import_rollback_api, name='v1_import_rollback'),

        # =================================================================
        # 📄 كشوف الحساب (Statement of Account)
        # =================================================================
        # 🚗 تصفية المركبات حسب العميل (Vehicle-Customer Filter)
        path('vehicles/by-customer/<int:customer_id>/', views.vehicles_by_customer_api, name='v1_vehicles_by_customer'),

        path('statement/customer/<int:customer_id>/', views.customer_statement_api, name='v1_customer_statement'),
        path('statement/vendor/<int:vendor_id>/', views.vendor_statement_api, name='v1_vendor_statement'),

    ])),

    # =====================================================================
    # 🖨️ كشوف الحساب للطباعة (Statement Print Views)
    # =====================================================================
    path('statement/customer/<int:customer_id>/print/', views.customer_statement_print, name='customer_statement_print'),
    path('statement/vendor/<int:vendor_id>/print/', views.vendor_statement_print, name='vendor_statement_print'),
]