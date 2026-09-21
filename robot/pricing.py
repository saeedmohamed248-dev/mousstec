"""
robot/pricing.py — the single, audited gate for every price the robot sees.

BUSINESS RULE (hard requirement): the robot is a customer- and floor-facing
device. It must NEVER expose, speak, log or return the wholesale/B2B price, the
purchase price, or the average cost of a part. Only RETAIL figures may leave the
backend toward a robot:

    * retail_price       — سعر البيع القطاعي (the normal customer price)
    * scrap_price        — for used/scrap parts, a retail-side floor
    * ai_suggested_price — AI market retail suggestion (used part condition)

`inventory.Product` also carries `b2b_wholesale_price`, `purchase_price` and
`average_cost`. Those are FORBIDDEN here. To make the rule impossible to break by
accident, this module builds robot payloads with an explicit allow-list and a
paranoid assertion that no forbidden key ever slips through.

Everything the robot API returns about a product goes through
`safe_product_payload()`. Nothing else should read Product price fields for the
robot.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# Fields that may never appear in a robot-facing payload, under any name.
FORBIDDEN_PRICE_FIELDS = frozenset({
    "b2b_wholesale_price",
    "wholesale_price",
    "wholesale",
    "purchase_price",
    "average_cost",
    "cost",
    "cost_at_sale",
    "last_purchase_price",
})

# The only price fields the robot is ever allowed to receive.
_RETAIL_FIELDS = ("retail_price", "scrap_price", "ai_suggested_price")


def _money(value: Any) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)))


def safe_product_payload(product, *, branch=None, include_scrap: bool = False) -> dict:
    """Build the ONLY dict about a product the robot is allowed to receive.

    Retail price always; used/scrap retail figures only when `include_scrap`
    (i.e. the scrap-condition flow). Never wholesale, never cost.

    `branch` (optional) scopes the stock number to one branch; without it the
    total across branches is returned.
    """
    payload: dict[str, Any] = {
        "id": product.id,
        "name": product.name,
        "part_number": product.part_number,
        "barcode": getattr(product, "barcode", None),
        # Part codes / OEM cross-reference — safe to expose, helps matching.
        "oem_cross_reference": getattr(product, "oem_cross_reference", None) or [],
        "all_part_numbers": list(getattr(product, "all_part_numbers", []) or []),
        "car_model": getattr(product, "car_model", None),
        "retail_price": _money(getattr(product, "retail_price", None)),
        "warranty_months": getattr(product, "warranty_months", None),
    }

    if include_scrap:
        payload["scrap_price"] = _money(getattr(product, "scrap_price", None))
        payload["ai_suggested_price"] = _money(getattr(product, "ai_suggested_price", None))

    # Stock: per-branch if given, else total across the company.
    if branch is not None:
        from inventory.models import Inventory  # local import (app-loading order)
        inv = Inventory.objects.filter(product=product, branch=branch).first()
        payload["stock"] = inv.quantity if inv else 0
        payload["branch"] = branch.name
    else:
        payload["stock"] = getattr(product, "total_stock", 0)

    _assert_no_wholesale(payload)
    return payload


def _assert_no_wholesale(payload: dict) -> None:
    """Fail loudly if a forbidden price field ever reaches a robot payload.

    This is a defense-in-depth tripwire: if someone later adds a field to
    `safe_product_payload` by copying from a staff serializer, this raises in
    tests and in prod logs instead of silently leaking wholesale pricing.
    """
    leaked = FORBIDDEN_PRICE_FIELDS.intersection(payload.keys())
    if leaked:
        raise AssertionError(
            f"Robot payload would leak forbidden price field(s): {sorted(leaked)}. "
            "The robot may only ever see retail prices."
        )


def redact(text: str) -> str:
    """Last-line scrub for any free-text the robot is about to speak/return.

    Not a substitute for `safe_product_payload` — just a belt-and-braces filter
    so an LLM reply can't accidentally read out a wholesale figure it was told.
    Deliberately dependency-free (no Django translation) so it is usable and
    testable in any context.
    """
    lowered = text.lower()
    for term in ("wholesale", "جمله", "جملة", "cost price", "سعر التكلفة", "التكلفة"):
        if term in lowered:
            return "[price detail withheld]"
    return text
