"""
Omnichannel Console — a dedicated, feature-focused control center for tenants
subscribed to the add-on. Professional SaaS-style dashboard: overview KPIs, a
conversations inbox, per-contact threads, and a contacts list.

All views require: login + a real tenant + a VALID subscription. Tenants without
an active subscription are redirected to the public/overview page to subscribe —
so the console is "this feature only", fully self-contained, and never exposes
unrelated ERP modules.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .dashboard_views import _current_tenant
from .models import ChannelMessageLog, TenantChannelConfig

logger = logging.getLogger("mouss_tec_core")


def _console_guard(view):
    """login + tenant + active subscription, else redirect to the overview page."""
    @wraps(view)
    @login_required
    def _wrapped(request, *args, **kwargs):
        tenant = _current_tenant()
        if tenant is None:
            messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
            return redirect("/")
        config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)
        if not config.subscription_is_valid:
            messages.info(request, "اشترك في أتمتة القنوات للوصول إلى لوحة التحكم.")
            return redirect("omnichannel_overview")
        request.omni_tenant = tenant
        request.omni_config = config
        return view(request, *args, **kwargs)
    return _wrapped


def _nav(active: str, config) -> dict:
    return {
        "active": active,
        "config": config,
        "nav_home": reverse("omnichannel_console"),
        "nav_inbox": reverse("omnichannel_console_inbox"),
        "nav_contacts": reverse("omnichannel_console_contacts"),
        "nav_settings": reverse("omnichannel_settings"),
        "nav_guide": reverse("omnichannel_guide"),
        "nav_overview": reverse("omnichannel_overview"),
        "standalone": bool(getattr(config, "standalone_mode", False)),
    }


def _conversations(tenant, *, limit=None, search=""):
    """Return conversations (latest message per channel+sender) with counts."""
    base = ChannelMessageLog.objects.filter(tenant=tenant)
    if search:
        base = base.filter(
            Q(sender_id__icontains=search) | Q(contact_name__icontains=search)
        )

    # Accurate message counts per conversation (1 query).
    counts = {
        (r["channel"], r["sender_id"]): r["c"]
        for r in base.values("channel", "sender_id").annotate(c=Count("id"))
    }
    # Latest message per conversation via Postgres DISTINCT ON (1 query).
    latest = (
        base.order_by("channel", "sender_id", "-created_at")
        .distinct("channel", "sender_id")
    )
    convs = []
    for row in latest:
        key = (row.channel, row.sender_id)
        convs.append({
            "channel": row.channel,
            "sender_id": row.sender_id,
            "name": row.contact_name or row.sender_id,
            "last_text": row.inbound_text or row.outbound_text,
            "last_at": row.created_at,
            "status": row.status,
            "count": counts.get(key, 1),
        })
    convs.sort(key=lambda c: c["last_at"], reverse=True)
    return convs[:limit] if limit else convs


# ──────────────────────────────────────────────────────────────────────
@_console_guard
def console_home(request):
    tenant = request.omni_tenant
    config = request.omni_config
    logs = ChannelMessageLog.objects.filter(tenant=tenant)

    now = timezone.now()
    last_7 = now - timedelta(days=7)
    convs = _conversations(tenant)
    kpis = {
        "conversations": len(convs),
        "messages_total": logs.count(),
        "replied": logs.filter(status=ChannelMessageLog.Status.REPLIED).count(),
        "last7": logs.filter(created_at__gte=last_7).count(),
    }
    ctx = _nav("home", config)
    ctx.update({
        "tenant": tenant,
        "kpis": kpis,
        "recent": convs[:8],
        "sub_state": config.subscription_state,
        "days_left": config.subscription_days_left,
    })
    return render(request, "omnichannel/console/home.html", ctx)


@_console_guard
def console_inbox(request):
    tenant = request.omni_tenant
    config = request.omni_config
    search = (request.GET.get("q") or "").strip()
    convs = _conversations(tenant, search=search)
    ctx = _nav("inbox", config)
    ctx.update({"tenant": tenant, "conversations": convs, "search": search})
    return render(request, "omnichannel/console/inbox.html", ctx)


@_console_guard
def console_conversation(request, channel, sender_id):
    tenant = request.omni_tenant
    config = request.omni_config
    msgs = ChannelMessageLog.objects.filter(
        tenant=tenant, channel=channel, sender_id=sender_id,
    ).order_by("created_at")
    name = ""
    for m in msgs:
        if m.contact_name:
            name = m.contact_name
            break
    ctx = _nav("inbox", config)
    ctx.update({
        "tenant": tenant,
        "messages": msgs,
        "contact_name": name or sender_id,
        "sender_id": sender_id,
        "channel": channel,
    })
    return render(request, "omnichannel/console/conversation.html", ctx)


@_console_guard
def console_contacts(request):
    tenant = request.omni_tenant
    config = request.omni_config
    search = (request.GET.get("q") or "").strip()
    convs = _conversations(tenant, search=search)  # one row per contact already
    ctx = _nav("contacts", config)
    ctx.update({"tenant": tenant, "contacts": convs, "search": search})
    return render(request, "omnichannel/console/contacts.html", ctx)
