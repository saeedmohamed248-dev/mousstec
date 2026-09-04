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

import csv
import logging
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

from decimal import Decimal

from django.db import transaction
from django.db.models import F

from .dashboard_views import _current_tenant
from .models import ChannelMessageLog, TenantChannelConfig, TenantChannelNumber
from .services.routing import (
    CHANNEL_INSTAGRAM,
    CHANNEL_MESSENGER,
    CHANNEL_WEBSITE,
    CHANNEL_WHATSAPP,
)

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
        "nav_test": reverse("omnichannel_console_test"),
        "nav_numbers": reverse("omnichannel_console_numbers"),
        "nav_settings": reverse("omnichannel_settings"),
        "nav_guide": reverse("omnichannel_guide"),
        "nav_overview": reverse("omnichannel_overview"),
        "standalone": bool(getattr(config, "standalone_mode", False)),
    }


def _daily_series(tenant, days=14):
    """Return [(label, count), ...] of messages per day for the last `days`."""
    start = (timezone.now() - timedelta(days=days - 1)).date()
    rows = (
        ChannelMessageLog.objects.filter(tenant=tenant, created_at__date__gte=start)
        .annotate(day=TruncDate("created_at")).values("day").annotate(c=Count("id"))
    )
    by_day = {r["day"]: r["c"] for r in rows}
    series = []
    for i in range(days):
        d = start + timedelta(days=i)
        series.append({"label": d.strftime("%m-%d"), "count": by_day.get(d, 0)})
    return series


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
    total = logs.count()
    replied = logs.filter(status=ChannelMessageLog.Status.REPLIED).count()
    kpis = {
        "conversations": len(convs),
        "messages_total": total,
        "replied": replied,
        "last7": logs.filter(created_at__gte=last_7).count(),
        "response_rate": round((replied / total) * 100) if total else 0,
    }
    series = _daily_series(tenant, days=14)
    max_count = max((p["count"] for p in series), default=0) or 1
    ctx = _nav("home", config)
    ctx.update({
        "tenant": tenant,
        "kpis": kpis,
        "recent": convs[:8],
        "series": series,
        "series_max": max_count,
        "sub_state": config.subscription_state,
        "days_left": config.subscription_days_left,
    })
    return render(request, "omnichannel/console/home.html", ctx)


# ── CSV export ─────────────────────────────────────────────────────────
def _csv_response(filename):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.write("﻿")  # BOM so Excel reads Arabic correctly
    return resp


@_console_guard
def console_export_contacts(request):
    tenant = request.omni_tenant
    resp = _csv_response("omnichannel_contacts.csv")
    w = csv.writer(resp)
    w.writerow(["الاسم", "القناة", "المعرّف", "عدد الرسائل", "آخر تواصل"])
    for c in _conversations(tenant):
        w.writerow([c["name"], c["channel"], c["sender_id"], c["count"],
                    c["last_at"].strftime("%Y-%m-%d %H:%M")])
    return resp


@_console_guard
def console_export_conversations(request):
    tenant = request.omni_tenant
    resp = _csv_response("omnichannel_messages.csv")
    w = csv.writer(resp)
    w.writerow(["الوقت", "القناة", "العميل", "المعرّف", "رسالة العميل", "رد المساعد", "الحالة"])
    for m in (ChannelMessageLog.objects.filter(tenant=tenant)
              .order_by("-created_at")[:5000]):
        w.writerow([m.created_at.strftime("%Y-%m-%d %H:%M"), m.channel,
                    m.contact_name or "", m.sender_id, m.inbound_text,
                    m.outbound_text, m.get_status_display()])
    return resp


