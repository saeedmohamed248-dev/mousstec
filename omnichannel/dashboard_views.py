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
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import TenantChannelConfigForm
from .models import ChannelMessageLog, TenantChannelConfig

logger = logging.getLogger("mouss_tec_core")

# One self-serve purchase grants 30 days.
_SUBSCRIPTION_PERIOD = timedelta(days=30)


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
        "overview_url": reverse("omnichannel_overview"),
    }
    return render(request, "omnichannel/settings.html", context)


@login_required
def onboarding_guide(request):
    context = {
        "webhook_url": _webhook_url(request),
        "platform_verify_token": getattr(settings, "OMNICHANNEL_VERIFY_TOKEN", ""),
        "settings_url": reverse("omnichannel_settings"),
        "overview_url": reverse("omnichannel_overview"),
    }
    return render(request, "omnichannel/onboarding_guide.html", context)


@login_required
def overview(request):
    """Dedicated landing/overview page for the add-on (feature page + subscribe)."""
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")

    config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)
    context = {
        "config": config,
        "tenant": tenant,
        "price": TenantChannelConfig.MONTHLY_PRICE,
        "wallet_balance": getattr(tenant, "wallet_balance", Decimal("0")),
        "currency": _currency(tenant),
        "settings_url": reverse("omnichannel_settings"),
        "guide_url": reverse("omnichannel_guide"),
        "subscribe_url": reverse("omnichannel_subscribe"),
        "can_manage": request.user.is_superuser,
    }
    return render(request, "omnichannel/overview.html", context)


@login_required
@require_POST
def subscribe(request):
    """Self-serve: charge one month (250 EGP) from the tenant wallet and grant 30 days.

    Wallet is topped up via the platform's existing Paymob flow; here we debit it
    atomically. Only the tenant's own admin (superuser) may purchase.
    """
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")
    if not request.user.is_superuser:
        messages.error(request, "فقط المدير المسؤول عن الحساب يمكنه تفعيل الاشتراك.")
        return redirect("omnichannel_overview")

    price = TenantChannelConfig.MONTHLY_PRICE
    config, _created = TenantChannelConfig.objects.get_or_create(tenant=tenant)

    try:
        with transaction.atomic():
            # Import here to avoid any import-time coupling with the clients app.
            from clients.models import Client, EscrowLedger

            locked = Client.objects.select_for_update().get(pk=tenant.pk)
            if locked.wallet_balance < price:
                messages.error(
                    request,
                    f"رصيد محفظتك ({locked.wallet_balance} {_currency(tenant)}) لا يكفي "
                    f"لتفعيل الاشتراك ({price} {_currency(tenant)}). برجاء شحن المحفظة أولاً.",
                )
                return redirect("omnichannel_overview")

            # Debit the wallet + record an auditable ledger entry.
            Client.objects.filter(pk=locked.pk).update(
                wallet_balance=F("wallet_balance") - price
            )
            EscrowLedger.objects.create(
                client=locked,
                transaction_type="fee_deduction",
                amount=price,
                description=f"اشتراك أتمتة القنوات (Omnichannel) — شهر ({price} {_currency(tenant)})",
            )
            config.grant_subscription(_SUBSCRIPTION_PERIOD, by_user=request.user)
    except Exception as exc:
        logger.exception("omnichannel: subscribe failed for %s: %s", tenant.schema_name, exc)
        messages.error(request, "تعذّر تفعيل الاشتراك. برجاء المحاولة مرة أخرى أو التواصل مع الدعم.")
        return redirect("omnichannel_overview")

    messages.success(
        request,
        "تم تفعيل اشتراك الأتمتة لمدة 30 يوماً ✅ اربط حسابك الآن من شاشة الإعدادات.",
    )
    return redirect("omnichannel_settings")


def _currency(tenant) -> str:
    try:
        return tenant.effective_currency
    except Exception:
        return "ج.م"
