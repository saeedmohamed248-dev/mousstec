"""robot/urls.py — mounted at /api/robot/v1/ (see erp_core/urls.py)."""

from django.urls import path

from . import views

app_name = "robot"

urlpatterns = [
    path("heartbeat/", views.heartbeat, name="heartbeat"),
    path("scan/", views.scan, name="scan"),
    path("voice/", views.voice, name="voice"),
    path("face/", views.face, name="face"),
    path("sale/", views.sale, name="sale"),
    path("motor/", views.motor, name="motor"),
    path("motor/pending/", views.motor_pending, name="motor_pending"),
    path("procurement-signals/", views.procurement_signals, name="procurement_signals"),
]
