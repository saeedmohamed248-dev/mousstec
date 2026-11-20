"""📋 خدمات الروبوت — الجرد بالصوت."""

from __future__ import annotations

from datetime import timedelta

import logging

from django.db import transaction
from django.utils import timezone

from robot import services  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from .learning import resolve_from_knowledge

logger = logging.getLogger("mouss_tec_core")


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
    from ..models import RobotStockTakeSession, RobotStockTakeLine

    session = RobotStockTakeSession.objects.create(
        device=device, branch=branch, instruction=instruction,
        status="completed", started_by=employee, completed_at=timezone.now(),
    )
    lines, unresolved = [], []
    for row in counts or []:
        product = resolve_from_knowledge(code=row.get("query", ""), label=row.get("query", ""))
        if product is None:
            product = services.find_product(row.get("query", ""))
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
    from ..models import RobotStockTakeSession
    since = timezone.now() - timedelta(hours=2)
    return (RobotStockTakeSession.objects
            .filter(device=device, status="open", created_at__gte=since)
            .order_by("-created_at")
            .first())


def start_stock_take(device, branch, *, instruction="", employee=None):
    """Open a new conversational stock-take session for the device."""
    from ..models import RobotStockTakeSession
    return RobotStockTakeSession.objects.create(
        device=device, branch=branch, instruction=instruction,
        status="open", started_by=employee,
    )


def add_stock_take_count(session, *, query: str, counted_qty: int):
    """Add/replace one counted line in an open session. Returns (line, product)."""
    from inventory.models import Inventory
    from ..models import RobotStockTakeLine

    product = resolve_from_knowledge(code=query, label=query) or services.find_product(query)
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

    from inventory.models import InventoryMovement
    from inventory.services.accounting_service import AccountingService

    adjusted = 0
    for line in session.lines.select_related("product"):
        if line.matched:
            continue
        inv, _created = Inventory.objects.select_for_update().get_or_create(
            product=line.product, branch=session.branch, defaults={"quantity": 0},
        )
        before = inv.quantity
        inv.quantity = int(line.counted_qty)
        inv.save(update_fields=["quantity"])
        diff = inv.quantity - before
        if diff:
            InventoryMovement.objects.create(
                product=line.product, branch=session.branch, reason="adjustment",
                quantity_change=diff, quantity_before=before, quantity_after=inv.quantity,
                reference_type="RobotStockTake", reference_id=session.pk,
                note=f"جرد الروبوت #{session.pk}",
                created_by=getattr(employee, "user", None),
            )
            # 📒 العجز/الزيادة في الجرد بيتقيّد على المخزون — من غيره حساب
            #    المخزون في الدفتر بيبعد عن البضاعة الفعلية بعد كل جرد.
            try:
                with transaction.atomic():
                    AccountingService.post_stock_adjustment(
                        product=line.product, quantity_change=diff, branch=session.branch,
                        reference=f"STOCKTAKE-{session.pk}-{line.product_id}",
                        description=f"فرق جرد الروبوت #{session.pk} — {line.product.name} ({diff:+d})",
                    )
            except Exception:  # the ledger never blocks a stock correction
                logger.exception("robot: stock-take ledger posting failed")
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
