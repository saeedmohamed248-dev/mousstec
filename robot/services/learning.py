"""📚 خدمات الروبوت — ذاكرة التعلّم: بصمة الصورة، الأسماء المتعلّمة، تصحيح المسح."""

from __future__ import annotations


import logging

from django.db import transaction

from robot import services  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from .diagnostics import _normalize_key

logger = logging.getLogger("mouss_tec_core")


# ---------------------------------------------------------------------------
# Learning memory — the robot gets better at recognizing parts with use
# ---------------------------------------------------------------------------

def resolve_from_knowledge(*, code: str = "", label: str = "", fingerprint_hash: str = ""):
    """Return a previously-confirmed Product for a code/label/fingerprint, or None.

    Checked before falling back to a fresh catalogue search, so a part the robot
    has been taught once is recognized instantly next time. A fingerprint with
    no exact entry falls back to the nearest learned one (see
    `match_fingerprint`), since two photos of the same part rarely hash equal.
    """
    from ..models import RobotKnowledge

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
    if fingerprint_hash:
        product, _distance = services.match_fingerprint(fingerprint_hash)
        return product
    return None


# Max differing bits (of 64) for two average-hashes to count as the same part.
# Re-shooting the same part on the same counter moves a handful of bits; a
# different part moves far more. Kept tight because a wrong match names the
# wrong part — the scan flow still asks a human to confirm fingerprint hits.
_FINGERPRINT_MAX_DISTANCE = 5

# How many learned rows a near-match or alias scan looks at (most-confirmed
# first), so a large knowledge table can't turn one request into a full scan.
_KNOWLEDGE_SCAN_LIMIT = 5000


def _hamming(a_hex: str, b_hex: str) -> int:
    """Differing bits between two hex hashes (64 when either is unreadable)."""
    try:
        return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def match_fingerprint(fingerprint_hash: str):
    """Nearest learned product for a visual hash → (product, distance).

    Returns (None, None) when nothing is within `_FINGERPRINT_MAX_DISTANCE`.
    Ties go to the mapping humans confirmed most often.
    """
    from ..models import RobotKnowledge

    target = _normalize_key(fingerprint_hash)
    if not target:
        return None, None
    rows = (RobotKnowledge.objects.filter(key_kind="fingerprint")
            .order_by("-hit_count")
            .values_list("key_value", "product_id", "hit_count")[:_KNOWLEDGE_SCAN_LIMIT])
    best = None  # (distance, -hits, product_id)
    for key, product_id, hits in rows:
        d = _hamming(target, key)
        if d > _FINGERPRINT_MAX_DISTANCE:
            continue
        cand = (d, -(hits or 0), product_id)
        if best is None or cand < best:
            best = cand
    if best is None:
        return None, None
    from inventory.models import Product
    return Product.objects.filter(pk=best[2]).first(), best[0]


def nearest_fingerprint_key(fingerprint_hash: str, product) -> str:
    """The learned hash of `product` closest to `fingerprint_hash` (within the
    match radius), or '' — the key a look-alone match actually came from."""
    from ..models import RobotKnowledge

    target = _normalize_key(fingerprint_hash)
    if not target or product is None:
        return ""
    best_key, best_d = "", _FINGERPRINT_MAX_DISTANCE + 1
    for key in (RobotKnowledge.objects
                .filter(key_kind="fingerprint", product=product)
                .values_list("key_value", flat=True)[:_KNOWLEDGE_SCAN_LIMIT]):
        d = _hamming(target, key)
        if d < best_d:
            best_key, best_d = key, d
    return best_key


def image_hash(image_bytes: bytes) -> str:
    """64-bit average-hash (hex) of an image — the visual learning key, or ''."""
    if not image_bytes:
        return ""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((8, 8))
        px = list(img.getdata())
        avg = sum(px) / len(px)
        bits = "".join("1" if p >= avg else "0" for p in px)
        return f"{int(bits, 2):016x}"
    except Exception:
        return ""


def scan_fingerprint(event) -> str:
    """Visual hash of a stored scan's photo ('' when it has none)."""
    if not getattr(event, "image", None):
        return ""
    try:
        f = event.image.open("rb")
        try:
            return services.image_hash(f.read())
        finally:
            f.close()
    except Exception:
        return ""


