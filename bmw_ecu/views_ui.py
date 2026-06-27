"""Server-side views for the Coding Room HTML page.

Kept separate from `api/views.py` so the JSON chatbot API and the UI page
don't entangle. The page itself is a thin Django template that boots the
vanilla-JS controller in `static/bmw_ecu/coding_room.js`.
"""
from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET


@login_required
@require_GET
def storefront(request):
    """Pricing-page UI — bilingual SaaS cards backed by the granular
    SubscriptionPackage catalog.

    The page bootstraps with no server-rendered package data (the JS
    fetches /api/ecu/storefront/packages/ on load) so a future admin
    edit on a package doesn't require a hard refresh of cached HTML
    fragments. Default language is Arabic; ?lang=en flips to English.
    """
    lang = "en" if request.GET.get("lang") == "en" else "ar"
    return render(request, "bmw_ecu/storefront.html", {"lang": lang})


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


@staff_member_required
@require_GET
def admin_gift_form(request):
    """Mousstec Super-Admin gift issuance UI.

    Renders a form that POSTs to /api/admin/entitlements/gift, plus a
    table of the 20 most-recent gifts with revoke buttons that hit
    /api/admin/entitlements/gift/<pk>/revoke.

    All data is fetched from the PUBLIC schema (where gift rows live
    for cross-tenant super-admin visibility) regardless of the
    requesting subdomain's tenant context.
    """
    from django_tenants.utils import schema_context
    from .models import GiftCredit

    with schema_context("public"):
        # Tenants list — Client is the django_tenants tenant model.
        try:
            from clients.models import Client
            tenants = list(
                Client.objects.exclude(schema_name="public")
                .order_by("schema_name")
                .values("schema_name", "name")[:500]
            )
        except Exception:
            tenants = []

        recent = list(
            GiftCredit.objects.order_by("-granted_at")[:20]
            .values("pk", "tenant_schema", "grant_type", "credits_total",
                    "credits_remaining", "valid_until", "status",
                    "granted_by", "granted_at", "note")
        )

    context = {
        "tenants": tenants,
        "recent_gifts": recent,
    }
    return render(request, "bmw_ecu/admin_gift_form.html", context)
