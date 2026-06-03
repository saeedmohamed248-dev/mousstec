"""WebSocket routing for smart_diagnostics."""
from django.urls import path

from smart_diagnostics.consumers import LiveTelemetryConsumer


websocket_urlpatterns = [
    path('ws/diagnostics/live/<str:vin>/', LiveTelemetryConsumer.as_asgi()),
]
