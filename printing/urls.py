from django.urls import path
from . import views

app_name = 'printing'

urlpatterns = [
    # 🤖 AI Studio API endpoints
    path('ai/generate/', views.ai_generate_design, name='ai_generate'),
    path('ai/watermark/', views.ai_smart_watermark, name='ai_watermark'),
    path('ai/whatsapp/', views.ai_send_whatsapp, name='ai_whatsapp'),
    path('ai/status/', views.ai_studio_status, name='ai_status'),
]
