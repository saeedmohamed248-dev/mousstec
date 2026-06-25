from django.urls import include, path

from . import views_ui

app_name = "bmw_ecu"

urlpatterns = [
    path("api/ecu/", include("bmw_ecu.api.urls")),
    # Super-admin gift engine — top-level /api/admin/ per the product spec.
    path("api/admin/", include("bmw_ecu.api.admin_urls")),

    # Coding Room HTML page (session-auth, technician/customer-facing).
    path("bmw-ecu/coding-room/", views_ui.coding_room, name="coding_room"),
]
