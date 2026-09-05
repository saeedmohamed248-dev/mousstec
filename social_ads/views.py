"""
Tenant dashboard screens for the Social Studio add-on:
  • overview       — feature page + subscription status + subscribe CTAs
  • subscribe      — wallet debit (250 EGP → 30 days)
  • pay_with_card  — Paymob card checkout
  • paymob_callback— server-to-server activation (HMAC-verified)
  • settings       — connect Meta + brand profile + autopilot tuning
  • guide          — Arabic onboarding tutorial
  • public         — no-login marketing page

Subscription plumbing mirrors the omnichannel add-on exactly (same wallet ledger,
same Paymob helpers) so billing behaves identically across add-ons.
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
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_tenants.utils import schema_context

from .forms import SocialAdsConfigForm
from .models import SocialAdsConfig

logger = logging.getLogger("mouss_tec_core")

_SUBSCRIPTION_PERIOD = timedelta(days=30)


def _current_tenant():
    tenant = getattr(connection, "tenant", None)
    if tenant is None or getattr(tenant, "schema_name", "public") == "public":
        return None
    return tenant


def _currency(tenant) -> str:
    try:
        return tenant.effective_currency
    except Exception:
        return "ج.م"


def _region_country(request) -> str:
    try:
        from erp_core.regions import region_from_request
        return region_from_request(request).get("country", "EG")
    except Exception:
        return "EG"


# =====================================================================
# Overview + subscribe
# =====================================================================
@login_required
def overview(request):
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")

    config, _ = SocialAdsConfig.objects.get_or_create(tenant=tenant)
    context = {
        "config": config,
        "tenant": tenant,
        "price": SocialAdsConfig.price_for_country(getattr(tenant, "country", "EG")),
        "wallet_balance": getattr(tenant, "wallet_balance", Decimal("0")),
        "currency": _currency(tenant),
        "settings_url": reverse("social_ads_settings"),
        "guide_url": reverse("social_ads_guide"),
        "subscribe_url": reverse("social_ads_subscribe"),
        "pay_url": reverse("social_ads_pay"),
        "studio_url": reverse("social_ads_studio"),
        "can_manage": request.user.is_superuser,
    }
    return render(request, "social_ads/overview.html", context)


@login_required
@require_POST
def subscribe(request):
    """Charge one month from the tenant wallet and grant 30 days."""
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")
    if not request.user.is_superuser:
        messages.error(request, "فقط المدير المسؤول عن الحساب يمكنه تفعيل الاشتراك.")
        return redirect("social_ads_overview")

    price = SocialAdsConfig.price_for_country(getattr(tenant, "country", "EG"))
    config, _ = SocialAdsConfig.objects.get_or_create(tenant=tenant)

    try:
        with transaction.atomic():
            from clients.models import Client, EscrowLedger

            locked = Client.objects.select_for_update().get(pk=tenant.pk)
            if locked.wallet_balance < price:
                messages.error(
                    request,
                    f"رصيد محفظتك ({locked.wallet_balance} {_currency(tenant)}) لا يكفي "
                    f"لتفعيل الاشتراك ({price} {_currency(tenant)}). برجاء شحن المحفظة أولاً.",
                )
                return redirect("social_ads_overview")

            Client.objects.filter(pk=locked.pk).update(wallet_balance=F("wallet_balance") - price)
            EscrowLedger.objects.create(
                client=locked,
                transaction_type="fee_deduction",
                amount=price,
                description=f"اشتراك استوديو التسويق (Social Studio) — شهر ({price} {_currency(tenant)})",
            )
            config.grant_subscription(_SUBSCRIPTION_PERIOD, by_user=request.user)
    except Exception as exc:
        logger.exception("social_ads: subscribe failed for %s: %s", tenant.schema_name, exc)
        messages.error(request, "تعذّر تفعيل الاشتراك. برجاء المحاولة مرة أخرى أو التواصل مع الدعم.")
        return redirect("social_ads_overview")

    messages.success(
        request,
        "تم تفعيل اشتراك استوديو التسويق لمدة 30 يوماً ✅ اربط صفحتك الآن من شاشة الإعدادات.",
    )
    return redirect("social_ads_settings")


@login_required
@require_POST
def pay_with_card(request):
    """Start a Paymob card checkout for one month of the add-on."""
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")
    if not request.user.is_superuser:
        messages.error(request, "فقط المدير المسؤول عن الحساب يمكنه تفعيل الاشتراك.")
        return redirect("social_ads_overview")

    SocialAdsConfig.objects.get_or_create(tenant=tenant)
    price = SocialAdsConfig.price_for_country(getattr(tenant, "country", "EG"))
    callback_url = request.build_absolute_uri(reverse("social_ads_paymob_callback"))
    try:
        from clients.services.paymob import create_iframe_url

        iframe_url = create_iframe_url(
            amount_egp=price,
            customer_phone=getattr(tenant, "phone", "") or "",
            customer_name=getattr(tenant, "name", "") or "",
            customer_email=getattr(tenant, "email", "") or "",
            order_ref=f"social-{tenant.pk}",
            callback_url=callback_url,
            item_name="Mouss Tec Social Studio (1 month)",
            metadata={"client_pk": tenant.pk, "kind": "social_ads_sub"},
            cache_key_prefix="paymob_social",
        )
    except Exception as exc:
        logger.error("social_ads: paymob iframe failed for %s: %s", tenant.schema_name, exc)
        messages.error(request, f"تعذّر فتح بوابة الدفع: {exc}")
        return redirect("social_ads_overview")
    return redirect(iframe_url)


@csrf_exempt
def paymob_callback(request):
    """Server-to-server payment confirmation from Paymob → activate 30 days."""
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    from clients.services.paymob import verify_paymob_hmac

    ok, reason = verify_paymob_hmac(request, body_data=data)
    if not ok:
        logger.warning("social_ads: paymob callback HMAC failed: %s", reason)
        return JsonResponse({"error": "hmac_failed", "reason": reason}, status=403)

    obj = data.get("obj", {}) or {}
    if not obj.get("success"):
        return JsonResponse({"status": "ignored"})

    order_id = str((obj.get("order", {}) or {}).get("id") or "")
    metadata = cache.get(f"paymob_social_{order_id}") or {}
    if not metadata:
        metadata = (obj.get("payment_key_claims", {}) or {}).get("extra", {}) or {}
    if metadata.get("kind") != "social_ads_sub":
        return JsonResponse({"status": "ignored"})
    client_pk = metadata.get("client_pk")
    if not client_pk:
        logger.error("social_ads: paymob callback missing client_pk — %s", metadata)
        return JsonResponse({"error": "metadata"}, status=400)

    paymob_id = obj.get("id", "")
    guard = f"social_paymob_{paymob_id}"
    if paymob_id and cache.get(guard):
        return JsonResponse({"status": "duplicate"})

    with schema_context("public"):
        config, _ = SocialAdsConfig.objects.get_or_create(tenant_id=client_pk)
        config.grant_subscription(_SUBSCRIPTION_PERIOD)

    if paymob_id:
        cache.set(guard, "processed", timeout=86400)
    logger.info("social_ads: paymob subscription activated client=%s paymob_id=%s", client_pk, paymob_id)
    return JsonResponse({"status": "ok"})


# =====================================================================
# Settings + guide + public
# =====================================================================
@login_required
def settings_screen(request):
    tenant = _current_tenant()
    if tenant is None:
        messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
        return redirect("/")

    config, _ = SocialAdsConfig.objects.get_or_create(tenant=tenant)
    if request.method == "POST":
        form = SocialAdsConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ إعدادات الاستوديو بنجاح ✅")
            return redirect("social_ads_settings")
        messages.error(request, "برجاء مراجعة الحقول المميّزة بالأحمر.")
    else:
        form = SocialAdsConfigForm(instance=config)

    context = {
        "form": form,
        "config": config,
        "guide_url": reverse("social_ads_guide"),
        "overview_url": reverse("social_ads_overview"),
        "studio_url": reverse("social_ads_studio"),
    }
    return render(request, "social_ads/settings.html", context)


@login_required
def onboarding_guide(request):
    context = {
        "settings_url": reverse("social_ads_settings"),
        "overview_url": reverse("social_ads_overview"),
        "graph_version": getattr(settings, "META_GRAPH_VERSION", "v19.0"),
    }
    return render(request, "social_ads/guide.html", context)


def public_page(request):
    tenant = _current_tenant()
    country = _region_country(request)
    context = {
        "price": SocialAdsConfig.price_for_country(country),
        "country": country,
        "login_url": "/login/?next=/social-studio/",
        "signup_url": reverse("saas_customer_signup"),
        "is_logged_in_tenant": tenant is not None and request.user.is_authenticated,
        "overview_url": reverse("social_ads_overview"),
    }
    return render(request, "social_ads/public.html", context)
