from django.urls import path

from . import views

app_name = "bmw_ecu_api"

urlpatterns = [
    path("execute", views.execute, name="execute"),
    path("wizard/step", views.wizard_step, name="wizard_step"),
]