# ── Live "test the assistant" ──────────────────────────────────────────
@_console_guard
def console_test(request):
    tenant = request.omni_tenant
    config = request.omni_config
    ctx = _nav("test", config)
    ctx["tenant"] = tenant
    sample = "السلام عليكم، عايز أعرف الأسعار المتوفرة عندكم"

    if request.method == "POST":
        from .services.inventory_context import build_catalog_context
        from .services.llm import generate_reply

        message = (request.POST.get("message") or sample).strip()
        currency = ""
        try:
            currency = tenant.effective_currency
        except Exception:
            pass
        catalog = ""
        try:
            with schema_context(tenant.schema_name):
                catalog = build_catalog_context(message, currency=currency)
        except Exception as exc:
            logger.warning("omnichannel test: catalog read failed: %s", exc)
        reply = generate_reply(config, message, catalog) or config.fallback_message
        ctx.update({"message": message, "reply": reply, "catalog": catalog})
    else:
        ctx["message"] = sample

    return render(request, "omnichannel/console/test.html", ctx)


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
def console_numbers(request):
    """Manage additional WhatsApp numbers / Messenger pages, and buy capacity."""
    tenant = request.omni_tenant
    config = request.omni_config

    if request.method == "POST":
        used = config.numbers_used
        if used >= config.number_capacity:
            messages.error(request, "وصلت الحد الأقصى لعدد الأرقام في باقتك — اشترِ باقة أرقام إضافية.")
            return redirect("omnichannel_console_numbers")
        num = TenantChannelNumber(
            config=config,
            label=(request.POST.get("label") or "").strip(),
            channel=request.POST.get("channel") or "whatsapp",
            whatsapp_phone_number_id=(request.POST.get("whatsapp_phone_number_id") or "").strip(),
            whatsapp_business_account_id=(request.POST.get("whatsapp_business_account_id") or "").strip(),
            facebook_page_id=(request.POST.get("facebook_page_id") or "").strip(),
            instagram_account_id=(request.POST.get("instagram_account_id") or "").strip(),
        )
        num.meta_access_token = (request.POST.get("meta_access_token") or "").strip()
        num.app_secret = (request.POST.get("app_secret") or "").strip()
        num.save()
        messages.success(request, "تمت إضافة الرقم/الصفحة ✅")
        return redirect("omnichannel_console_numbers")

    country = getattr(tenant, "country", "EG")
    packages = [
        {"extra": 2, "price": TenantChannelConfig.number_package_price(2, country)},
        {"extra": 4, "price": TenantChannelConfig.number_package_price(4, country)},
    ]
    ctx = _nav("numbers", config)
    ctx.update({
        "tenant": tenant,
        "numbers": config.extra_channel_numbers.all().order_by("-created_at"),
        "capacity": config.number_capacity,
        "used": config.numbers_used,
        "remaining": max(config.number_capacity - config.numbers_used, 0),
        "currency": _tenant_currency(tenant),
        "packages": packages,
        "buy_url": reverse("omnichannel_console_buy_numbers"),
    })
    return render(request, "omnichannel/console/numbers.html", ctx)


@_console_guard
def console_number_delete(request, pk):
    if request.method == "POST":
        TenantChannelNumber.objects.filter(pk=pk, config=request.omni_config).delete()
        messages.success(request, "تم حذف الرقم.")
    return redirect("omnichannel_console_numbers")


