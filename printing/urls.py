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

    # 🏷️ Product Type Autocomplete & Report
    path('api/product-types/', views.product_type_autocomplete, name='product_type_autocomplete'),
    path('api/product-types/report/', views.product_type_report, name='product_type_report'),
]
