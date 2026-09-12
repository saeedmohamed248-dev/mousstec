"""
Catalogue bridge — read the tenant's live Mouss Tec inventory for auto-posting.

Social Studio models live in the PUBLIC schema, but a tenant's products live in
that tenant's own schema. This module switches into the tenant schema, reads the
same catalogue the FixIt website sells from, and returns post-ready product dicts
(name, price, image URL, stock, branch origin, store link). The autopilot then
turns each into a product post grounded in real inventory.

Everything is best-effort: any failure returns an empty list rather than raising,
so a catalogue hiccup never stalls the studio.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.db.models import Sum
from django_tenants.utils import schema_context

logger = logging.getLogger("mouss_tec_core")

# Selection strategies for which products to promote.
STRATEGY_NEW = "new"          # newest additions (proxy: highest id)
STRATEGY_FEATURED = "featured"  # most stock on hand (safe to push hard)
STRATEGY_LOW = "low"          # low but still-available stock (create urgency)


def _abs_media_url(image_field) -> str:
    """Return a publicly reachable absolute URL for a product image, or ""."""
    if not image_field:
        return ""
    try:
        url = image_field.url
    except Exception:
        return ""
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = (getattr(settings, "SITE_BASE_URL", "") or os.environ.get("SITE_BASE_URL", "")).rstrip("/")
    if url.startswith("/") and base:
        return base + url
    return url


def _store_link(config, sku: str) -> str:
    """Best-effort deep link to the product on the FixIt store (falls back to home)."""
    base = (config.website_url or "").rstrip("/")
    if not base:
        return ""
    if sku:
        # The FixIt storefront resolves products by SKU via a search query — safe,
        # always-valid link even if a dedicated product route changes.
        return f"{base}/?search={sku}"
    return base


def fetch_catalog_products(config, *, strategy: str = STRATEGY_NEW,
                           count: int = 3, require_image: bool = True) -> list[dict]:
    """Return up to `count` post-ready product dicts from the tenant's inventory.

    Only active, in-stock products are returned. Ordered per `strategy`.
    """
    tenant = config.tenant
    out: list[dict] = []
    try:
        with schema_context(tenant.schema_name):
            from inventory.models import Product

            qs = (Product.objects
                  .filter(is_active=True)
                  .annotate(_stock=Sum("inventory__quantity")))
            qs = qs.filter(_stock__gt=0)
            if require_image:
                qs = qs.exclude(image="").exclude(image__isnull=True)

            if strategy == STRATEGY_FEATURED:
                qs = qs.order_by("-_stock", "-id")
            elif strategy == STRATEGY_LOW:
                # Low but available — nudge urgency. Prefer 1..5 in stock.
                qs = qs.filter(_stock__lte=5).order_by("_stock", "-id")
            else:  # STRATEGY_NEW
                qs = qs.order_by("-id")

            for product in qs[: max(1, count) * 3]:  # over-read; caller de-dups
                image_url = _abs_media_url(product.image)
                if require_image and not image_url:
                    continue
                out.append({
                    "sku": product.part_number or "",
                    "name": product.name or "",
                    "brand": product.brand or "",
                    "condition": product.get_condition_display() if hasattr(product, "get_condition_display") else "",
                    "price": float(product.retail_price or 0),
                    "stock": int(getattr(product, "_stock", 0) or 0),
                    "car_model": product.car_model or "",
                    "car_year": product.car_year or "",
                    "image_url": image_url,
                    "link": _store_link(config, product.part_number or ""),
                })
                if len(out) >= count:
                    break
    except Exception as exc:
        logger.warning("social_ads: catalogue read failed for %s: %s",
                       getattr(tenant, "schema_name", "?"), exc)
        return []
    return out


def build_product_hint(product: dict, currency: str = "ج.م") -> str:
    """Turn a product dict into a grounding hint for the content LLM.

    The model must quote the real name/price and drive to the store — never invent
    specs. We pass the facts; the model writes the persuasive Arabic copy.
    """
    price = product.get("price") or 0
    price_txt = f"{price:,.0f} {currency}" if price else "السعر عند الطلب"
    lines = [
        "روّج للمنتج ده تحديداً باستخدام الحقائق دي فقط (متخترعش مواصفات):",
        f"- القطعة: {product.get('name','')}",
    ]
    if product.get("brand"):
        lines.append(f"- الماركة: {product['brand']}")
    if product.get("car_model") or product.get("car_year"):
        lines.append(f"- تناسب: {product.get('car_model','')} {product.get('car_year','')}".rstrip())
    if product.get("condition"):
        lines.append(f"- الحالة: {product['condition']}")
    lines.append(f"- السعر: {price_txt}")
    if product.get("sku"):
        lines.append(f"- كود القطعة: {product['sku']}")
    if product.get("link"):
        lines.append(f"- اطلب من الموقع: {product['link']}")
    lines.append("اكتب البوست بنبرة تبيع، مع دعوة واضحة للطلب أو التواصل، بدون مبالغة كاذبة.")
    return "\n".join(lines)
