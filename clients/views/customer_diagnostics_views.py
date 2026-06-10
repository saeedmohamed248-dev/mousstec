"""Customer-facing diagnostics — separate from the workshop (tenant) diag room.

Flow for a car owner:
  /marketplace/diagnostics/         → landing + status + scan launcher
  /marketplace/diagnostics/pricing/ → 3-tier pricing page (99/199/399)
  /marketplace/diagnostics/upgrade/<tier>/ → checkout (Paymob if configured,
                                              else dev-mode auto-activate)
  /marketplace/diagnostics/scan/    → POST, records a scan against the quota
  /marketplace/diagnostics/paymob-callback/ → server-to-server payment confirm

The DiagnosticsQuotaService in smart_diagnostics is tenant-scoped — we keep
customers on their own model (CustomerDiagnosticsSubscription) so the two
worlds never mix.
"""
from __future__ import annotations

import json
import logging
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from clients.models import CustomerDiagnosticsSubscription
from clients.views._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')


def _sub_for(request):
    """Resolve customer + their subscription (creating a trial if missing —
    covers customers who registered before the trial feature shipped)."""
    customer = _marketplace_auth(request)
    if not customer:
        return None, None
    with schema_context('public'):
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        sub.reset_period_if_needed()
    return customer, sub


def _tier_card(tier_key: str) -> dict:
    return {
        'key': tier_key,
        'label': dict(CustomerDiagnosticsSubscription.TIER_CHOICES).get(tier_key, tier_key),
        'price_egp': CustomerDiagnosticsSubscription.TIER_PRICES_EGP.get(tier_key),
        'quota': CustomerDiagnosticsSubscription.TIER_QUOTAS.get(tier_key),
        'features': CustomerDiagnosticsSubscription.TIER_FEATURES.get(tier_key, []),
    }


def diagnostics_landing(request):
    """صفحة التشخيص للعميل — لوحة الحالة + زر الفحص."""
    customer, sub = _sub_for(request)
    if not customer:
        return redirect('/marketplace/automotive/')

    return render(request, 'clients/marketplace/diagnostics_landing.html', {
        'customer': customer,
        'sub': sub,
        'is_active': sub.is_active(),
        'days_remaining': sub.days_remaining(),
        'quota_remaining': sub.quota_remaining(),
        'quota_total': CustomerDiagnosticsSubscription.TIER_QUOTAS.get(sub.tier, 0),
        'features': sub.TIER_FEATURES.get(sub.tier, []),
    })


def diagnostics_pricing(request):
    """صفحة الباقات — 3 شرائح + التجربة المجانية."""
    customer, sub = _sub_for(request)
    tiers = [_tier_card(t) for t in ('basic', 'pro', 'empire')]
    return render(request, 'clients/marketplace/diagnostics_pricing.html', {
        'customer': customer,
        'sub': sub,
        'tiers': tiers,
        'current_tier': sub.tier if sub else None,
        'days_remaining': sub.days_remaining() if sub else 0,
    })


