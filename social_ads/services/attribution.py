"""
Sales attribution — connect published product posts to real Mouss Tec sales.

A post created from inventory carries `product_sku`. When that part later sells
(a posted, non-return SaleInvoice line in the tenant schema), we credit the sale
to the most recent post that promoted the SKU, within an attribution window.
This turns the studio from an engagement tool into a *sales* tool: the strategist
can then learn which posts drive revenue, not just likes.

Recomputed from scratch each run over a bounded window, so it is idempotent and
self-correcting (a later return removes the credit on the next pass).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django_tenants.utils import schema_context

logger = logging.getLogger("mouss_tec_core")

# A sale counts toward a post if it happens within this many days of publishing.
ATTRIBUTION_WINDOW_DAYS = 14
# How far back to (re)attribute on each run.
LOOKBACK_DAYS = 45


def attribute_sales(config) -> dict:
    """Recompute attributed sales for a tenant's recent product posts.

    Returns {"posts": n, "sales": total_units, "value": total_value}.
    """
    from social_ads.models import SocialPost

    now = timezone.now()
    posts = list(
        SocialPost.objects.filter(
            config=config,
            status=SocialPost.Status.PUBLISHED,
            published_at__gte=now - timedelta(days=LOOKBACK_DAYS),
        ).exclude(product_sku="").exclude(published_at__isnull=True)
        .order_by("published_at")
    )
    if not posts:
        return {"posts": 0, "sales": 0, "value": 0}

    # SKU → posts (chronological), and reset counters for a clean recompute.
    by_sku: dict[str, list] = {}
    for p in posts:
        p.attributed_sales_count = 0
        p.attributed_sales_value = Decimal("0")
        by_sku.setdefault(p.product_sku, []).append(p)

    earliest = min(p.published_at for p in posts)

    # ── Read matching sales from the tenant schema ────────────────────
    sales: list[dict] = []
    try:
        with schema_context(config.tenant.schema_name):
            from inventory.models import SaleInvoiceItem

            rows = (SaleInvoiceItem.objects
                    .filter(
                        product__part_number__in=list(by_sku.keys()),
                        invoice__status="posted",
                        invoice__is_return=False,
                        invoice__date_created__gte=earliest,
                    )
                    .values("product__part_number", "quantity", "unit_price",
                            "discount", "invoice__date_created"))
            for r in rows:
                sales.append({
                    "sku": r["product__part_number"],
                    "qty": int(r["quantity"] or 0),
                    "value": (Decimal(str(r["unit_price"] or 0)) * int(r["quantity"] or 0))
                             - Decimal(str(r["discount"] or 0)),
                    "date": r["invoice__date_created"],
                })
    except Exception as exc:
        logger.warning("social_ads: attribution read failed for %s: %s",
                       config.tenant.schema_name, exc)
        return {"posts": 0, "sales": 0, "value": 0}

    # ── Assign each sale to the best-matching post ────────────────────
    total_units = 0
    total_value = Decimal("0")
    window = timedelta(days=ATTRIBUTION_WINDOW_DAYS)
    for sale in sales:
        candidates = by_sku.get(sale["sku"]) or []
        target = None
        for p in candidates:  # chronological; pick latest published on/before sale
            if p.published_at <= sale["date"] <= p.published_at + window:
                target = p  # keep advancing → ends on the most recent eligible
        if not target:
            continue
        target.attributed_sales_count += sale["qty"]
        target.attributed_sales_value = (Decimal(str(target.attributed_sales_value))
                                         + sale["value"])
        total_units += sale["qty"]
        total_value += sale["value"]

    # ── Persist ───────────────────────────────────────────────────────
    for p in posts:
        p.save(update_fields=["attributed_sales_count", "attributed_sales_value", "updated_at"])

    logger.info("social_ads: attributed %d sales (value=%s) across %d posts for %s",
                total_units, total_value, len(posts), config.tenant.schema_name)
    return {"posts": len(posts), "sales": total_units, "value": float(total_value)}
