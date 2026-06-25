"""Server-side views for the Coding Room HTML page.

Kept separate from `api/views.py` so the JSON chatbot API and the UI page
don't entangle. The page itself is a thin Django template that boots the
vanilla-JS controller in `static/bmw_ecu/coding_room.js`.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET


@login_required
@require_GET
def coding_room(request):
    """Render the Coding & Retrofit room.

    Query params:
        vin           — VIN of the car on the bench (optional, can be set in UI).
        profile_name  — ECU profile (default FEM_F30).
        chassis       — F30 / G20 / … (drives initial feature filter).
    """
    context = {
        "vin": request.GET.get("vin", ""),
        "profile_name": request.GET.get("profile_name", "FEM_F30"),
        "chassis": request.GET.get("chassis", "F30"),
    }
    return render(request, "bmw_ecu/coding_room.html", context)
