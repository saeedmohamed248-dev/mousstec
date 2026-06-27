"""Public-facing subscribe endpoint — POST /api/ecu/subscribe/.

Tenant technicians hit this from the Storefront page when they click
"Subscribe via Wallet" on a pricing card. Distinct from the Super-Admin
/api/admin/grants/ endpoint:

  • super_admin grant       : login=is_staff, no payment, grant any package
  • tenant subscribe (here)  : login=any user, charges wallet, only their tenant

Pricing flow:
  1. Look up the package the technician picked (must be is_active=True).
  2. Decide what to charge:
       price_override_egp  > package.price_egp  > 0
     (the override is for promo coupons / sales support, future use)
  3. Atomically deduct from clients.Client.wallet_balance via SELECT FOR
     UPDATE. If wallet < price, return 402 Payment Required with the
     current balance so the UI can render a "top-up first" message.
  4. Inside the same DB transaction, create the TenantPackageGrant. If
     either step raises, both roll back (no half-purchase).
  5. Return the new grant payload — the UI uses it to flip the card to
     "✅ Subscribed".

Audit: every successful purchase emits a PlatformEvent so the Mousstec
management team can see purchases in their existing event timeline. A
failed purchase (insufficient wallet) is logged but NOT eventized,
because pricing-page click-through noise is high.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from ..logging_setup import get_logger

log = get_logger(__name__)


def _coerce_decimal(value, default: Decimal = Decimal("0")) -> Optional[Decimal]:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _current_tenant_schema(request) -> Optional[str]:
    """Resolve which tenant the request was routed to. django-tenants
    sets `request.tenant`; the schema lives at `.schema_name`."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        from django.db import connection
        tenant = getattr(connection, "tenant", None)
    return getattr(tenant, "schema_name", None)


