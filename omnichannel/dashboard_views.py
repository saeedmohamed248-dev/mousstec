"""
Tenant dashboard screens for the Omnichannel add-on:
  • settings screen  — connect Meta credentials + tune AI behaviour
  • onboarding guide — the step-by-step Arabic tutorial (Deliverable 3)

Both run in a tenant request context (connection.tenant is the current Client).
The config row lives in the public schema but is reachable from the tenant
schema because django-tenants keeps `public` on the search_path.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_tenants.utils import schema_context

from .forms import TenantChannelConfigForm
from .models import ChannelMessageLog, TenantChannelConfig

logger = logging.getLogger("mouss_tec_core")

# One purchase grants 30 days.
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


def public_page(request):
    """PUBLIC marketing page for the add-on — no login required.

    Served on the public domain so prospective customers can learn about the
    feature and start subscribing. Actual purchase happens inside a tenant
    account, so the CTAs route to login / signup (and to /omnichannel/ for
    already-logged-in tenant admins).
    """
    tenant = _current_tenant()
    country = _region_country(request)
    context = {
        "price": TenantChannelConfig.price_for_country(country),
        "country": country,
        "free_conversations": 1000,
        "login_url": "/login/?next=/omnichannel/",
        "signup_url": reverse("saas_customer_signup"),
        "is_logged_in_tenant": tenant is not None and request.user.is_authenticated,
        "overview_url": reverse("omnichannel_overview"),
    }
    return render(request, "omnichannel/public.html", context)


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
        "price": TenantChannelConfig.price_for_country(getattr(tenant, "country", "EG")),
        "free_conversations": 1000,
        "wallet_balance": getattr(tenant, "wallet_balance", Decimal("0")),
        "currency": _currency(tenant),
        "settings_url": reverse("omnichannel_settings"),
        "guide_url": reverse("omnichannel_guide"),
        "subscribe_url": reverse("omnichannel_subscribe"),
        "pay_url": reverse("omnichannel_pay"),
        "console_url": reverse("omnichannel_console"),
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

    price = TenantChannelConfig.price_for_country(getattr(tenant, "country", "EG"))
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


def _region_country(request) -> str:
    """Resolve the marketing region (EG/AE) from the request host."""
    try:
        from erp_core.regions import region_from_request
        return region_from_request(request).get("country", "EG")
    except Exception:
        return "EG"


def _currency(tenant) -> str:
    try:
        return tenant.effective_currency
    except Exception:
        return "ج.م"


# =====================================================================
# 💳 Direct card payment via Paymob (in addition to wallet debit)
# =====================================================================
@login_required
@require_POST
def pay_with_card(request):
    """Start a Paymob card checkout for one month of the add-on.

    Mirrors the platform's other Paymob checkouts (diagnostics/parts): build an
    iframe URL with the subscription metadata and redirect. Activation happens
    server-to-server in `paymob_callback` after Paymob confirms payment.
    """
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")
    if not request.user.is_superuser:
        messages.error(request, "فقط المدير المسؤول عن الحساب يمكنه تفعيل الاشتراك.")
        return redirect("omnichannel_overview")

    # Ensure a config row exists so the callback can always find/activate it.
    TenantChannelConfig.objects.get_or_create(tenant=tenant)

    price = TenantChannelConfig.price_for_country(getattr(tenant, "country", "EG"))
    callback_url = request.build_absolute_uri(reverse("omnichannel_paymob_callback"))
    try:
        from clients.services.paymob import create_iframe_url

        iframe_url = create_iframe_url(
            amount_egp=price,
            customer_phone=getattr(tenant, "phone", "") or "",
            customer_name=getattr(tenant, "name", "") or "",
            customer_email=getattr(tenant, "email", "") or "",
            order_ref=f"omni-{tenant.pk}",
            callback_url=callback_url,
            item_name="Mouss Tec Omnichannel AI (1 month)",
            metadata={"client_pk": tenant.pk, "kind": "omnichannel_sub"},
            cache_key_prefix="paymob_omni",
        )
    except Exception as exc:  # RuntimeError with an Arabic message, or network
        logger.error("omnichannel: paymob iframe failed for %s: %s", tenant.schema_name, exc)
        messages.error(request, f"تعذّر فتح بوابة الدفع: {exc}")
        return redirect("omnichannel_overview")

    return redirect(iframe_url)


@csrf_exempt
def paymob_callback(request):
    """Server-to-server payment confirmation from Paymob → activate 30 days.

    HMAC verification is mandatory (fail-closed): without it anyone could grant
    themselves a paid subscription by POSTing a forged success payload.
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    from clients.services.paymob import verify_paymob_hmac

    ok, reason = verify_paymob_hmac(request, body_data=data)
    if not ok:
        logger.warning("omnichannel: paymob callback HMAC failed: %s", reason)
        return JsonResponse({"error": "hmac_failed", "reason": reason}, status=403)

    obj = data.get("obj", {}) or {}
    if not obj.get("success"):
        return JsonResponse({"status": "ignored"})

    # Resolve our metadata. Primary: the cache keyed by the Paymob order id
    # (create_iframe_url stores it under 'paymob_omni_{order_id}'). Fallback:
    # payment_key_claims.extra, if the integration echoes it.
    order_id = str((obj.get("order", {}) or {}).get("id") or "")
    metadata = cache.get(f"paymob_omni_{order_id}") or {}
    if not metadata:
        metadata = (obj.get("payment_key_claims", {}) or {}).get("extra", {}) or {}

    if metadata.get("kind") != "omnichannel_sub":
        # Not ours — another feature's callback. Ack without acting.
        return JsonResponse({"status": "ignored"})
    client_pk = metadata.get("client_pk")
    if not client_pk:
        logger.error("omnichannel: paymob callback missing client_pk — %s", metadata)
        return JsonResponse({"error": "metadata"}, status=400)

    paymob_id = obj.get("id", "")
    # Idempotency — Paymob may retry the callback.
    guard = f"omni_paymob_{paymob_id}"
    if paymob_id and cache.get(guard):
        return JsonResponse({"status": "duplicate"})

    with schema_context("public"):
        config, _created = TenantChannelConfig.objects.get_or_create(tenant_id=client_pk)
        config.grant_subscription(_SUBSCRIPTION_PERIOD)

    if paymob_id:
        cache.set(guard, "processed", timeout=86400)
    logger.info("omnichannel: paymob subscription activated client=%s paymob_id=%s",
                client_pk, paymob_id)
    return JsonResponse({"status": "ok"})
