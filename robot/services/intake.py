"""📦 خدمات الروبوت — استلام البضاعة: صوّر القطعة وسجّلها."""

from __future__ import annotations

from decimal import Decimal

import logging

from django.db import transaction

from robot import services  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from .learning import resolve_from_knowledge

logger = logging.getLogger("mouss_tec_core")


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
      3. If found → increment on-hand quantity at this branch (inventory_service.
         set_branch_stock: the movement, the average cost, the ledger).
      4. If NOT found → create a new Product with its details, retail price and
         white-background photo, then set the branch stock.
      5. Learn the mapping (code/label) → product so next time is instant.

    Returns a dict describing what happened.
    """
    from django.core.files.base import ContentFile
    from inventory.models import Inventory, Product, ProductImage
    from ..models import RobotScanEvent
    from .. import vision

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
        product = services.find_product(part_number)
    if product is None and name:
        product = services.find_product(name)

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

    # 3/4) Add stock at the branch, through the ERP's own stock setting.
    # 🐛 [FIX]: the quantity was written straight in — no ledger entry (the books' inventory drifted
    # from the shelf with every intake) and no average cost weighed in, and the movement came out as
    # an unexplained «manual edit». set_branch_stock does all three, as the product screen's edit
    # and the voice's «ضيف للمخزون» (entries.record_stock) do.
    from inventory.services.inventory_service import set_branch_stock

    here = (Inventory.objects.select_for_update().filter(product=product, branch=branch)
            .values_list("quantity", flat=True).first() or 0)
    set_branch_stock(product, branch, here + int(quantity), user=getattr(employee, "user", None),
                     reference_type="RobotIntake", note="🤖 إدخال بضاعة بالصورة")
    inv = Inventory.objects.get(product=product, branch=branch)

    # 5) Learn + audit — including a VISUAL fingerprint so the same physical
    #    part can be re-identified from a photo next time, not just its code.
    fp_hash, fp_details = _image_fingerprint(image_bytes, product) if image_bytes else ("", {})
    services.learn_from_confirmation(
        product=product, code=part_number, label=name,
        fingerprint_hash=fp_hash, details=fp_details, employee=employee,
    )
    RobotScanEvent.objects.create(
        device=device, purpose="intake",
        recognized_label=name, recognized_part_number=part_number,
        confidence=1.0, product=product, created_by=employee,
        quantity_added=int(quantity), created_new_product=created_new,
    )

    # Low-stock is unlikely right after intake, but keep the ecosystem in sync.
    services.maybe_raise_procurement_signal(device=device, product=product, branch=branch)

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


def _image_fingerprint(image_bytes: bytes, product=None):
    """Return (avg_hash_hex, details) for visual re-identification learning.

    The key is a perceptual average-hash (8x8 grayscale, thresholded at the
    mean) — near-identical photos of the same part yield the same hash, so
    `resolve_from_knowledge(fingerprint_hash=...)` re-recognizes it. `details`
    additionally carries the ERP's richer Gemini fingerprint when available.
    """
    ahash = services.image_hash(image_bytes)
    details = {}
    try:
        from .. import vision
        details = vision.fingerprint(image_bytes) or {}
    except Exception:
        details = {}
    return ahash, details
