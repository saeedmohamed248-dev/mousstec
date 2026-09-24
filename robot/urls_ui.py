"""robot/urls_ui.py — staff dashboard/profile pages, mounted at /robot/."""

from django.urls import path

from . import views_ui

app_name = "robot_ui"

urlpatterns = [
    path("", views_ui.dashboard, name="dashboard"),
    path("device/<int:pk>/", views_ui.device_profile, name="device_profile"),
    path("device/<int:pk>/control/", views_ui.device_control, name="device_control"),
    path("device/<int:pk>/frame/", views_ui.live_frame, name="live_frame"),
    path("device/<int:pk>/mjpeg/", views_ui.live_mjpeg, name="live_mjpeg"),
    path("device/<int:pk>/faces/", views_ui.face_enrollment, name="face_enrollment"),
    path("device/<int:pk>/page/", views_ui.page_employee, name="page_employee"),
    path("stock-take/<int:pk>/apply/", views_ui.apply_stock_take, name="apply_stock_take"),
    path("teach/", views_ui.teach, name="teach"),
    path("signal/<int:pk>/resolve/", views_ui.resolve_signal, name="resolve_signal"),
    path("alerts/", views_ui.alerts, name="alerts"),
    path("customers/", views_ui.customers, name="customers"),
]
