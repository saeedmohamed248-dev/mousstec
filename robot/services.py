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

from datetime import timedelta
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
    # Taught mappings first (exact), then the catalogue, then any shop-slang
    # alias staff taught that appears inside the sentence.
    product = (resolve_from_knowledge(code=query, label=query)
               or find_product(query)
               or learned_alias_in(query))
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

    # 2b) Payment: "cash" settles the full amount into the branch cash treasury
    #     now (so the drawer/ledger is accurate); "credit" leaves it due (آجل).
    #     The execute_sale posting reads treasury + paid_amount to record the
    #     FinancialTransaction, so set them BEFORE flipping to posted.
    if str(payment).lower() in ("cash", "كاش", "نقدي", "نقدا"):
        treasury = _cash_treasury(branch)
        if treasury is not None:
            invoice.treasury = treasury
            invoice.paid_amount = invoice.total_amount
            invoice.save(update_fields=["treasury", "paid_amount"])

    # 3) Post — NOW the signal deducts stock/accrues (and settles cash) with the
    #    item present.
    invoice.status = "posted"
    invoice.save(update_fields=["status"])
    invoice.refresh_from_db()
    return invoice


def _cash_treasury(branch):
    """The branch's active cash treasury (for settling a cash sale), or None."""
    try:
        from inventory.models import Treasury
        return (Treasury.objects
                .filter(branch=branch, type="cash", is_active=True)
                .order_by("id")
                .first())
    except Exception:
        return None


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
    signal, created = ProcurementSignal.objects.get_or_create(
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

    # Multi-agent hand-off: open a real RFQ so the Procurement Agent / vendors
    # can quote it — the robot triggers procurement, it doesn't order directly.
    # Only on first raise, and only if there isn't already an open RFQ for it.
    if created:
        rfq_id = _open_rfq_for(product, branch, reorder)
        if rfq_id:
            signal.manifest_payload["rfq_id"] = rfq_id
            signal.save(update_fields=["manifest_payload"])
    return signal


# What each actuator can physically do (mirrors the Mega's handleCommand).
MOTOR_DIRECTIONS = {
    "head": {"left", "right", "stop"},
    "arm_left": {"up", "down", "stop"},
    "arm_right": {"up", "down", "stop"},
    "track": {"forward", "backward", "stop"},
}
# Longest single move, matching the Mega's DEFAULT_PULSE_CAP. A move always
# ends on its own: "0 = run until stop" would run a window motor into its end
# stop and stall it (burning the motor and relay), and a lost stop frame would
# leave the tracks driving.
MOTOR_MAX_MS = 5000
MOTOR_DEFAULT_MS = 500


def validate_motor_command(actuator, direction, duration_ms=None):
    """Normalize a requested move → (actuator, direction, duration_ms).

    Raises ValueError with a spoken-friendly reason for anything the hardware
    can't do. `stop` always has duration 0; any other move is clamped to
    1..MOTOR_MAX_MS (missing/0 → MOTOR_DEFAULT_MS).
    """
    actuator = str(actuator or "").strip().lower()
    direction = str(direction or "").strip().lower()
    if actuator not in MOTOR_DIRECTIONS:
        raise ValueError(f"جزء غير معروف: {actuator or '—'}")
    if direction not in MOTOR_DIRECTIONS[actuator]:
        raise ValueError(f"الاتجاه «{direction or '—'}» غير متاح لـ {actuator}")
    if direction == "stop":
        return actuator, direction, 0
    try:
        ms = int(float(duration_ms)) if duration_ms not in (None, "") else 0
    except (TypeError, ValueError):
        raise ValueError("مدة الحركة غير صالحة")
    if ms <= 0:
        ms = MOTOR_DEFAULT_MS
    return actuator, direction, min(ms, MOTOR_MAX_MS)


# A device silent for this long counts as offline (the dashboard's "online"
# dot uses 90s; alerts wait longer so a Wi-Fi blip doesn't page anyone).
OFFLINE_AFTER_SECONDS = 5 * 60


def note_device_seen(device, now=None):
    """Record that the device just talked to us; alert if it was offline.

    Returns the `back_online` RobotAlert when this call ends an outage, else
    None. Called on every authenticated device request, before `last_seen_at`
    is overwritten.
    """
    from .models import RobotAlert

    now = now or timezone.now()
    last = device.last_seen_at
    if last is None or (now - last).total_seconds() < OFFLINE_AFTER_SECONDS:
        return None
    gone = int((now - last).total_seconds() // 60)
    return RobotAlert.objects.create(
        device=device, kind="back_online",
        message=f"✅ الروبوت رجع أونلاين بعد انقطاع حوالي {gone} دقيقة.",
    )


def raise_offline_alerts(now=None) -> int:
    """Alert once per outage for every active device that went silent.

    Run periodically (see `robot.tasks`). An outage is alerted once: we skip a
    device that already has an `offline` alert newer than its last contact.
    """
    from .models import RobotAlert, RobotDevice

    now = now or timezone.now()
    cutoff = now - timedelta(seconds=OFFLINE_AFTER_SECONDS)
    raised = 0
    for device in RobotDevice.objects.filter(is_active=True, last_seen_at__lt=cutoff):
        already = RobotAlert.objects.filter(
            device=device, kind="offline", created_at__gte=device.last_seen_at,
        ).exists()
        if already:
            continue
        RobotAlert.objects.create(
            device=device, kind="offline",
            message=(f"📡 الروبوت {device.name} فقد الاتصال — آخر اتصال "
                     f"{timezone.localtime(device.last_seen_at):%Y-%m-%d %H:%M}."),
        )
        raised += 1
    return raised


def look_at(device, offset: float, *, issued_by=None):
    """Turn the robot's head toward a target at horizontal `offset` ∈ [-1,1].

    offset < 0 → target is to the LEFT, > 0 → RIGHT, ~0 → centered (no move).
    Emits a head MotorCommandLog whose duration is proportional to how far
    off-center the target is; the ESP32 picks it up from /motor/pending/ and the
    Mega pans the head. Used to face the customer/speaker.
    """
    from .models import MotorCommandLog

    try:
        offset = max(-1.0, min(1.0, float(offset)))
    except (TypeError, ValueError):
        return None
    if abs(offset) < 0.12:  # dead-zone: already looking at them
        return None
    direction = "right" if offset > 0 else "left"
    duration_ms = int(min(abs(offset), 1.0) * 700)  # up to ~0.7s pan
    return MotorCommandLog.objects.create(
        device=device, actuator="head", direction=direction,
        duration_ms=duration_ms, issued_by=issued_by,
    )


# ---------------------------------------------------------------------------
# After-hours guard
# ---------------------------------------------------------------------------

def is_after_hours(device, now=None) -> bool:
    """True if `now` (local) falls in the device's guard window (shop closed).

    Uses the device's explicit guard_from/guard_to when set. When they're not,
    falls back to a sane default night window (20:00–08:00) so guarding is armed
    out of the box rather than silently off.
    """
    now = now or timezone.localtime()
    t = now.time()
    start = device.guard_from
    end = device.guard_to
    if start is None or end is None:
        start = start or _dt_time(20, 0)
        end = end or _dt_time(8, 0)
    if start <= end:
        return start <= t <= end
    # Window crosses midnight (e.g. 20:00 → 08:00).
    return t >= start or t <= end


def _dt_time(h, m):
    from datetime import time as _t
    return _t(h, m)


def raise_low_battery_alert(device, percent):
    """Raise a low-battery alert, deduped to one per 30 minutes per device."""
    from .models import RobotAlert
    recent = (RobotAlert.objects
              .filter(device=device, kind="low_battery",
                      created_at__gte=timezone.now() - timedelta(minutes=30))
              .exists())
    if recent:
        return None
    return RobotAlert.objects.create(
        device=device, kind="low_battery",
        message=f"🔋 بطارية الروبوت منخفضة ({percent}%) — محتاجة شحن.",
    )


@transaction.atomic
def raise_after_hours_alert(device, *, snapshot=None, message=""):
    """Create an after-hours motion alert (deduped to one per 5 minutes)."""
    from .models import RobotAlert

    recent = (RobotAlert.objects
              .filter(device=device, kind="after_hours_motion",
                      created_at__gte=timezone.now() - timedelta(minutes=5))
              .exists())
    if recent:
        return None
    return RobotAlert.objects.create(
        device=device, kind="after_hours_motion", snapshot=snapshot,
        message=message or "🚨 حركة مرصودة بعد غلق المحل!",
    )


# ---------------------------------------------------------------------------
# Offline sync — catalog for the robot to work on when the net is down
# ---------------------------------------------------------------------------

def offline_catalog(branch, *, limit: int = 5000) -> dict:
    """A compact, RETAIL-ONLY snapshot the robot caches to work offline.

    Parts (name, codes, retail price, branch stock) + known customer/face hashes
    so it can still answer stock/price and recognize people without the network.
    NEVER includes wholesale/cost — same guarantee as every robot-facing payload.
    """
    from inventory.models import Inventory

    parts = []
    inv_rows = (Inventory.objects.filter(branch=branch)
                .select_related("product")[:limit])
    for inv in inv_rows:
        p = inv.product
        parts.append({
            "part_number": p.part_number,
            "name": p.name,
            "oem": getattr(p, "oem_cross_reference", None) or [],
            "retail_price": float(p.retail_price or 0),
            "stock": inv.quantity,
        })
    # What staff have taught the robot (shop slang / read codes → part), so it
    # keeps understanding those words offline too.
    from .models import RobotKnowledge
    aliases = [
        {"kind": kind, "key": key, "part_number": pn}
        for kind, key, pn in (RobotKnowledge.objects
                              .filter(key_kind__in=("code", "label"))
                              .order_by("-hit_count")
                              .values_list("key_kind", "key_value", "product__part_number")[:limit])
    ]
    return {
        "branch": branch.name,
        "generated_at": timezone.now().isoformat(),
        "parts": parts,
        "part_count": len(parts),
        "aliases": aliases,
    }


@transaction.atomic
def apply_offline_events(device, events: list) -> dict:
    """Replay a batch of offline events idempotently (by client_uid).

    Each event: {client_uid, kind, payload}. Supported kinds:
      * learn      — payload {code|label, part_number}  → learn_from_confirmation
      * count      — payload {session? , query, counted_qty} (best-effort log)
    Unknown kinds are stored but not applied. Duplicates (same client_uid) are
    skipped so a retried upload never double-applies.
    """
    from inventory.models import Product
    from .models import RobotSyncEvent

    applied, skipped, failed = 0, 0, 0
    count_session = None
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        uid = str(ev.get("client_uid") or "").strip()[:64]
        if not uid:
            continue
        kind = str(ev.get("kind") or "")[:32]
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        row, created = RobotSyncEvent.objects.get_or_create(
            device=device, client_uid=uid,
            defaults={"kind": kind, "payload": payload},
        )
        if not created and row.applied:
            skipped += 1
            continue
        try:
            # A savepoint per event: one bad event rolls back only itself.
            # Catching a DB error without one would leave the whole batch's
            # transaction broken and fail every event after it.
            with transaction.atomic():
                if kind == "learn":
                    pn = (payload.get("part_number") or "").strip()
                    product = find_product(pn) if pn else None
                    if product:
                        learn_from_confirmation(
                            product=product,
                            code=payload.get("code", ""),
                            label=payload.get("label", ""),
                        )
                elif kind == "count":
                    # Counts taken offline land in one stock-take per sync
                    # batch. Like any robot count it only proposes numbers: a
                    # manager still approves the adjustment from the dashboard.
                    query = str(payload.get("query") or "").strip()
                    qty = int(payload.get("counted_qty"))
                    if query and qty >= 0:
                        if count_session is None:
                            count_session = start_stock_take(
                                device, device.branch,
                                instruction="جرد أوفلاين (مزامنة)",
                            )
                        add_stock_take_count(count_session, query=query, counted_qty=qty)
                row.applied = True
                row.save(update_fields=["applied"])
            applied += 1
        except Exception:
            # Leave unapplied so a later sync can retry.
            failed += 1
    report = None
    if count_session is not None:
        report = complete_stock_take(count_session)
    return {"applied": applied, "skipped_duplicates": skipped,
            "failed": failed, "stock_take": report}


def _open_rfq_for(product, branch, quantity: int):
    """Open an RFQ for a low-stock part, or return None. Deduplicates on open."""
    try:
        from inventory.models import RFQ
    except Exception:
        return None
    existing = RFQ.objects.filter(
        product=product, branch=branch, status=RFQ.STATUS_OPEN,
    ).first()
    if existing:
        return existing.id
    rfq = RFQ.objects.create(
        branch=branch,
        product=product,
        part_number_requested=product.part_number,
        part_name_requested=product.name,
        quantity=max(int(quantity), 1),
        notes="🤖 طلب تلقائي من الروبوت — المخزون وصل حد التنبيه.",
        status=RFQ.STATUS_OPEN,
    )
    return rfq.id


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
    has been taught once is recognized instantly next time. A fingerprint with
    no exact entry falls back to the nearest learned one (see
    `match_fingerprint`), since two photos of the same part rarely hash equal.
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
    if fingerprint_hash:
        product, _distance = match_fingerprint(fingerprint_hash)
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
    from .models import RobotKnowledge

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
            return image_hash(f.read())
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
    from .models import RobotKnowledge

    norm = _normalize_key(text)
    if len(norm) < 3:
        return None
    rows = (RobotKnowledge.objects.filter(key_kind="label")
            .order_by("-hit_count")
            .values_list("key_value", "product_id")[:_KNOWLEDGE_SCAN_LIMIT])
    best_key, best_pid = "", None
    for key, product_id in rows:
        if len(key) >= 3 and key in norm and len(key) > len(best_key):
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
    from .models import RobotKnowledge

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
    fp = scan_fingerprint(event)
    code = event.recognized_part_number or ""
    label = event.recognized_label or ""
    wrong = event.product
    forgotten = 0
    if wrong is not None and wrong.pk != product.pk:
        forgotten = unlearn(product=wrong, code=code, label=label, fingerprint_hash=fp)
    learn_from_confirmation(
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

    # 5) Learn + audit — including a VISUAL fingerprint so the same physical
    #    part can be re-identified from a photo next time, not just its code.
    fp_hash, fp_details = _image_fingerprint(image_bytes, product) if image_bytes else ("", {})
    learn_from_confirmation(
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


def _image_fingerprint(image_bytes: bytes, product=None):
    """Return (avg_hash_hex, details) for visual re-identification learning.

    The key is a perceptual average-hash (8x8 grayscale, thresholded at the
    mean) — near-identical photos of the same part yield the same hash, so
    `resolve_from_knowledge(fingerprint_hash=...)` re-recognizes it. `details`
    additionally carries the ERP's richer Gemini fingerprint when available.
    """
    ahash = image_hash(image_bytes)
    details = {}
    try:
        from . import vision
        details = vision.fingerprint(image_bytes) or {}
    except Exception:
        details = {}
    return ahash, details


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


def get_open_stock_take(device):
    """The device's currently-open stock-take session (last 2 hours), or None."""
    from .models import RobotStockTakeSession
    since = timezone.now() - timedelta(hours=2)
    return (RobotStockTakeSession.objects
            .filter(device=device, status="open", created_at__gte=since)
            .order_by("-created_at")
            .first())


def start_stock_take(device, branch, *, instruction="", employee=None):
    """Open a new conversational stock-take session for the device."""
    from .models import RobotStockTakeSession
    return RobotStockTakeSession.objects.create(
        device=device, branch=branch, instruction=instruction,
        status="open", started_by=employee,
    )


def add_stock_take_count(session, *, query: str, counted_qty: int):
    """Add/replace one counted line in an open session. Returns (line, product)."""
    from inventory.models import Inventory
    from .models import RobotStockTakeLine

    product = resolve_from_knowledge(code=query, label=query) or find_product(query)
    if product is None:
        return None, None
    inv = Inventory.objects.filter(product=product, branch=session.branch).first()
    expected = inv.quantity if inv else 0
    line, _created = RobotStockTakeLine.objects.update_or_create(
        session=session, product=product,
        defaults={"expected_qty": expected, "counted_qty": int(counted_qty)},
    )
    return line, product


def complete_stock_take(session):
    """Close a session and return its reconciliation report."""
    session.status = "completed"
    session.completed_at = timezone.now()
    session.save(update_fields=["status", "completed_at"])
    lines = list(session.lines.select_related("product"))
    return {
        "session_id": session.id,
        "counted_items": len(lines),
        "matches": sum(1 for ln in lines if ln.matched),
        "variances": [
            {"product": ln.product.name, "expected": ln.expected_qty,
             "counted": ln.counted_qty, "variance": ln.variance}
            for ln in lines if not ln.matched
        ],
    }


@transaction.atomic
def apply_stock_take(session, *, employee=None):
    """Apply a completed stock-take: correct Inventory to the counted numbers.

    Each variance line sets the branch on-hand to the counted quantity; the
    existing inventory signal records the movement (reason 'adjustment' via the
    manual path). Idempotent — a session already 'applied' is a no-op.
    """
    from inventory.models import Inventory

    if session.status == "applied":
        return {"applied": False, "reason": "already applied"}
    # Only a finished count may move stock: an open session is still being
    # counted (half the shelf would be "corrected" to zero), and a cancelled
    # one was thrown away on purpose.
    if session.status != "completed":
        return {"applied": False, "reason": f"session is {session.status}, not completed"}

    adjusted = 0
    for line in session.lines.select_related("product"):
        if line.matched:
            continue
        inv, _created = Inventory.objects.select_for_update().get_or_create(
            product=line.product, branch=session.branch, defaults={"quantity": 0},
        )
        inv.quantity = int(line.counted_qty)
        inv.save(update_fields=["quantity"])
        adjusted += 1

    session.status = "applied"
    session.save(update_fields=["status"])
    return {"applied": True, "adjusted_lines": adjusted}


def parse_count_utterance(text: str):
    """Parse "<part name/code> <number>" from a spoken count, e.g. "كنترول ٣".

    Returns (query, qty) or (None, None). Handles Arabic-Indic digits and a
    trailing integer.
    """
    import re
    if not text:
        return None, None
    # Normalize Arabic-Indic digits to ASCII.
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    t = text.translate(trans).strip()
    m = re.search(r"(.+?)\s*(\d+)\s*$", t)
    if not m:
        return None, None
    query = m.group(1).strip(" :،,-")
    try:
        qty = int(m.group(2))
    except ValueError:
        return None, None
    return (query or None), qty


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
