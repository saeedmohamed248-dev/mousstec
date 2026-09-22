"""robot/urls.py — mounted at /api/robot/v1/ (see erp_core/urls.py)."""

from django.urls import path

from . import views

app_name = "robot"

urlpatterns = [
    path("heartbeat/", views.heartbeat, name="heartbeat"),
    path("scan/", views.scan, name="scan"),
    path("voice/", views.voice, name="voice"),
    path("face/", views.face, name="face"),
    path("customer/greet/", views.customer_greet, name="customer_greet"),
    path("sale/", views.sale, name="sale"),
    path("intake/", views.intake, name="intake"),
    path("stock-take/", views.stock_take, name="stock_take"),
    path("stock-take/apply/", views.stock_take_apply, name="stock_take_apply"),
    path("speak/", views.speak, name="speak"),
    path("motor/", views.motor, name="motor"),
    path("motor/pending/", views.motor_pending, name="motor_pending"),
    path("look/", views.look, name="look"),
    path("camera/frame/", views.camera_frame, name="camera_frame"),
    path("telemetry/", views.telemetry, name="telemetry"),
    path("snapshot/", views.snapshot_upload, name="snapshot_upload"),
    path("commands/pending/", views.commands_pending, name="commands_pending"),
    path("commands/ack/", views.commands_ack, name="commands_ack"),
    path("sync/pull/", views.sync_pull, name="sync_pull"),
    path("sync/push/", views.sync_push, name="sync_push"),
    path("procurement-signals/", views.procurement_signals, name="procurement_signals"),
]
