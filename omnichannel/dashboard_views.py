"""
Tenant dashboard screens for the Omnichannel add-on:
  • settings screen  — connect Meta credentials + tune AI behaviour
  • onboarding guide — the step-by-step Arabic tutorial (Deliverable 3)

Both run in a tenant request context (connection.tenant is the current Client).
The config row lives in the public schema but is reachable from the tenant
schema because django-tenants keeps `public` on the search_path.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.shortcuts import redirect, render
from django.urls import reverse

from .forms import TenantChannelConfigForm
from .models import ChannelMessageLog, TenantChannelConfig

logger = logging.getLogger("mouss_tec_core")


def _current_tenant():
    tenant = getattr(connection, "tenant", None)
    # public schema has no real tenant Client row we want to edit here
    if tenant is None or getattr(tenant, "schema_name", "public") == "public":
        return None
    return tenant


def _webhook_url(request) -> str:
    path = reverse("omnichannel_webhook")
    return request.build_absolute_uri(path)


@login_required
def settings_screen(request):
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")

    config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)

    if request.method == "POST":
        form = TenantChannelConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ إعدادات الأتمتة بنجاح ✅")
            return redirect("omnichannel_settings")
        messages.error(request, "برجاء مراجعة الحقول المميّزة بالأحمر.")
    else:
        form = TenantChannelConfigForm(instance=config)

    recent_logs = ChannelMessageLog.objects.filter(tenant=tenant)[:20]

    context = {
        "form": form,
        "config": config,
        "webhook_url": _webhook_url(request),
        "platform_verify_token": getattr(settings, "OMNICHANNEL_VERIFY_TOKEN", ""),
        "recent_logs": recent_logs,
        "guide_url": reverse("omnichannel_guide"),
    }
    return render(request, "omnichannel/settings.html", context)


@login_required
def onboarding_guide(request):
    context = {
        "webhook_url": _webhook_url(request),
        "platform_verify_token": getattr(settings, "OMNICHANNEL_VERIFY_TOKEN", ""),
        "settings_url": reverse("omnichannel_settings"),
    }
    return render(request, "omnichannel/onboarding_guide.html", context)