def learned_alias_in(text: str):
    """Product whose taught name/alias appears inside a spoken sentence.

    Staff teach the robot shop slang ("الطرمبة" → a water-pump SKU); customers
    then say it inside a longer question, so we look for the longest learned
    label contained in the sentence rather than an exact match. None if no
    alias of 3+ characters appears.
    """
    from ..models import RobotKnowledge

    import re as _re
    # Whole words only ("abs" must not match inside "tabs"); an Arabic
    # article/conjunction prefix is allowed ("طرمبة" matches "الطرمبة").
    norm = " " + " ".join(_re.sub(r"[^\w\s]", " ", _normalize_key(text)).split()) + " "
    if len(norm.strip()) < 3:
        return None
    rows = (RobotKnowledge.objects.filter(key_kind="label")
            .order_by("-hit_count")
            .values_list("key_value", "product_id")[:_KNOWLEDGE_SCAN_LIMIT])
    best_key, best_pid = "", None
    for key, product_id in rows:
        if len(key) < 3 or len(key) <= len(best_key):
            continue
        pattern = r"\s(?:ال|وال|بال|لل|و|ب)?" + _re.escape(key) + r"\s"
        if _re.search(pattern, norm):
            best_key, best_pid = key, product_id
    if best_pid is None:
        return None
    from inventory.models import Product
    return Product.objects.filter(pk=best_pid).first()


def unlearn(*, product, code: str = "", label: str = "", fingerprint_hash: str = ""):
    """A human said this code/label/look is NOT `product`: weaken that memory.

    Each correction takes one confirmation off the wrong mapping and forgets it
    entirely at zero, so a single mistaken confirmation can't keep mislabeling
    a part forever. Returns how many mappings were weakened or removed.
    """
    from ..models import RobotKnowledge

    touched = 0
    for kind, val in (("code", code), ("label", label), ("fingerprint", fingerprint_hash)):
        val = _normalize_key(val)
        if not val:
            continue
        entry = RobotKnowledge.objects.filter(
            key_kind=kind, key_value=val, product=product,
        ).first()
        if entry is None:
            continue
        if (entry.hit_count or 0) <= 1:
            entry.delete()
        else:
            entry.hit_count -= 1
            entry.save(update_fields=["hit_count", "updated_at"])
        touched += 1
    return touched


@transaction.atomic
def teach_scan_correction(event, *, product, employee=None) -> dict:
    """Staff correct a scan: "this photo is really THAT part".

    Weakens whatever the robot wrongly guessed for this scan's code/label/look,
    learns the right mapping, and re-points the scan at the right product — so
    the same mistake gets less likely every time someone fixes it.
    """
    fp = services.scan_fingerprint(event)
    code = event.recognized_part_number or ""
    label = event.recognized_label or ""
    wrong = event.product
    forgotten = 0
    if wrong is not None and wrong.pk != product.pk:
        # A look-alone guess came from a NEARBY learned hash, stored under a
        # different key than this photo's — weaken that one.
        wrong_fp = services.nearest_fingerprint_key(fp, wrong) or fp
        forgotten = services.unlearn(product=wrong, code=code, label=label, fingerprint_hash=wrong_fp)
    services.learn_from_confirmation(
        product=product, code=code, label=label, fingerprint_hash=fp,
        employee=employee, details={"source": "correction", "scan_id": event.pk},
    )
    event.product = product
    event.save(update_fields=["product"])
    return {
        "ok": True,
        "scan_id": event.pk,
        "product_id": product.pk,
        "name": product.name,
        "was": getattr(wrong, "name", None),
        "forgot_wrong_mappings": forgotten,
    }


def learn_from_confirmation(*, product, code: str = "", label: str = "",
                            fingerprint_hash: str = "", details: dict = None,
                            employee=None):
    """Record that a human confirmed code/label/fingerprint → product.

    Idempotent per (kind, value, product): repeat confirmations bump `hit_count`
    (rising confidence) instead of duplicating rows.
    """
    from ..models import RobotKnowledge

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
