"""
robot/services.py — business logic bridging the hardware to the Mouss Tec ERP.

Pure functions/helpers (no HTTP here — see views.py). Everything that touches a
product price goes through `robot.pricing.safe_product_payload` so wholesale/cost
can never leak to the robot.

Integrations:
  * inventory.Product / Inventory / Branch / SaleInvoice / StockAlert
  * hr.Employee / AttendanceRecord (via robot.security)
  * diagnostics_catalog.DTCDefinition — fault codes ("قرابات الأعطال")
  * bmw_ecu.EcuHardwareProfile / EcuPinoutDiagram — ECU coding/programming refs
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .pricing import safe_product_payload


# ---------------------------------------------------------------------------
# Part / inventory lookup (branch-aware, retail-only)
# ---------------------------------------------------------------------------

def find_product(query: str):
    """Best-effort match of a scanned code or spoken description to a Product.

    Order: exact part_number / barcode → OEM & additional part numbers → name
    contains. Returns a Product or None. Never returns pricing directly — callers
    serialize via `safe_product_payload`.
    """
    from inventory.models import Product

    q = (query or "").strip()
    if not q:
        return None

    # 1) Exact code hits (part_number, barcode).
    exact = Product.objects.filter(
        Q(part_number__iexact=q) | Q(barcode__iexact=q)
    ).first()
    if exact:
        return exact

    # 2) OEM cross-reference / additional part numbers (JSON contains).
    #    icontains on the JSON text is a pragmatic match across DBs.
    code_hit = Product.objects.filter(
        Q(oem_cross_reference__icontains=q) | Q(additional_part_numbers__icontains=q)
    ).first()
    if code_hit:
        return code_hit

    # 3) Name / model description.
    return Product.objects.filter(
        Q(name__icontains=q) | Q(car_model__icontains=q)
    ).first()


def inventory_answer(query: str, branch=None) -> dict:
    """Answer a stock/price question (voice assistant or lookup scan).

    Returns a robot-safe dict: found flag + retail-only product payload, or a
    not-found marker. This is what the workshop assistant speaks back.
    """
    product = find_product(query)
    if not product:
        return {"found": False, "query": query}
    payload = safe_product_payload(product, branch=branch)
    payload["found"] = True
    return payload


# ---------------------------------------------------------------------------
# Scrap condition → dynamic RETAIL price suggestion
# ---------------------------------------------------------------------------

def suggest_used_price(product, condition_score: float) -> Decimal:
    """Suggest a RETAIL selling price for a used part from its condition.

    `condition_score` ∈ [0,1] (0 = destroyed, 1 = like-new) comes from the vision
    wear analysis. We interpolate between the scrap floor and the AI/retail
    ceiling — deliberately anchored to RETAIL figures only, never wholesale.

    ceiling = ai_suggested_price if set else retail_price
    floor   = scrap_price
    price   = floor + condition * (ceiling - floor)
    """
    score = max(0.0, min(1.0, float(condition_score)))
    retail = Decimal(str(product.retail_price or 0))
    ai = Decimal(str(getattr(product, "ai_suggested_price", 0) or 0))
    ceiling = ai if ai > 0 else retail
    floor = Decimal(str(getattr(product, "scrap_price", 0) or 0))
    if ceiling <= 0:
        return floor
    if floor > ceiling:
        floor = ceiling
    price = floor + (Decimal(str(score)) * (ceiling - floor))
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# POS: create a sale invoice from a scan (RETAIL price enforced explicitly)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_robot_sale(*, product, branch, customer, employee=None,
                      quantity: int = 1, unit_price: Optional[Decimal] = None,
                      payment: str = "cash"):
    """Create a one-line sale invoice from the robot POS flow.

    CRITICAL: we ALWAYS pass an explicit `unit_price` = retail_price. Leaving it
    blank makes `SaleInvoiceItem.save()` auto-fill the B2B *wholesale* price —
    exactly what the robot must never sell at. So `unit_price` defaults to the
    product's retail_price and is passed explicitly.

    Stock deduction, totals and profit are handled by the existing inventory
    signals/`update_total` — we don't reimplement them.
    """
    from inventory.models import SaleInvoice, SaleInvoiceItem

    if unit_price is None:
        unit_price = Decimal(str(product.retail_price or 0))
    unit_price = Decimal(str(unit_price))

    invoice = SaleInvoice.objects.create(
        invoice_type="retail_sale",
        status="posted",
        customer=customer,
        branch=branch,
        sales_channel="robot" if _has_channel("robot") else "in_store",
    )
    SaleInvoiceItem.objects.create(
        invoice=invoice,
        product=product,
        quantity=quantity,
        unit_price=unit_price,   # explicit retail — never let it auto-fill wholesale
    )
    invoice.update_total()
    return invoice


def _has_channel(value: str) -> bool:
    """True if SaleInvoice.sales_channel offers `value` as a choice."""
    try:
        from inventory.models import SaleInvoice
        field = SaleInvoice._meta.get_field("sales_channel")
        return any(value == c[0] for c in (field.choices or []))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Multi-agent sync: raise a procurement signal on critically-low stock
# ---------------------------------------------------------------------------

def maybe_raise_procurement_signal(*, device, product, branch):
    """If stock at `branch` is at/below the alert level, raise a trigger signal.

    Idempotent per (product, branch) while status='open' (DB constraint). Returns
    the ProcurementSignal or None. The Procurement Agent consumes open signals to
    build the next shipping-container manifest — the robot never orders directly.
    """
    from inventory.models import Inventory
    from .models import ProcurementSignal

    inv = Inventory.objects.filter(product=product, branch=branch).first()
    on_hand = inv.quantity if inv else 0
    min_level = getattr(product, "ai_calculated_min_stock", None) or getattr(
        product, "min_stock_level", 0
    )
    if on_hand > min_level:
        return None

    # Suggested reorder: bring back to ~2x the alert level, at least 1.
    reorder = max((min_level * 2) - on_hand, 1)
    signal, _created = ProcurementSignal.objects.get_or_create(
        product=product, branch=branch, status="open",
        defaults={
            "device": device,
            "quantity_on_hand": on_hand,
            "min_stock_level": min_level,
            "suggested_reorder_qty": reorder,
            "manifest_payload": {
                "part_number": product.part_number,
                "oem": getattr(product, "oem_cross_reference", None) or [],
                "name": product.name,
                "branch": branch.name,
                "on_hand": on_hand,
                "min_level": min_level,
                "suggested_qty": reorder,
                "raised_at": timezone.now().isoformat(),
            },
        },
    )
    return signal


# ---------------------------------------------------------------------------
# Diagnostics + ECU enrichment (part codes, fault codes, coding refs)
# ---------------------------------------------------------------------------

def lookup_fault_code(code: str) -> dict:
    """Resolve a DTC / fault code to its definition + guided steps + causes.

    Backs the voice assistant when a mechanic asks about a code (e.g. "P0301").
    Reads the shared `diagnostics_catalog.DTCDefinition`.
    """
    try:
        from diagnostics_catalog.models import DTCDefinition
    except Exception:
        return {"found": False, "code": code}

    dtc = DTCDefinition.objects.filter(code__iexact=(code or "").strip()).first()
    if not dtc:
        return {"found": False, "code": code}
    return {
        "found": True,
        "code": dtc.code,
        "severity": getattr(dtc, "severity", None),
        "description": getattr(dtc, "short_description", "") or getattr(dtc, "long_description", ""),
        "guided_steps": getattr(dtc, "guided_steps", []) or [],
        "likely_causes": getattr(dtc, "likely_causes", []) or [],
    }


def lookup_ecu_profile(ecu_name: str) -> dict:
    """Return ECU hardware/coding reference (pinout + physical steps) for a module.

    Backs the "programming/coding" requirement — the robot can read out the
    verified physical steps and pinout callouts for an ECU from `bmw_ecu`.
    """
    try:
        from bmw_ecu.models import EcuHardwareProfile
    except Exception:
        return {"found": False, "ecu_name": ecu_name}

    profile = EcuHardwareProfile.objects.filter(
        ecu_name__iexact=(ecu_name or "").strip()
    ).first()
    if not profile:
        return {"found": False, "ecu_name": ecu_name}
    return {
        "found": True,
        "ecu_name": profile.ecu_name,
        "board_revision": getattr(profile, "board_revision", ""),
        "verified": getattr(profile, "verified", False),
        "callouts": getattr(profile, "callouts", []) or [],
        "physical_steps_ar": getattr(profile, "physical_steps_ar", []) or [],
        "physical_steps_en": getattr(profile, "physical_steps_en", []) or [],
    }
