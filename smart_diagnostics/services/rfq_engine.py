"""
📩 RFQ Engine — fan a part request out to multiple suppliers via
   WhatsApp deep-links, persist the trail as RFQ + RFQQuote rows.

Flow:
    1. `top_vendors_for_product(product, limit=3)` — rank vendors by past
       purchase frequency (recency-weighted) of the same product. Falls
       back to overall most-active vendors if the product is new.
    2. `create_rfq(...)` — atomic create of one `RFQ` + N `RFQQuote`
       skeletons (one per chosen vendor) so the inventory manager has a
       persistent ledger of who was asked.
    3. `build_whatsapp_messages(rfq)` — returns per-vendor wa.me URLs
       with a pre-filled professional Arabic body. Phone numbers
       normalised to E.164 (EG default).
    4. `accept_quote(quote)` — promotes a winning quote to a draft
       `PurchaseInvoice`, links it back, and stamps the RFQ as ORDERED.

Why we don't auto-create the PO at RFQ time: workshops want to compare
prices BEFORE committing to one supplier. The draft PO is created only
when a quote is explicitly accepted.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max

logger = logging.getLogger("mouss_tec_core")

_MAX_VENDORS_PER_RFQ = 5
_DEFAULT_VENDOR_COUNT = 3


def top_vendors_for_product(product, limit: int = _DEFAULT_VENDOR_COUNT):
    """Rank by `recent purchase activity` of the same product:
        score = (count of past purchases of this product) +
                bonus if the most recent purchase is within 90 days.

    Falls back to overall most-active vendors when the product has
    never been purchased here before.
    """
    from inventory.models import PurchaseInvoiceItem, Vendor
    from django.utils import timezone
    from datetime import timedelta

    if product is None:
        # No catalogue match → just return the most-active vendors overall.
        return list(
            Vendor.objects.annotate(
                purchases=Count('purchaseinvoice'),
            ).order_by('-purchases', 'name')[:limit]
        )

    cutoff = timezone.now() - timedelta(days=90)
    ranked = (
        PurchaseInvoiceItem.objects
        .filter(product=product)
        .values('invoice__vendor_id')
        .annotate(
            n=Count('id'),
            last_seen=Max('invoice__date_created'),
        )
        .order_by('-n', '-last_seen')[:limit]
    )
    vendor_ids = [r['invoice__vendor_id'] for r in ranked]
    vendors = list(Vendor.objects.filter(id__in=vendor_ids))
    # Preserve the ranking order
    by_id = {v.id: v for v in vendors}
    vendors = [by_id[vid] for vid in vendor_ids if vid in by_id]

    # Top up with overall-active vendors if we still need more
    if len(vendors) < limit:
        missing = limit - len(vendors)
        existing_ids = {v.id for v in vendors}
        extras = (
            Vendor.objects.exclude(id__in=existing_ids)
            .annotate(n_total=Count('purchaseinvoice'))
            .order_by('-n_total', 'name')[:missing]
        )
        vendors.extend(extras)

    return vendors


def _normalise_phone(raw: str) -> str:
    """Normalise EG phone to E.164 digits (no '+', wa.me wants raw digits)."""
    if not raw:
        return ''
    digits = re.sub(r'[\s\-\(\)+]+', '', raw)
    if digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('0'):
        digits = '20' + digits[1:]   # EG default
    return digits


def create_rfq(
    *,
    branch,
    product,
    part_number_requested: str,
    part_name_requested: str = '',
    quantity: int = 1,
    job_card=None,
    requested_by=None,
    vendors=None,
    notes: str = '',
):
    """Atomic create. Returns the `RFQ` with `quotes.all()` ready to read."""
    from inventory.models import RFQ, RFQQuote

    if quantity < 1 or quantity > 999:
        quantity = 1
    pn = (part_number_requested or '').strip().upper()
    if not pn:
        raise ValueError("part_number_requested is required")

    if not vendors:
        vendors = top_vendors_for_product(product, limit=_DEFAULT_VENDOR_COUNT)
    vendors = vendors[:_MAX_VENDORS_PER_RFQ]

    with transaction.atomic():
        rfq = RFQ.objects.create(
            job_card=job_card,
            branch=branch,
            product=product,
            part_number_requested=pn,
            part_name_requested=(part_name_requested or '').strip()[:200],
            quantity=quantity,
            requested_by=requested_by,
            notes=notes.strip()[:1000],
        )
        for v in vendors:
            # de-dupe in case the caller passed the same vendor twice
            RFQQuote.objects.get_or_create(rfq=rfq, vendor=v)

    logger.info(
        "[RFQ] tenant_local: branch=%s pn=%s qty=%s vendors=%s rfq=%s",
        getattr(branch, 'id', None), pn, quantity,
        [v.id for v in vendors], rfq.id,
    )
    return rfq


def build_whatsapp_messages(rfq, workshop_name: str = '') -> list[dict]:
    """One per vendor — returns list of {vendor_id, vendor_name, phone, wa_url, can_send}."""
    out = []
    for q in rfq.quotes.select_related('vendor').all():
        v = q.vendor
        phone = _normalise_phone(v.phone or '')
        can_send = bool(phone)
        body = _format_rfq_message(rfq, workshop_name=workshop_name)
        import urllib.parse
        text = urllib.parse.quote(body)
        wa_url = f"https://wa.me/{phone}?text={text}" if can_send else ''
        out.append({
            'quote_id': q.id,
            'vendor_id': v.id,
            'vendor_name': v.name,
            'phone': v.phone or '',
            'can_send': can_send,
            'wa_url': wa_url,
            'has_response': q.has_response,
            'quoted_price': float(q.quoted_price) if q.quoted_price is not None else None,
            'quoted_eta_days': q.quoted_eta_days,
        })
    return out


def _format_rfq_message(rfq, workshop_name: str = '') -> str:
    """The Arabic body the vendor sees in WhatsApp. Kept short + scannable
    so it works well in WA's preview."""
    workshop_line = (
        f"من: *{workshop_name}*\n\n"
        if workshop_name else ""
    )
    name_part = rfq.part_name_requested or 'القطعة'
    lines = [
        f"السلام عليكم 👋",
        "",
        f"{workshop_line}نحتاج تسعير قطعة:",
        f"📌 *{name_part}*",
        f"🔧 رقم القطعة: *{rfq.part_number_requested}*",
        f"📦 الكمية: *{rfq.quantity}*",
        "",
        "برجاء إفادتنا بـ:",
        "  • السعر للوحدة",
        "  • مدة التوفر / التوريد",
        "  • أي شروط استبدال أو ضمان",
        "",
        "في انتظار ردكم. شكراً.",
    ]
    return "\n".join(lines)


