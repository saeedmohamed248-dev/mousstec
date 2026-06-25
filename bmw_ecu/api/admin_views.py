"""Super-Admin gift endpoint — POST /api/admin/entitlements/gift.

Only Mousstec management (users with `is_staff=True`) can grant gifts.
The endpoint writes a GiftCredit row in the targeted tenant's schema so
the credit is immediately visible to the tenant's entitlement +
settlement layers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response

from ..logging_setup import get_logger
from ._admin_validation import validate_gift_payload as _validate

log = get_logger(__name__)


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAdminUser])
def grant_gift(request: Request) -> Response:
    """Issue a promotional gift credit to a specific tenant.

    Body:
        {
          "tenant_schema": "workshop_acme",     # required
          "grant_type": "coding_credits",       # required: coding_credits |
                                                #          isn_credits |
                                                #          subscription_window
          "credits": 5,                         # required for *_credits
          "valid_until": "2026-12-31T23:59",    # required for subscription_window;
                                                # optional cap for *_credits
          "note": "Eid promo — 5 free codings", # optional
          "allow_stack": false                  # optional; default false →
                                                # refuse if another active gift
                                                # of the same type exists
        }
    """
    payload = request.data or {}
    err = _validate(payload)
    if err:
        return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

    tenant_schema: str = payload["tenant_schema"]
    grant_type: str = payload["grant_type"]
    credits = int(payload.get("credits") or 0)
    valid_until_raw = payload.get("valid_until")
    valid_until = parse_datetime(valid_until_raw) if valid_until_raw else None
    if valid_until and timezone.is_naive(valid_until):
        valid_until = timezone.make_aware(valid_until)
    note = (payload.get("note") or "")[:255]
    allow_stack = bool(payload.get("allow_stack", False))

    # All gift rows live in the PUBLIC schema so super-admins working on
    # any schema can read/write them, and gift_credits.consume_gift can
    # always find them.
    from django_tenants.utils import schema_context
    from ..models import GiftCredit

    with schema_context("public"):
        if not allow_stack:
            existing = GiftCredit.objects.filter(
                tenant_schema=tenant_schema, grant_type=grant_type,
                status="active",
            ).first()
            if existing is not None:
                return Response(
                    {"detail": "Active gift of this type already exists",
                     "existing_pk": existing.pk,
                     "hint": "Pass allow_stack=true to stack."},
                    status=status.HTTP_409_CONFLICT,
                )

        gift = GiftCredit.objects.create(
            tenant_schema=tenant_schema,
            grant_type=grant_type,
            credits_total=credits if grant_type != "subscription_window" else 0,
            credits_remaining=credits if grant_type != "subscription_window" else 0,
            valid_until=valid_until,
            note=note,
            granted_by=request.user.username if request.user else "",
        )

    log.info("Gift granted", extra={
        "by": gift.granted_by, "tenant": tenant_schema,
        "type": grant_type, "credits": credits,
        "valid_until": valid_until.isoformat() if valid_until else None,
    })
    return Response({
        "pk": gift.pk,
        "tenant_schema": gift.tenant_schema,
        "grant_type": gift.grant_type,
        "credits_total": gift.credits_total,
        "credits_remaining": gift.credits_remaining,
        "valid_from": gift.valid_from.isoformat(),
        "valid_until": gift.valid_until.isoformat() if gift.valid_until else None,
        "status": gift.status,
        "granted_by": gift.granted_by,
        "note": gift.note,
    }, status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(["POST"])
@permission_classes([IsAdminUser])
def revoke_gift(request: Request, gift_pk: int) -> Response:
    """Revoke an active gift. Audit usages survive; further consumption blocked."""
    from django_tenants.utils import schema_context
    from ..models import GiftCredit

    with schema_context("public"):
        try:
            gift = GiftCredit.objects.get(pk=gift_pk)
        except GiftCredit.DoesNotExist:
            return Response({"detail": "Gift not found"},
                            status=status.HTTP_404_NOT_FOUND)
        if gift.status != "active":
            return Response({"detail": f"Gift status is {gift.status!r}, not active"},
                            status=status.HTTP_409_CONFLICT)
        gift.status = "revoked"
        gift.save(update_fields=["status"])
    log.info("Gift revoked", extra={"pk": gift_pk,
                                    "by": request.user.username if request.user else ""})
    return Response({"pk": gift.pk, "status": gift.status})


# `_validate` is the pure validator from `_admin_validation`, imported at
# top of file. Tests import it directly from there to avoid pulling DRF in.
