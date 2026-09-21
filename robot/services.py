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

    Posting order matters: the `execute_sale_posting` signal fires when the
    invoice's status becomes 'posted', and it deducts stock from the invoice's
    ITEMS. So we must create the invoice as a draft quotation, add the item(s),
    then flip to 'posted' — otherwise the signal would run on an empty invoice,
    deduct nothing, and mark it applied. Stock deduction, totals, accrual and
    treasury are then handled by the existing InvoiceService — we don't
    reimplement them.
    """
    from inventory.models import SaleInvoice, SaleInvoiceItem

    if unit_price is None:
        unit_price = Decimal(str(product.retail_price or 0))
    unit_price = Decimal(str(unit_price))

    # 1) Draft first (status defaults to 'quotation'; invoice_type must be 'sale').
    invoice = SaleInvoice.objects.create(
        invoice_type="sale",
        status="quotation",
        customer=customer,
        branch=branch,
        sales_channel="in_store",  # only in_store/website exist; robot is in-store
    )
    # 2) Add the line at the explicit RETAIL price (never auto-filled wholesale).
    SaleInvoiceItem.objects.create(
        invoice=invoice,
        product=product,
        quantity=quantity,
        unit_price=unit_price,
    )
    invoice.update_total()

    # 3) Post — NOW the signal deducts stock/accrues with the item present.
    invoice.status = "posted"
    invoice.save(update_fields=["status"])
    invoice.refresh_from_db()
    return invoice


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


def _normalize_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


# ---------------------------------------------------------------------------
# Learning memory — the robot gets better at recognizing parts with use
# ---------------------------------------------------------------------------

def resolve_from_knowledge(*, code: str = "", label: str = "", fingerprint_hash: str = ""):
    """Return a previously-confirmed Product for a code/label/fingerprint, or None.

    Checked before falling back to a fresh catalogue search, so a part the robot
    has been taught once is recognized instantly next time.
    """
    from .models import RobotKnowledge

    for kind, val in (("code", code), ("label", label), ("fingerprint", fingerprint_hash)):
        val = _normalize_key(val)
        if not val:
            continue
        entry = (
            RobotKnowledge.objects.filter(key_kind=kind, key_value=val)
            .select_related("product")
            .order_by("-hit_count")
            .first()
        )
        if entry:
            return entry.product
    return None


def learn_from_confirmation(*, product, code: str = "", label: str = "",
                            fingerprint_hash: str = "", details: dict = None,
                            employee=None):
    """Record that a human confirmed code/label/fingerprint → product.

    Idempotent per (kind, value, product): repeat confirmations bump `hit_count`
    (rising confidence) instead of duplicating rows.
    """
    from .models import RobotKnowledge

    created_entries = []
    for kind, val in (("code", code), ("label", label), ("fingerprint", fingerprint_hash)):
        val = _normalize_key(val)
        if not val:
            continue
        entry, created = RobotKnowledge.objects.get_or_create(
            key_kind=kind, key_value=val, product=product,
            defaults={"details": details or {}, "confirmed_by": employee},
        )
        if not created:
            entry.hit_count = models_F_increment(entry)
            entry.save(update_fields=["hit_count", "updated_at"])
        created_entries.append(entry)
    return created_entries


def models_F_increment(entry):
    """Return hit_count+1 as a plain int (kept simple; avoids F() refresh dance)."""
    return (entry.hit_count or 0) + 1


# ---------------------------------------------------------------------------
# Goods intake — "صوّر القطعة وسجّلها": photograph → white bg → add/increment
# ---------------------------------------------------------------------------

@transaction.atomic
def intake_part(*, device, branch, image_bytes: bytes = None, name: str = "",
                part_number: str = "", retail_price=None, quantity: int = 1,
                car_model: str = "", part_category: str = "", employee=None):
    """Photograph a part, register it, and add stock — the core intake flow.

    Steps:
      1. If an image is given, read the part number via vision (unless supplied)
         and produce a clean studio-white version for the product photo.
      2. Find the product (knowledge memory → catalogue by code/OEM/name).
      3. If found → increment on-hand quantity at this branch (an InventoryMovement
         is recorded by the existing inventory signal).
      4. If NOT found → create a new Product with its details, retail price and
         white-background photo, then set the branch stock.
      5. Learn the mapping (code/label) → product so next time is instant.

    Returns a dict describing what happened.
    """
    from django.core.files.base import ContentFile
    from inventory.models import Inventory, Product, ProductImage
    from .models import RobotScanEvent
    from . import vision

    # 1) Vision: read code + make white-background image.
    white_bytes = None
    if image_bytes:
        if not part_number:
            _label, code, conf = vision.identify_part(image_bytes)
            if conf >= 0.75 and code:
                part_number = code
        white_bytes = vision.white_background(image_bytes)

    # 2) Resolve existing product (learning memory first, then catalogue).
    product = resolve_from_knowledge(code=part_number, label=name)
    if product is None and part_number:
        product = find_product(part_number)
    if product is None and name:
        product = find_product(name)

    created_new = False
    if product is None:
        # 4) Create a brand-new product with sensible defaults for required fields.
        if not part_number:
            # Deterministic fallback SKU so intake never fails for lack of a code.
            from django.utils.crypto import get_random_string
            part_number = f"ROBOT-{get_random_string(8).upper()}"
        product = Product.objects.create(
            name=name or part_number,
            part_number=part_number,
            part_category=part_category or "mechanical",
            car_model=car_model or "غير محدد",
            car_year="غير محدد",
            retail_price=Decimal(str(retail_price or 0)),
        )
        created_new = True
        _attach_photo(product, white_bytes or image_bytes, ProductImage, ContentFile)
    else:
        # Update retail price if a new one was dictated, and ensure a photo.
        if retail_price not in (None, "", 0):
            product.retail_price = Decimal(str(retail_price))
            product.save(update_fields=["retail_price"])
        if (white_bytes or image_bytes) and not product.images.exists():
            _attach_photo(product, white_bytes or image_bytes, ProductImage, ContentFile)

    # 3/4) Add stock at the branch (signal records the movement on update).
    inv, _created_inv = Inventory.objects.select_for_update().get_or_create(
        product=product, branch=branch, defaults={"quantity": 0},
    )
    inv.quantity = (inv.quantity or 0) + int(quantity)
    inv.save(update_fields=["quantity"])

    # 5) Learn + audit.
    learn_from_confirmation(
        product=product, code=part_number, label=name, employee=employee,
    )
    RobotScanEvent.objects.create(
        device=device, purpose="intake",
        recognized_label=name, recognized_part_number=part_number,
        confidence=1.0, product=product, created_by=employee,
        quantity_added=int(quantity), created_new_product=created_new,
    )

    # Low-stock is unlikely right after intake, but keep the ecosystem in sync.
    maybe_raise_procurement_signal(device=device, product=product, branch=branch)

    return {
        "ok": True,
        "created_new_product": created_new,
        "product_id": product.id,
        "part_number": product.part_number,
        "name": product.name,
        "quantity_added": int(quantity),
        "on_hand": inv.quantity,
        "retail_price": float(product.retail_price or 0),
    }


def _attach_photo(product, image_bytes, ProductImage, ContentFile):
    """Save an image (white-bg preferred) as the product's primary photo."""
    if not image_bytes:
        return
    fname = f"{product.part_number}.jpg"
    img = ProductImage(product=product, is_primary=True)
    img.image.save(fname, ContentFile(image_bytes), save=True)


