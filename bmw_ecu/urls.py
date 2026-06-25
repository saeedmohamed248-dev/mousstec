from django.urls import include, path

app_name = "bmw_ecu"

urlpatterns = [
    path("api/ecu/", include("bmw_ecu.api.urls")),
]
