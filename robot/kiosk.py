"""
robot/kiosk.py — read-only answers for the customer kiosk (`smart_robot/`).

The kiosk (an Android phone in the robot's head + a FastAPI brain) talks to
customers. Until now it answered from a mock table; these functions give it the
real ERP with the same return shapes its tools expect, through the robot API
(device-token auth), so the kiosk never touches the database directly.

Customer-facing, so: retail prices only (via `safe_product_payload`), no
outstanding balances, and a phone lookup returns a first name only — enough to
greet, not enough to mine the customer book.
"""

from __future__ import annotations

import os

from django.utils import timezone

from . import services
from .pricing import safe_product_payload

RETURN_WINDOW_DAYS = int(os.getenv("ROBOT_RETURN_WINDOW_DAYS", "14"))


def part_info(query: str, branch) -> dict:
    """{found, part_number, name, in_stock, stock, location, fits, price, currency}."""
    query = (query or "").strip()
    product = (services.resolve_from_knowledge(code=query, label=query)
               or services.find_product(query)
               or services.learned_alias_in(query)) if query else None
    if product is None:
        return {"found": False, "query": query}
    p = safe_product_payload(product, branch=branch)
    stock = int(p.get("stock") or 0)
    return {
        "found": True,
        "part_number": p["part_number"],
        "name": p["name"],
        "in_stock": stock > 0,
        "stock": stock,
        "location": services.shelf_location(product, branch) or None,
        "fits": [p["car_model"]] if p.get("car_model") else [],
        "price": p.get("retail_price"),
        "currency": "EGP",
        "warranty_months": p.get("warranty_months") or 0,
    }


def customer_brief(phone: str) -> dict:
    """First name only, for a greeting — never balance, history or tier.

    Anyone can type any number at a kiosk, so this must not be a way to learn
    about other people's accounts."""
    from inventory.models import Customer
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return {"found": False, "phone": phone}
    c = Customer.objects.filter(phone=digits).first()
    if c is None:
        return {"found": False, "phone": phone}
    first = (c.name or "").split()[0] if c.name else ""
    return {"found": True, "name": first}


def return_check(invoice_number: str = "", phone: str = "") -> dict:
    """Return / warranty eligibility of a posted sale invoice.

    Needs BOTH the invoice number (its id; "INV-123", "#123" and "123" all
    work — usually scanned off the paper invoice) AND the phone on that
    invoice. Invoice ids are sequential and phones are guessable, so either
    one alone would let anyone at the kiosk read other customers' purchases.
    Eligible for return within RETURN_WINDOW_DAYS; under warranty while the
    longest item warranty lasts (0 months = no warranty).
    """
    from inventory.models import SaleInvoice

    digits = "".join(ch for ch in (invoice_number or "") if ch.isdigit())
    ph = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits or not ph:
        return {
            "found": False,
            "needs": "phone" if digits else "invoice_number",
            "message": ("محتاج رقم الفاتورة ورقم التليفون المسجّل عليها مع بعض."),
        }
    invoice = (SaleInvoice.objects
               .filter(invoice_type="sale", status="posted", is_return=False,
                       pk=int(digits), customer__phone=ph)
               .select_related("customer").first())
    if invoice is None:
        return {"found": False, "invoice_number": invoice_number}

    items = list(invoice.items.select_related("product"))
    first = items[0].product if items else None
    warranty_months = max([int(getattr(i.product, "warranty_months", 0) or 0) for i in items] or [0])
    days = (timezone.now() - invoice.date_created).days
    return {
        "found": True,
        "invoice_number": str(invoice.pk),
        "part_number": getattr(first, "part_number", "") if first else "",
        "part_name": getattr(first, "name", "") if first else "",
        "purchase_date": timezone.localtime(invoice.date_created).date().isoformat(),
        "days_since_purchase": days,
        "return_window_days": RETURN_WINDOW_DAYS,
        "return_eligible": days <= RETURN_WINDOW_DAYS,
        "under_warranty": warranty_months > 0 and days <= warranty_months * 30,
        "warranty_months": warranty_months,
        "amount": float(invoice.total_amount or 0),
        "currency": "EGP",
    }