@_console_guard
def console_buy_numbers(request):
    """Buy an additional-numbers package (wallet debit), region-priced."""
    tenant = request.omni_tenant
    config = request.omni_config
    if request.method != "POST":
        return redirect("omnichannel_console_numbers")
    if not request.user.is_superuser:
        messages.error(request, "فقط المدير المسؤول عن الحساب يمكنه شراء الباقات.")
        return redirect("omnichannel_console_numbers")

    try:
        extra = int(request.POST.get("extra") or 0)
    except ValueError:
        extra = 0
    if extra not in TenantChannelConfig.NUMBER_PACKAGES:
        messages.error(request, "باقة غير معروفة.")
        return redirect("omnichannel_console_numbers")

    country = getattr(tenant, "country", "EG")
    price = TenantChannelConfig.number_package_price(extra, country)
    try:
        with transaction.atomic():
            from clients.models import Client, EscrowLedger
            locked = Client.objects.select_for_update().get(pk=tenant.pk)
            if locked.wallet_balance < price:
                messages.error(
                    request,
                    f"رصيد محفظتك ({locked.wallet_balance} {_tenant_currency(tenant)}) لا يكفي "
                    f"لشراء الباقة ({price} {_tenant_currency(tenant)}). برجاء شحن المحفظة.")
                return redirect("omnichannel_console_numbers")
            Client.objects.filter(pk=locked.pk).update(wallet_balance=F("wallet_balance") - price)
            EscrowLedger.objects.create(
                client=locked, transaction_type="fee_deduction", amount=price,
                description=f"باقة أرقام إضافية (Omnichannel) — {extra} أرقام ({price} {_tenant_currency(tenant)})")
            config.extra_numbers = extra
            config.save(update_fields=["extra_numbers", "updated_at"])
    except Exception as exc:
        logger.exception("omnichannel: buy numbers failed for %s: %s", tenant.schema_name, exc)
        messages.error(request, "تعذّر شراء الباقة. حاول مرة أخرى أو تواصل مع الدعم.")
        return redirect("omnichannel_console_numbers")

    messages.success(request, f"تم تفعيل باقة {extra} أرقام إضافية ✅ يمكنك الآن إضافة أرقامك.")
    return redirect("omnichannel_console_numbers")


def _tenant_currency(tenant) -> str:
    try:
        return tenant.effective_currency
    except Exception:
        return "ج.م"


@_console_guard
def console_reply(request, channel, sender_id):
    """Human takeover: send a manual reply to a customer via the tenant's Meta token."""
    tenant = request.omni_tenant
    config = request.omni_config
    back = reverse("omnichannel_console_conversation", kwargs={"channel": channel, "sender_id": sender_id})

    if request.method != "POST":
        return redirect(back)
    text = (request.POST.get("text") or "").strip()
    if not text:
        messages.error(request, "اكتب رسالة أولاً.")
        return redirect(back)

    token = config.meta_access_token
    if not token:
        messages.error(request, "لا يمكن الإرسال — لم يتم ربط حساب Meta بعد. أكمل الإعدادات.")
        return redirect(back)

    from .services import meta_api

    error = ""
    try:
        if channel == CHANNEL_WHATSAPP:
            meta_api.send_whatsapp_text(
                access_token=token, phone_number_id=config.whatsapp_phone_number_id,
                recipient_id=sender_id, text=text)
        elif channel in (CHANNEL_MESSENGER, CHANNEL_INSTAGRAM):
            meta_api.send_messenger_text(access_token=token, recipient_id=sender_id, text=text)
        elif channel == CHANNEL_WEBSITE:
            messages.error(request, "لا يمكن الرد يدوياً على شات الموقع (رد فوري تلقائي فقط).")
            return redirect(back)
        else:
            messages.error(request, "قناة غير معروفة.")
            return redirect(back)
    except Exception as exc:
        error = str(exc)
        logger.warning("omnichannel manual reply failed for %s: %s", tenant.schema_name, exc)

    ChannelMessageLog.objects.create(
        tenant=tenant, channel=channel, sender_id=sender_id,
        outbound_text=text, is_human=True,
        status=(ChannelMessageLog.Status.FAILED if error else ChannelMessageLog.Status.REPLIED),
        error=error,
    )
    if error:
        messages.error(request, f"تعذّر الإرسال: {error} — قد يكون خارج نافذة 24 ساعة المسموح بها من واتساب.")
    else:
        messages.success(request, "تم إرسال ردك للعميل ✅")
    return redirect(back)


@_console_guard
def console_contacts(request):
    tenant = request.omni_tenant
    config = request.omni_config
    search = (request.GET.get("q") or "").strip()
    convs = _conversations(tenant, search=search)  # one row per contact already
    ctx = _nav("contacts", config)
    ctx.update({"tenant": tenant, "contacts": convs, "search": search})
    return render(request, "omnichannel/console/contacts.html", ctx)