@csrf_exempt
def diagnostics_upgrade(request, tier: str):
    """Start an upgrade. If Paymob is configured we hand off to it; otherwise
    (dev/staging) we activate immediately and log a synthetic ref."""
    customer, sub = _sub_for(request)
    if not customer:
        return JsonResponse({"error": "auth_required"}, status=401)

    if tier not in ('basic', 'pro', 'empire'):
        return JsonResponse({"error": "invalid tier"}, status=400)

    paymob_key = getattr(settings, 'PAYMOB_API_KEY', '')
    if not paymob_key:
        # Dev path — activate immediately so the flow is testable end-to-end.
        synthetic_ref = f"dev-{uuid.uuid4().hex[:16]}"
        with schema_context('public'):
            sub.upgrade(tier, payment_ref=synthetic_ref)
        logger.info(
            "[CUSTOMER DIAG] dev-mode upgrade: customer=%s tier=%s ref=%s",
            customer.pk, tier, synthetic_ref,
        )
        if request.method == 'POST':
            return JsonResponse({
                "status": "activated",
                "tier": tier,
                "redirect": "/marketplace/diagnostics/",
            })
        return redirect('/marketplace/diagnostics/?upgraded=1')

    # Paymob path — defer to the existing parts_checkout integration shape.
    # We surface only the iframe URL; the actual integration_id and billing
    # data is the same shape used elsewhere in this codebase.
    price = CustomerDiagnosticsSubscription.TIER_PRICES_EGP[tier]
    # Lazy import to avoid bootstrapping payment libs on every request.
    try:
        from clients.services.paymob import create_iframe_url  # type: ignore
    except Exception:
        create_iframe_url = None

    if create_iframe_url is None:
        return JsonResponse({
            "error": "payment_unavailable",
            "message": "بوابة الدفع غير متاحة حالياً. تواصل مع الدعم.",
        }, status=503)

    callback_url = request.build_absolute_uri('/marketplace/diagnostics/paymob-callback/')
    iframe_url = create_iframe_url(
        amount_egp=price,
        customer_phone=customer.phone,
        customer_name=customer.full_name,
        order_ref=f"diag-{customer.pk}-{tier}-{uuid.uuid4().hex[:8]}",
        callback_url=callback_url,
        metadata={'customer_pk': customer.pk, 'tier': tier},
    )
    return redirect(iframe_url)


@csrf_exempt
def diagnostics_scan(request):
    """POST {vin?, dtc_codes?} — records one scan against the quota.

    Does NOT execute the actual AI diagnosis here; that's handled by the
    smart_diagnostics service. This endpoint is the gate + counter so the
    business rule (quota per tier) lives in one place.
    """
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)
    customer, sub = _sub_for(request)
    if not customer:
        return JsonResponse({"error": "auth_required"}, status=401)

    ok, reason = sub.can_scan()
    if not ok:
        return JsonResponse({
            "error": "quota_exceeded",
            "reason": reason,
            "upgrade_url": "/marketplace/diagnostics/pricing/",
        }, status=402)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = {}

    sub.record_scan()
    logger.info(
        "[CUSTOMER DIAG] scan recorded: customer=%s tier=%s used=%s/%s",
        customer.pk, sub.tier, sub.scans_used, sub.TIER_QUOTAS.get(sub.tier),
    )

    return JsonResponse({
        "status": "ok",
        "vin": (payload.get('vin') or '').upper()[:17],
        "dtc_codes": payload.get('dtc_codes', []),
        "quota_remaining": sub.quota_remaining(),
        "tier": sub.tier,
    })


@csrf_exempt
def diagnostics_paymob_callback(request):
    """Server-to-server payment confirmation. Paymob posts here; we activate
    the subscription based on metadata."""
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    obj = data.get('obj', {})
    if not obj.get('success'):
        logger.warning("[CUSTOMER DIAG] paymob callback: not success — %s", obj.get('id'))
        return JsonResponse({"status": "ignored"})

    metadata = obj.get('payment_key_claims', {}).get('extra', {}) or {}
    customer_pk = metadata.get('customer_pk')
    tier = metadata.get('tier')
    paymob_id = obj.get('id', '')

    if not customer_pk or tier not in ('basic', 'pro', 'empire'):
        logger.error("[CUSTOMER DIAG] paymob callback: missing metadata — %s", metadata)
        return JsonResponse({"error": "metadata"}, status=400)

    with schema_context('public'):
        try:
            sub = CustomerDiagnosticsSubscription.objects.get(customer_id=customer_pk)
        except CustomerDiagnosticsSubscription.DoesNotExist:
            return JsonResponse({"error": "subscription_not_found"}, status=404)
        sub.upgrade(tier, payment_ref=f"paymob-{paymob_id}")

    logger.info(
        "[CUSTOMER DIAG] paymob upgrade OK: customer=%s tier=%s paymob_id=%s",
        customer_pk, tier, paymob_id,
    )
    return JsonResponse({"status": "ok"})
