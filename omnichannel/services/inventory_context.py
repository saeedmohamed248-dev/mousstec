"""
Build a compact, LLM-ready snapshot of the tenant's priced catalogue.

IMPORTANT: every function here assumes the caller has already switched into the
tenant's schema (via django_tenants.utils.schema_context). Product / stock /
service tables live in the tenant schema, so calling these from the public
schema would read the wrong (or empty) tables.

The snapshot is deliberately small and text-only — we feed it to the LLM as
grounding context, so it must stay well under the model's context budget. We
prioritise items whose name/part-number matches the customer's question, then
back-fill with best-selling / in-stock items.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("mouss_tec_core")

_MAX_ITEMS = 25
_MIN_TOKEN_LEN = 3


def _keywords(text: str) -> list[str]:
    tokens = re.findall(r"[\w؀-ۿ]+", (text or "").lower())
    return [t for t in tokens if len(t) >= _MIN_TOKEN_LEN][:8]


def build_catalog_context(query_text: str, *, currency: str = "") -> str:
    """Return a plain-text catalogue excerpt relevant to `query_text`.

    Best-effort and never raises: any per-source failure is logged and skipped,
    so a missing table or app can't break the auto-reply pipeline.
    """
    blocks: list[str] = []

    parts = _automotive_products(query_text, currency)
    if parts:
        blocks.append("قطع الغيار المتوفرة (Parts in stock):\n" + parts)

    services = _service_catalog(query_text, currency)
    if services:
        blocks.append("الخدمات والمصنعيات (Services):\n" + services)

    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _fmt_price(value, currency: str) -> str:
    try:
        amount = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        amount = str(value)
    return f"{amount} {currency}".strip()


def _automotive_products(query_text: str, currency: str) -> str:
    try:
        from django.db.models import Q, Sum
        from inventory.models.catalog import Product
    except Exception:  # app/table not present in this tenant
        return ""

    try:
        qs = Product.objects.filter(is_active=True)

        keywords = _keywords(query_text)
        if keywords:
            q = Q()
            for kw in keywords:
                q |= (
                    Q(name__icontains=kw)
                    | Q(part_number__icontains=kw)
                    | Q(car_model__icontains=kw)
                    | Q(brand__icontains=kw)
                )
            matched = list(qs.filter(q)[: _MAX_ITEMS])
        else:
            matched = []

        if len(matched) < _MAX_ITEMS:
            fill = qs.exclude(pk__in=[p.pk for p in matched])[: _MAX_ITEMS - len(matched)]
            matched.extend(fill)

        lines = []
        for p in matched[:_MAX_ITEMS]:
            try:
                qty = p.total_inventory_qty
            except Exception:
                qty = None
            price = _fmt_price(p.retail_price, currency)
            stock = f"متوفر: {qty}" if qty is not None else ""
            lines.append(
                f"- {p.name} (كود {p.part_number}"
                + (f" | {p.car_model}" if p.car_model else "")
                + f") — السعر {price}"
                + (f" — {stock}" if stock else "")
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("omnichannel: automotive catalog lookup failed: %s", exc)
        return ""


def _service_catalog(query_text: str, currency: str) -> str:
    try:
        from inventory.models.catalog import ServiceCatalog
    except Exception:
        return ""
    try:
        services = list(ServiceCatalog.objects.all()[:_MAX_ITEMS])
        lines = [
            f"- {s.name} — {_fmt_price(s.labor_price, currency)}"
            for s in services
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("omnichannel: service catalog lookup failed: %s", exc)
        return ""
