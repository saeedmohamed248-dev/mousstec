"""URL routing for smart_diagnostics (mounted under /api/diagnostics/)."""
from django.urls import path

from smart_diagnostics.api import views as api_views
from smart_diagnostics import views as html_views

app_name = 'smart_diagnostics'

urlpatterns = [
    path('scan/', api_views.scan_dtc, name='scan-dtc'),
    path('vin/<str:vin>/decode/', api_views.decode_vin, name='decode-vin'),
    path('vin/<str:vin>/passport/', api_views.vehicle_health_passport, name='health-passport'),
    path('dtc/<str:code>/plan/', api_views.dtc_test_plan, name='dtc-plan'),
    path('dtc/<str:code>/parts/', api_views.dtc_parts, name='dtc-parts'),
    # HTML dashboard
    path('live/<str:vin>/', html_views.live_dashboard, name='live-dashboard'),

    # 🤖 AI Diagnostics Room — Web-Bluetooth co-pilot workstation
    path('room/', html_views.diagnostics_room, name='diagnostics-room'),
    path('room/chat/', html_views.diagnostics_room_chat, name='diagnostics-room-chat'),

    # Device management (token-based OBD auth)
    path('devices/', html_views.device_list, name='device-list'),
    path('devices/register/', html_views.device_register, name='device-register'),
    path('devices/<int:device_id>/rotate/', html_views.device_rotate, name='device-rotate'),
    path('devices/<int:device_id>/toggle/', html_views.device_toggle, name='device-toggle'),

    # 💎 Premium upgrade landing
    path('upgrade/', html_views.upgrade_premium, name='upgrade-premium'),
]
