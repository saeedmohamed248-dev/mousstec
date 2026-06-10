"""
Vehicle fitment matching.

Sellers want to see *open* PartWantedRequest rows that match a vehicle
they actually stock parts for. The match is a strict equality on the
filter dimensions the seller provides — partial matches are NOT in scope
here (that produces noise for high-value engine parts).

Standard filter dimensions:
    make        — required when filtering
    model       — case-insensitive exact match (e.g. 'F30' == 'f30')
    year        — exact int
    engine_code — case-insensitive exact match; '' means "ignore"

Anything else (description keywords, fuzzy model matching) belongs in
search/relevance, not in this strict filter.
"""
from __future__ import annotations

from typing import Optional

from django.db.models import Q
from django.utils import timezone


def open_wanted_requests(
    *,
    make=None,
    model: Optional[str] = None,
    year: Optional[int] = None,
    engine_code: Optional[str] = None,
):
    """
    Return a queryset of PartWantedRequest matching the given filters.

    All filters are AND'd. None / empty values are ignored. The queryset
    excludes soft-deleted, non-open, and expired requests at the source —
    callers don't need to re-filter.
    """
    # Local import to avoid circular dependency at module load.
    from clients.models import PartWantedRequest

    qs = PartWantedRequest.objects.filter(
        is_deleted=False,
        status='open',
        expires_at__gt=timezone.now(),
    ).select_related('car_make', 'buyer_customer', 'buyer_tenant')

    if make is not None:
        qs = qs.filter(car_make=make)
    if model:
        qs = qs.filter(car_model__iexact=model.strip())
    if year is not None:
        qs = qs.filter(car_year=year)
    if engine_code:
        qs = qs.filter(engine_code__iexact=engine_code.strip().upper())

    return qs.order_by('-created_at')