# ---------------------------------------------------------------------------
# Stock-take (جرد) — "اجرد كذا وكذا"
# ---------------------------------------------------------------------------

@transaction.atomic
def run_stock_take(*, device, branch, counts: list, instruction: str = "", employee=None):
    """Reconcile a list of counted items against on-hand inventory.

    `counts` = [{"query": "<code/name>", "counted_qty": <int>}, ...]. Creates a
    RobotStockTakeSession with a reconciled line per resolvable item. Does NOT
    auto-adjust stock — a supervisor approves variances from the dashboard.
    """
    from inventory.models import Inventory
    from .models import RobotStockTakeSession, RobotStockTakeLine

    session = RobotStockTakeSession.objects.create(
        device=device, branch=branch, instruction=instruction,
        status="completed", started_by=employee, completed_at=timezone.now(),
    )
    lines, unresolved = [], []
    for row in counts or []:
        product = resolve_from_knowledge(code=row.get("query", ""), label=row.get("query", ""))
        if product is None:
            product = find_product(row.get("query", ""))
        if product is None:
            unresolved.append(row.get("query", ""))
            continue
        inv = Inventory.objects.filter(product=product, branch=branch).first()
        expected = inv.quantity if inv else 0
        line = RobotStockTakeLine.objects.create(
            session=session, product=product,
            expected_qty=expected, counted_qty=int(row.get("counted_qty", 0) or 0),
        )
        lines.append(line)

    return {
        "session_id": session.id,
        "counted_items": len(lines),
        "matches": sum(1 for ln in lines if ln.matched),
        "variances": [
            {"product": ln.product.name, "expected": ln.expected_qty,
             "counted": ln.counted_qty, "variance": ln.variance}
            for ln in lines if not ln.matched
        ],
        "unresolved": [u for u in unresolved if u],
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