@csrf_exempt
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_active_packages(request: Request) -> Response:
    """Storefront feed — every SubscriptionPackage with is_active=True
    plus the workshop's CURRENT wallet balance so the UI can show
    "Insufficient balance — top up" preemptively."""
    from ..models import SubscriptionPackage

    schema = _current_tenant_schema(request)
    wallet_balance = None
    if schema:
        from clients.models import Client
        c = Client.objects.filter(schema_name=schema).values(
            "wallet_balance").first()
        if c is not None:
            wallet_balance = str(c["wallet_balance"])

    rows = []
    qs = (SubscriptionPackage.objects.filter(is_active=True)
          .prefetch_related("features").order_by("sort_order", "code"))
    for pkg in qs:
        rows.append({
            "pk": pkg.pk,
            "code": pkg.code,
            "name": pkg.name,
            "description": pkg.description,
            "billing_mode": pkg.billing_mode,
            "default_duration_days": pkg.default_duration_days,
            "default_usage_quota": pkg.default_usage_quota,
            "price_egp": str(pkg.price_egp),
            "currency": pkg.currency,
            "is_featured": pkg.is_featured,
            "feature_codes": list(pkg.features.values_list("code", flat=True)),
            "feature_names": list(pkg.features.values_list("name", flat=True)),
        })

    return Response({
        "tenant_schema": schema or "",
        "wallet_balance_egp": wallet_balance,
        "currency": "EGP",
        "results": rows,
    })


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def subscribe(request: Request) -> Response:
    """Atomic wallet → grant flow. Body:
        { "package_code": "pkg_starter" }
    Returns 201 + grant payload on success, 402 on insufficient balance,
    404 on unknown package."""
    from ..models import SubscriptionPackage, TenantPackageGrant
    from clients.models import Client

    schema = _current_tenant_schema(request)
    if not schema or schema == "public":
        return Response(
            {"detail": "Subscribe must be called from a tenant subdomain."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    payload = request.data or {}
    pkg_code = (payload.get("package_code") or "").strip()
    if not pkg_code:
        return Response({"detail": "package_code is required"},
                        status=status.HTTP_400_BAD_REQUEST)

    pkg = (SubscriptionPackage.objects
           .filter(code=pkg_code, is_active=True)
           .prefetch_related("features").first())
    if pkg is None:
        return Response(
            {"detail": f"Unknown or inactive package: {pkg_code!r}"},
            status=status.HTTP_404_NOT_FOUND,
        )

    price = _coerce_decimal(pkg.price_egp)
    if price is None or price < 0:
        return Response({"detail": "Package has invalid price"},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # ── Atomic wallet → grant. If wallet < price OR grant creation
    # fails, both halves roll back — no money disappears, no half-grant.
    try:
        with transaction.atomic():
            client = (Client.objects.select_for_update()
                      .filter(schema_name=schema).first())
            if client is None:
                return Response(
                    {"detail": f"Tenant client row not found: {schema!r}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            wallet_before: Decimal = client.wallet_balance
            if wallet_before < price:
                # Not transactionally meaningful (we didn't mutate yet),
                # but exit the block so the response is outside atomic().
                raise _InsufficientWallet(have=wallet_before, need=price)

            Client.objects.filter(pk=client.pk).update(
                wallet_balance=F("wallet_balance") - price,
            )
            client.refresh_from_db(fields=["wallet_balance"])
            wallet_after = client.wallet_balance

            # Compute valid_until from package defaults.
            valid_until = None
            if pkg.default_duration_days and pkg.default_duration_days > 0:
                valid_until = (timezone.now()
                               + timedelta(days=pkg.default_duration_days))

            grant = TenantPackageGrant.objects.create(
                tenant_schema=schema,
                package=pkg,
                billing_mode=pkg.billing_mode,
                valid_until=valid_until,
                usage_quota=pkg.default_usage_quota or 0,
                price_paid_egp=price,
                granted_by=f"wallet:{request.user.username}",
                note="Self-service subscribe via storefront",
            )

        log.info("Storefront subscribe OK", extra={
            "tenant": schema, "package": pkg.code,
            "user": request.user.username,
            "price_egp": str(price),
            "wallet_before": str(wallet_before),
            "wallet_after": str(wallet_after),
        })

        # Best-effort audit — never block the success response on this.
        try:
            from clients.models import PlatformEvent
            PlatformEvent.objects.create(
                event_type="other", tenant_schema=schema,
                tenant_name=schema, user_name=request.user.username,
                description=(
                    f"💳 اشترك في الباقة «{pkg.name}» ({pkg.code}) "
                    f"بمبلغ {price} EGP. الرصيد بعد الاشتراك: {wallet_after}."
                ),
            )
        except Exception:
            log.debug("PlatformEvent write skipped", exc_info=True)

        return Response({
            "ok": True,
            "grant_pk": grant.pk,
            "package_code": pkg.code,
            "package_name": pkg.name,
            "billing_mode": grant.billing_mode,
            "valid_until": (grant.valid_until.isoformat()
                            if grant.valid_until else None),
            "usage_quota": grant.usage_quota,
            "price_paid_egp": str(price),
            "wallet_after_egp": str(wallet_after),
        }, status=status.HTTP_201_CREATED)

    except _InsufficientWallet as e:
        log.warning("Storefront subscribe insufficient", extra={
            "tenant": schema, "package": pkg.code,
            "user": request.user.username,
            "need": str(e.need), "have": str(e.have),
        })
        return Response({
            "ok": False,
            "error": "insufficient_balance",
            "detail": (f"رصيد المحفظة ({e.have} EGP) أقل من سعر الباقة "
                       f"({e.need} EGP). اشحن المحفظة أولاً ثم حاول مرة أخرى."),
            "wallet_balance_egp": str(e.have),
            "price_egp": str(e.need),
        }, status=status.HTTP_402_PAYMENT_REQUIRED)


class _InsufficientWallet(Exception):
    def __init__(self, *, have: Decimal, need: Decimal) -> None:
        self.have = have
        self.need = need
