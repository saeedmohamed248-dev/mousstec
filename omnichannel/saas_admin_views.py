"""
Super-admin management for the Omnichannel AI Automation add-on.

Mirrors the OBD add-on tooling (clients/views/saas_admin_views.py): a list of
tenants with their subscription state, plus grant / extend / revoke actions.
Runs in the PUBLIC schema only, gated by `saas_admin_required`.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages
from django.utils import timezone

from clients.models import Client
from clients.views.saas_admin_views import saas_admin_required, _log_event

from .models import ChannelMessageLog, TenantChannelConfig

logger = logging.getLogger("mouss_tec_core")

_DURATIONS = {
    "1w": timedelta(days=7),
    "1m": timedelta(days=30),
    "3m": timedelta(days=90),
    "6m": timedelta(days=180),
    "1y": timedelta(days=365),
    "lifetime": None,
}


@saas_admin_required
def subscription_list(request):
    """List tenants and their Omnichannel subscription state, with quick filters."""
    show = request.GET.get("show", "all")  # all | active | inactive | expiring
    now = timezone.now()

    tenants = list(
        Client.objects.exclude(schema_name="public")
        .filter(is_deleted=False)
        .order_by("name")
    )
    configs = {
        c.tenant_id: c
        for c in TenantChannelConfig.objects.select_related("tenant")
    }

    rows = []
    for t in tenants:
        cfg = configs.get(t.id)
        state = cfg.subscription_state if cfg else "inactive"
        rows.append({"tenant": t, "config": cfg, "state": state})

    if show == "active":
        rows = [r for r in rows if r["state"] in ("active", "lifetime")]
    elif show == "inactive":
        rows = [r for r in rows if r["state"] in ("inactive", "expired")]
    elif show == "expiring":
        soon = now + timedelta(days=7)
        rows = [
            r for r in rows
            if r["config"] and r["config"].subscription_expires_at
            and now < r["config"].subscription_expires_at <= soon
        ]

    counts = {
        "total": len(tenants),
        "active": sum(1 for r in rows if r["state"] in ("active", "lifetime")),
    }
    return render(request, "omnichannel/saas_admin/subscription_list.html", {
        "rows": rows,
        "show": show,
        "now": now,
        "counts": counts,
        "price": TenantChannelConfig.MONTHLY_PRICE,
    })


@saas_admin_required
def subscription_grant(request, tenant_id):
    """Grant or extend the subscription for a tenant. POST['duration']."""
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    tenant = get_object_or_404(Client.objects.exclude(schema_name="public"), pk=tenant_id)

    key = (request.POST.get("duration") or "").strip()
    if key not in _DURATIONS:
        messages.error(request, f"🛑 المدة غير معروفة: {key}")
        return redirect("saas_omnichannel_list")

    delta = _DURATIONS[key]
    config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)
    config.grant_subscription(delta, by_user=request.user)

    label = "مدى الحياة" if delta is None else key
    logger.warning(
        "OMNICHANNEL subscription GRANTED tenant=%s duration=%s by=%s",
        tenant.schema_name, label, request.user.username,
    )
    _log_event(
        "other", tenant=tenant, user=request.user,
        description=f"🎁 منح اشتراك أتمتة القنوات لـ «{tenant.name}» لمدة {label}",
    )
    messages.success(request, f"✅ تم منح اشتراك الأتمتة لـ «{tenant.name}» ({label}).")
    return redirect("saas_omnichannel_list")


@saas_admin_required
def subscription_revoke(request, tenant_id):
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    tenant = get_object_or_404(Client.objects.exclude(schema_name="public"), pk=tenant_id)

    config = TenantChannelConfig.objects.filter(tenant=tenant).first()
    if config:
        config.revoke_subscription(by_user=request.user)
    logger.warning(
        "OMNICHANNEL subscription REVOKED tenant=%s by=%s",
        tenant.schema_name, request.user.username,
    )
    _log_event(
        "suspension", tenant=tenant, user=request.user,
        description=f"سحب اشتراك أتمتة القنوات من «{tenant.name}»",
    )
    messages.success(request, f"تم سحب اشتراك الأتمتة من «{tenant.name}».")
    return redirect("saas_omnichannel_list")
