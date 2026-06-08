from django.urls import path
from . import views

app_name = 'printing'

urlpatterns = [
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

    # 💰 Price Quotations (عروض الأسعار)
    path('quotation/create/', views.quotation_create, name='quotation_create'),
    path('quotation/view/<uuid:share_token>/', views.quotation_public_view, name='quotation_public_view'),
    path('quotation/view/<uuid:share_token>/respond/', views.quotation_respond, name='quotation_respond'),
    path('quotation/<int:quote_id>/convert/', views.quotation_convert_to_order, name='quotation_convert'),
]