def log_quote_response(quote, *, price, eta_days=None, notes: str = ''):
    """Inventory manager pastes the vendor's reply into the system."""
    from django.utils import timezone
    from inventory.models import RFQ
    try:
        price_dec = Decimal(str(price))
    except Exception:
        raise ValueError("bad_price")
    if price_dec <= 0:
        raise ValueError("non_positive_price")
    if eta_days is not None:
        try:
            eta_days = int(eta_days)
        except (TypeError, ValueError):
            eta_days = None
        if eta_days is not None and (eta_days < 0 or eta_days > 365):
            eta_days = None

    quote.quoted_price = price_dec
    quote.quoted_eta_days = eta_days
    quote.notes = (notes or '').strip()[:240]
    quote.quoted_at = timezone.now()
    quote.save(update_fields=[
        'quoted_price', 'quoted_eta_days', 'notes', 'quoted_at',
    ])

    # Promote the parent RFQ status if it's still OPEN
    rfq = quote.rfq
    if rfq.status == RFQ.STATUS_OPEN:
        rfq.status = RFQ.STATUS_QUOTED
        rfq.save(update_fields=['status'])
    return quote


def accept_quote(quote, *, treasury=None):
    """Convert the winning quote into a *draft* PurchaseInvoice.

    The PO is created in DRAFT — the inventory manager still has to
    confirm receipt + post it, which is the existing PurchaseInvoice
    lifecycle. We just save them the data-entry step.
    """
    from inventory.models import (
        RFQ, PurchaseInvoice, PurchaseInvoiceItem,
    )

    rfq = quote.rfq
    if rfq.status == RFQ.STATUS_ORDERED:
        return rfq.purchase_invoice  # already accepted, idempotent

    if quote.quoted_price is None:
        raise ValueError("quote_has_no_price")
    if rfq.product is None:
        raise ValueError("product_not_in_catalogue")

    with transaction.atomic():
        po = PurchaseInvoice.objects.create(
            vendor=quote.vendor,
            branch=rfq.branch,
            treasury=treasury,
            status='draft',
        )
        PurchaseInvoiceItem.objects.create(
            invoice=po,
            product=rfq.product,
            quantity=rfq.quantity,
            cost_price=quote.quoted_price,
        )
        po.update_total()

        rfq.status = RFQ.STATUS_ORDERED
        rfq.accepted_quote = quote
        rfq.purchase_invoice = po
        rfq.save(update_fields=[
            'status', 'accepted_quote', 'purchase_invoice',
        ])

    logger.info(
        "[RFQ.accept] rfq=%s vendor=%s qty=%s price=%s → PO #%s (draft)",
        rfq.id, quote.vendor_id, rfq.quantity, quote.quoted_price, po.id,
    )
    return po
