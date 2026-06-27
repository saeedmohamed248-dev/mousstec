"""Super-Admin Packages & Grants endpoints — /api/admin/packages/, /grants/.

Lets the Mousstec management team build subscription bundles from the
atomic Feature catalog seeded by migration 0006, set prices, and assign
those packages (or a-la-carte single features) to specific workshops.

Multi-tenant note: SubscriptionPackage + Feature catalog rows are seeded
into every tenant's schema by migration 0006 (bmw_ecu lives in
TENANT_APPS). TenantPackageGrant / TenantFeatureGrant rows for a given
workshop are written *inside that workshop's schema* via schema_context.
That keeps every grant collocated with the consume_feature_usage_sync
queries that decrement it — no cross-schema queries on the hot path.

Endpoints
---------
GET    /api/admin/features/                 — list every Feature
GET    /api/admin/packages/                 — list every SubscriptionPackage
POST   /api/admin/packages/                 — create a new package
GET    /api/admin/packages/<id>/            — one package detail
PATCH  /api/admin/packages/<id>/            — update a package
DELETE /api/admin/packages/<id>/            — soft-deactivate a package
POST   /api/admin/grants/                   — issue a Package or Feature grant
POST   /api/admin/grants/<id>/revoke/       — revoke an active grant
GET    /api/admin/grants/?tenant=<schema>   — list grants for a tenant
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response

from ..logging_setup import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _serialize_feature(f) -> dict:
    return {
        "pk": f.pk,
        "code": f.code,
        "name": f.name,
        "category": f.category,
        "default_operation_type": f.default_operation_type,
        "description": f.description,
        "is_active": f.is_active,
        "sort_order": f.sort_order,
    }


def _serialize_package(p) -> dict:
    return {
        "pk": p.pk,
        "code": p.code,
        "name": p.name,
        "description": p.description,
        "billing_mode": p.billing_mode,
        "default_duration_days": p.default_duration_days,
        "default_usage_quota": p.default_usage_quota,
        "price_egp": str(p.price_egp),
        "currency": p.currency,
        "is_active": p.is_active,
        "is_featured": p.is_featured,
        "sort_order": p.sort_order,
        "feature_codes": list(p.features.values_list("code", flat=True)),
    }


def _serialize_grant(g, *, kind: str) -> dict:
    out = {
        "pk": g.pk,
        "kind": kind,
        "tenant_schema": g.tenant_schema,
        "status": g.status,
        "billing_mode": g.billing_mode,
        "valid_until": g.valid_until.isoformat() if g.valid_until else None,
        "usage_quota": g.usage_quota,
        "usage_used": g.usage_used,
        "usage_remaining": g.usage_remaining(),
        "price_paid_egp": str(g.price_paid_egp),
        "granted_by": g.granted_by,
        "note": g.note,
        "granted_at": g.granted_at.isoformat(),
    }
    if kind == "package":
        out["package_code"] = g.package.code
        out["package_name"] = g.package.name
    else:
        out["feature_code"] = g.feature.code
        out["feature_name"] = g.feature.name
    return out


def _coerce_decimal(value, default: Decimal = Decimal("0")) -> Optional[Decimal]:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _resolve_tenant(tenant_schema: str):
    """Return (Client_or_None, error_response_or_None)."""
    from clients.models import Client
    if not tenant_schema:
        return None, Response(
            {"detail": "tenant_schema is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    tenant = Client.objects.filter(schema_name=tenant_schema).first()
    if tenant is None:
        return None, Response(
            {"detail": f"Unknown tenant_schema: {tenant_schema!r}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return tenant, None


# ---------------------------------------------------------------------------
# Feature catalog — read-only
# ---------------------------------------------------------------------------
@csrf_exempt
@api_view(["GET"])
@permission_classes([IsAdminUser])
def list_features(request: Request) -> Response:
    """List every Feature in the catalog. is_active filter optional."""
    from ..models import Feature
    qs = Feature.objects.all().order_by("sort_order", "code")
    only_active = request.query_params.get("active") in ("1", "true", "True")
    if only_active:
        qs = qs.filter(is_active=True)
    return Response({"results": [_serialize_feature(f) for f in qs]})


# ---------------------------------------------------------------------------
# SubscriptionPackage CRUD
# ---------------------------------------------------------------------------
_VALID_BILLING_MODES = {"time", "usage", "hybrid", "unlimited"}


def _apply_package_payload(pkg, payload: dict, *, partial: bool) -> Optional[str]:
    """Apply payload fields onto pkg. Returns error string or None."""
    from ..models import Feature

    # Required fields on create; optional on PATCH
    for field in ("code", "name"):
        if not partial and not payload.get(field):
            return f"{field} is required"

    if "code" in payload:
        pkg.code = (payload["code"] or "").strip()
    if "name" in payload:
        pkg.name = (payload["name"] or "").strip()
    if "description" in payload:
        pkg.description = payload["description"] or ""

    if "billing_mode" in payload:
        bm = payload["billing_mode"]
        if bm not in _VALID_BILLING_MODES:
            return f"billing_mode must be one of {sorted(_VALID_BILLING_MODES)}"
        pkg.billing_mode = bm

    for int_field in ("default_duration_days", "default_usage_quota",
                      "sort_order"):
        if int_field in payload:
            try:
                pkg.__setattr__(int_field, max(0, int(payload[int_field])))
            except (TypeError, ValueError):
                return f"{int_field} must be a non-negative integer"

    if "price_egp" in payload:
        price = _coerce_decimal(payload["price_egp"])
        if price is None or price < 0:
            return "price_egp must be a non-negative number"
        pkg.price_egp = price

    if "currency" in payload:
        pkg.currency = (payload["currency"] or "EGP")[:3]

    for bool_field in ("is_active", "is_featured"):
        if bool_field in payload:
            pkg.__setattr__(bool_field, bool(payload[bool_field]))

    # feature_codes assignment is deferred until after pkg.save() so the m2m
    # add operation has a valid pk. We validate the codes here though.
    codes = payload.get("feature_codes")
    if codes is not None:
        if not isinstance(codes, list):
            return "feature_codes must be a list"
        known = set(Feature.objects.filter(code__in=codes)
                    .values_list("code", flat=True))
        unknown = [c for c in codes if c not in known]
        if unknown:
            return f"Unknown feature_codes: {unknown}"
    return None


@csrf_exempt
@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
def packages_collection(request: Request) -> Response:
    from ..models import SubscriptionPackage, Feature

    if request.method == "GET":
        qs = SubscriptionPackage.objects.all().prefetch_related("features")
        if request.query_params.get("active") in ("1", "true", "True"):
            qs = qs.filter(is_active=True)
        return Response({"results": [_serialize_package(p) for p in qs]})

    # POST — create
    payload = request.data or {}
    pkg = SubscriptionPackage()
    err = _apply_package_payload(pkg, payload, partial=False)
    if err:
        return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
    if SubscriptionPackage.objects.filter(code=pkg.code).exists():
        return Response(
            {"detail": f"Package with code {pkg.code!r} already exists"},
            status=status.HTTP_409_CONFLICT,
        )
    pkg.save()
    codes = payload.get("feature_codes") or []
    if codes:
        pkg.features.set(Feature.objects.filter(code__in=codes))
    log.info("Package created", extra={
        "by": request.user.username, "code": pkg.code,
        "features": codes,
    })
    return Response(_serialize_package(pkg), status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAdminUser])
def package_detail(request: Request, pk: int) -> Response:
    from ..models import SubscriptionPackage, Feature, TenantPackageGrant

    pkg = SubscriptionPackage.objects.filter(pk=pk).first()
    if pkg is None:
        return Response({"detail": "package not found"},
                        status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(_serialize_package(pkg))

    if request.method == "PATCH":
        payload = request.data or {}
        err = _apply_package_payload(pkg, payload, partial=True)
        if err:
            return Response({"detail": err},
                            status=status.HTTP_400_BAD_REQUEST)
        pkg.save()
        if "feature_codes" in payload:
            codes = payload["feature_codes"] or []
            pkg.features.set(Feature.objects.filter(code__in=codes))
        log.info("Package updated", extra={
            "by": request.user.username, "code": pkg.code,
        })
        return Response(_serialize_package(pkg))

    # DELETE — soft-deactivate so existing TenantPackageGrants survive
    # (they still need to render in the audit timeline / accounting).
    if TenantPackageGrant.objects.filter(package=pkg, status="active").exists():
        # Allow deactivation even with active grants — the grants stay
        # honourable; only NEW grants are blocked because is_active=False
        # excludes the package from the granular lookup filter.
        pass
    pkg.is_active = False
    pkg.save(update_fields=["is_active", "updated_at"])
    log.warning("Package deactivated", extra={
        "by": request.user.username, "code": pkg.code,
    })
    return Response({"pk": pkg.pk, "is_active": pkg.is_active})


# ---------------------------------------------------------------------------
# Grants — assign + revoke + list
# ---------------------------------------------------------------------------
def _compute_valid_until(payload: dict, default_days: int) -> Optional[Any]:
    """Pick valid_until from payload (ISO string) or compute from duration_days."""
    raw = payload.get("valid_until")
    if raw:
        dt = parse_datetime(raw)
        if dt is None:
            return False  # sentinel — caller treats False as invalid
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt
    days = payload.get("duration_days")
    if days is None:
        days = default_days
    try:
        days = int(days)
    except (TypeError, ValueError):
        return False
    if days <= 0:
        return None  # unlimited
    return timezone.now() + timedelta(days=days)


@csrf_exempt
@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
def grants_collection(request: Request) -> Response:
    from django_tenants.utils import schema_context
    from ..models import (
        Feature, SubscriptionPackage,
        TenantFeatureGrant, TenantPackageGrant,
    )

    if request.method == "GET":
        tenant_schema = request.query_params.get("tenant") or ""
        tenant, err = _resolve_tenant(tenant_schema)
        if err is not None:
            return err
        with schema_context(tenant_schema):
            pkg_grants = (TenantPackageGrant.objects
                          .filter(tenant_schema=tenant_schema)
                          .select_related("package")
                          .order_by("-granted_at"))
            feat_grants = (TenantFeatureGrant.objects
                           .filter(tenant_schema=tenant_schema)
                           .select_related("feature")
                           .order_by("-granted_at"))
            results = (
                [_serialize_grant(g, kind="package") for g in pkg_grants]
                + [_serialize_grant(g, kind="feature") for g in feat_grants]
            )
        return Response({"tenant_schema": tenant_schema, "results": results})

    # POST — issue a grant
    payload = request.data or {}
    tenant_schema = (payload.get("tenant_schema") or "").strip()
    tenant, err = _resolve_tenant(tenant_schema)
    if err is not None:
        return err

    package_code = (payload.get("package_code") or "").strip()
    feature_code = (payload.get("feature_code") or "").strip()
    if bool(package_code) == bool(feature_code):
        return Response(
            {"detail": "Provide exactly one of package_code or feature_code"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    price_paid = _coerce_decimal(payload.get("price_paid_egp"))
    if price_paid is None or price_paid < 0:
        return Response({"detail": "price_paid_egp must be a non-negative number"},
                        status=status.HTTP_400_BAD_REQUEST)
    note = (payload.get("note") or "")[:255]
    billing_mode_override = payload.get("billing_mode")
    if billing_mode_override and billing_mode_override not in _VALID_BILLING_MODES:
        return Response({"detail": f"billing_mode must be one of {sorted(_VALID_BILLING_MODES)}"},
                        status=status.HTTP_400_BAD_REQUEST)
    usage_quota_override = payload.get("usage_quota")
    if usage_quota_override is not None:
        try:
            usage_quota_override = max(0, int(usage_quota_override))
        except (TypeError, ValueError):
            return Response({"detail": "usage_quota must be a non-negative integer"},
                            status=status.HTTP_400_BAD_REQUEST)

    with schema_context(tenant_schema):
        if package_code:
            pkg = SubscriptionPackage.objects.filter(code=package_code).first()
            if pkg is None or not pkg.is_active:
                return Response(
                    {"detail": f"Unknown or inactive package: {package_code!r}"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            valid_until = _compute_valid_until(payload, pkg.default_duration_days)
            if valid_until is False:
                return Response({"detail": "valid_until / duration_days invalid"},
                                status=status.HTTP_400_BAD_REQUEST)
            grant = TenantPackageGrant.objects.create(
                tenant_schema=tenant_schema,
                package=pkg,
                billing_mode=billing_mode_override or pkg.billing_mode,
                valid_until=valid_until,
                usage_quota=(usage_quota_override
                             if usage_quota_override is not None
                             else pkg.default_usage_quota),
                price_paid_egp=price_paid,
                granted_by=request.user.username if request.user else "",
                note=note,
            )
            body = _serialize_grant(grant, kind="package")
        else:
            feat = Feature.objects.filter(code=feature_code).first()
            if feat is None or not feat.is_active:
                return Response(
                    {"detail": f"Unknown or inactive feature: {feature_code!r}"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            valid_until = _compute_valid_until(payload, 30)
            if valid_until is False:
                return Response({"detail": "valid_until / duration_days invalid"},
                                status=status.HTTP_400_BAD_REQUEST)
            grant = TenantFeatureGrant.objects.create(
                tenant_schema=tenant_schema,
                feature=feat,
                billing_mode=billing_mode_override or "time",
                valid_until=valid_until,
                usage_quota=usage_quota_override or 0,
                price_paid_egp=price_paid,
                granted_by=request.user.username if request.user else "",
                note=note,
            )
            body = _serialize_grant(grant, kind="feature")

    log.info("Grant issued", extra={
        "by": request.user.username, "tenant": tenant_schema,
        "kind": body["kind"], "ref": body.get("package_code") or body.get("feature_code"),
    })
    return Response(body, status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAdminUser])
def revoke_grant(request: Request, pk: int) -> Response:
    """Mark an active grant as 'revoked'. The kind is auto-detected via the
    `kind` query/body param (package | feature) — required because pk space
    is per-model."""
    from django_tenants.utils import schema_context
    from ..models import TenantFeatureGrant, TenantPackageGrant

    kind = (request.data.get("kind") or request.query_params.get("kind")
            or "").strip().lower()
    tenant_schema = (request.data.get("tenant_schema")
                     or request.query_params.get("tenant_schema") or "").strip()
    if kind not in ("package", "feature"):
        return Response({"detail": "kind must be 'package' or 'feature'"},
                        status=status.HTTP_400_BAD_REQUEST)
    tenant, err = _resolve_tenant(tenant_schema)
    if err is not None:
        return err

    Model = TenantPackageGrant if kind == "package" else TenantFeatureGrant
    with schema_context(tenant_schema):
        grant = Model.objects.filter(pk=pk, tenant_schema=tenant_schema).first()
        if grant is None:
            return Response({"detail": "grant not found"},
                            status=status.HTTP_404_NOT_FOUND)
        if grant.status == "revoked":
            return Response({"detail": "already revoked",
                             "pk": grant.pk}, status=status.HTTP_200_OK)
        grant.status = "revoked"
        grant.save(update_fields=["status", "updated_at"])

    log.warning("Grant revoked", extra={
        "by": request.user.username, "tenant": tenant_schema,
        "kind": kind, "pk": pk,
    })
    return Response({"pk": pk, "kind": kind, "status": "revoked"})
