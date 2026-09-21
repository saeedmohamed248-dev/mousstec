"""robot/urls_ui.py — staff dashboard/profile pages, mounted at /robot/."""

from django.urls import path

from . import views_ui

app_name = "robot_ui"

urlpatterns = [
    path("", views_ui.dashboard, name="dashboard"),
    path("device/<int:pk>/", views_ui.device_profile, name="device_profile"),
    path("stock-take/<int:pk>/apply/", views_ui.apply_stock_take, name="apply_stock_take"),
]
