"""🧾 خدمات الروبوت — البيع (فاتورة بسعر القطاعي) وإشارة الشراء لما المخزون يقل."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import logging

from django.db import transaction
from django.utils import timezone

from robot import services  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from .offline import _open_rfq_for

logger = logging.getLogger("mouss_tec_core")


# ---------------------------------------------------------------------------
# POS: create a sale invoice from a scan (RETAIL price enforced explicitly)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_robot_sale(*, product, branch, customer, employee=None,
                      quantity: int = 1, unit_price: Optional[Decimal] = None,
                      payment: str = "cash", post: bool = True):
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
    if unit_price is None:
        unit_price = Decimal(str(product.retail_price or 0))
    return create_robot_sale_lines(lines=[(product, quantity, unit_price)], branch=branch,
                                   customer=customer, payment=payment, post=post)


def create_robot_sale_lines(*, lines, branch, customer, payment: str = "cash",
                            post: bool = True, notes: str = ""):
    """`create_robot_sale` for several parts on one invoice: `lines` =
    [(product, quantity, unit_price)], each price explicit (retail — never
    left blank for the item to auto-fill wholesale). Same posting order."""
    from inventory.models import SaleInvoice, SaleInvoiceItem

    # A cash sale with no cash treasury would post as unpaid while the
    # customer has paid: refuse before anything is written.
    treasury = None
    if is_cash_payment(payment):
        treasury = services._cash_treasury(branch)
        if treasury is None:
            raise ValueError(f"no active cash treasury for branch {branch}")

    # 1) Draft first (status defaults to 'quotation'; invoice_type must be 'sale').
    invoice = SaleInvoice.objects.create(
        invoice_type="sale",
        status="quotation",
        customer=customer,
        branch=branch,
        sales_channel="in_store",  # robot is in-store (in_store/website/marketplace)
        **({"notes": notes} if notes else {}),
    )
    # 2) Add the lines at the explicit RETAIL price (never auto-filled wholesale).
    for product, quantity, unit_price in lines:
        SaleInvoiceItem.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_price=Decimal(str(unit_price)),
        )
    invoice.update_total()

    # 🐛 [FIX]: طلب لقطعة لسه جاية (allow_backorder) كان بيتعتمد زي البيع —
    #    والاعتماد بيخصم مخزون مش موجود فبيرمي خطأ والطلب كله بيقع بـ 500.
    #    الطلب بيتسجّل عرض سعر/أوردر (من غير خصم ولا تحصيل) لحد ما القطعة توصل.
    if not post:
        invoice.notes = "🤖 طلب من الروبوت — القطعة مش متوفرة حالياً (أوردر)."
        invoice.save(update_fields=["notes"])
        invoice.refresh_from_db()
        return invoice

    # 2b) Payment: "cash" settles the full amount into the branch cash treasury
    #     now (so the drawer/ledger is accurate); "credit" leaves it due (آجل).
    #     The execute_sale posting reads treasury + paid_amount to record the
    #     FinancialTransaction, so set them BEFORE flipping to posted.
    if is_cash_payment(payment):
        invoice.treasury = treasury
        invoice.paid_amount = invoice.total_amount
        invoice.save(update_fields=["treasury", "paid_amount"])

    # 3) Post — NOW the signal deducts stock/accrues (and settles cash) with the
    #    item present.
    invoice.status = "posted"
    invoice.save(update_fields=["status"])
    invoice.refresh_from_db()
    return invoice


def is_cash_payment(payment) -> bool:
    return str(payment).strip().lower() in ("cash", "كاش", "نقدي", "نقدا")


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
    from ..models import ProcurementSignal

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
