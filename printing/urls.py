from django.shortcuts import redirect
from django.urls import path, reverse
from . import views


def _dashboard(request):
    # 🐛 [FIX]: كانت بتعرض لوحة فرع السيارات (فواتير/قطع غيار) لشركة المطبعة —
    # لوحة المطبعة الحقيقية هي صفحة الأدمن الرئيسية.
    return redirect(reverse('admin:index'))


app_name = 'printing'

urlpatterns = [
    # 📊 Quick-access dashboard (نفس لوحة /system/dashboard/ بتاع السيارات،
    # عشان شركات الطباعة يكون عندها نفس الـ UX السريع للوصول للأرقام
    # المحورية بدل ما تعدّي على /secure-portal/ الثقيل)
    path('dashboard/', _dashboard, name='dashboard'),

    # 🤖 AI Studio API endpoints
    path('ai/generate/', views.ai_generate_design, name='ai_generate'),
    path('ai/watermark/', views.ai_smart_watermark, name='ai_watermark'),
    path('ai/whatsapp/', views.ai_send_whatsapp, name='ai_whatsapp'),
    path('ai/status/', views.ai_studio_status, name='ai_status'),

    # 🧠 Smart Business Copilot
    path('copilot/chat/', views.copilot_chat, name='copilot_chat'),

    # 🎨 AI Prompt Engineer Agent
    path('ai/prompt-engineer/', views.ai_prompt_engineer, name='ai_prompt_engineer'),

    # 💾 AI Studio History
    path('ai/history/', views.ai_studio_history, name='ai_history'),
    path('ai/history/api/', views.ai_studio_history_api, name='ai_history_api'),
    path('ai/session/<int:session_id>/favorite/', views.ai_session_toggle_favorite, name='ai_session_favorite'),
    path('ai/session/<int:session_id>/delete/', views.ai_session_delete, name='ai_session_delete'),

    # 🔗 Attach AI design to invoice
    path('ai/attach-search/', views.ai_attach_search, name='ai_attach_search'),
    path('ai/session/<int:session_id>/attach/', views.ai_session_attach, name='ai_session_attach'),

    # 🏷️ Product Type Autocomplete & Report
    path('api/product-types/', views.product_type_autocomplete, name='product_type_autocomplete'),
    path('api/product-types/report/', views.product_type_report, name='product_type_report'),

    # 📒 Customer Statement (كشف حساب العميل)
    path('customer/<int:customer_id>/statement/', views.customer_statement, name='customer_statement'),

    # 📈 P&L Report (تقرير الأرباح والخسائر)
    path('reports/profit-loss/', views.profit_loss_report, name='profit_loss_report'),

    # 📊 Order Profit Detail (تحليل ربحية الطلب)
    path('order/<int:order_id>/profit/', views.order_profit_detail, name='order_profit_detail'),

    # 👥 موظفين المطبعة (إضافة بدور + صلاحيات)
    path('staff/', views.staff_list, name='staff_list'),
    path('staff/add/', views.staff_add, name='staff_add'),
    path('staff/<int:user_id>/', views.staff_edit, name='staff_edit'),

    # 🧾 فاتورة/إيصال الطلب (و ?ticket=1 أمر شغل للإنتاج)
    path('order/<int:order_id>/invoice/', views.order_invoice, name='order_invoice'),

    # 💰 Price Quotations (عروض الأسعار)
    path('quotation/create/', views.quotation_create, name='quotation_create'),
    path('quotation/view/<uuid:share_token>/', views.quotation_public_view, name='quotation_public_view'),
    path('quotation/view/<uuid:share_token>/respond/', views.quotation_respond, name='quotation_respond'),
    path('quotation/<int:quote_id>/convert/', views.quotation_convert_to_order, name='quotation_convert'),

    # 🖼️ Proof approval (رابط البروفة للعميل)
    path('proof/<uuid:share_token>/', views.proof_public_view, name='proof_public_view'),
    path('proof/<uuid:share_token>/respond/', views.proof_respond, name='proof_respond'),

    # 📒 Accounting (ميزان/ميزانية/يومية/أعمار/ضريبة/فترات + مطابقة بنك) + كشف حساب المورد
    path('accounting/', views.accounting_overview, name='accounting_overview'),
    path('accounting/vat/file/', views.vat_return_file, name='vat_return_file'),
    path('accounting/periods/action/', views.periods_action, name='periods_action'),
    path('accounting/reconcile/<int:treasury_id>/', views.bank_reconciliation, name='bank_reconciliation'),
    path('material/<int:material_id>/card/', views.material_stock_card, name='material_stock_card'),
    path('supplier/<int:supplier_id>/statement/', views.supplier_statement, name='supplier_statement'),
]
