"""
database.py — Mocked ERP / Inventory data layer for the Smart Parts Robot.

In production these functions would query the company's real ERP database
(the Django `inventory` / `erp_core` apps in this repo, or an external SQL
server). For now every function returns realistic mock data so the whole
robot flow can be demoed end-to-end without a live database.

To wire this into the real ERP later, replace the bodies of each function
with actual queries (e.g. Django ORM calls or a SQL client) while keeping
the *return shapes* identical — the AI logic in `main.py` depends only on
these shapes, not on where the data comes from.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Mock tables
# ---------------------------------------------------------------------------

# Parts catalogue keyed by a normalized part number.
# `aliases` lets the AI match a spoken/typed description to a real SKU.
_PARTS: dict[str, dict] = {
    "31126794339": {
        "part_number": "31126794339",
        "name": "Front Control Arm (Left)",
        "fits": ["BMW F30", "BMW F31", "BMW F32"],
        "stock": 7,
        "price": 1850.00,
        "currency": "EGP",
        "location": "Shelf A3",
        "warranty_months": 12,
        "aliases": ["control arm", "wishbone", "front arm"],
    },
    "32106868687": {
        "part_number": "32106868687",
        "name": "Steering Rack (Electric)",
        "fits": ["BMW F30", "BMW F30 320i", "BMW F35"],
        "stock": 2,
        "price": 14250.00,
        "currency": "EGP",
        "location": "Cage B1",
        "warranty_months": 6,
        "aliases": ["steering rack", "steering gear", "rack and pinion"],
    },
    "34116794300": {
        "part_number": "34116794300",
        "name": "Front Brake Pads Set",
        "fits": ["BMW F30", "MINI F56", "BMW F20"],
        "stock": 0,
        "price": 2100.00,
        "currency": "EGP",
        "location": "Shelf C2",
        "warranty_months": 6,
        "aliases": ["brake pads", "front pads", "brake pad set"],
    },
    "11427953129": {
        "part_number": "11427953129",
        "name": "Oil Filter",
        "fits": ["BMW F30", "MINI F56", "BMW G20"],
        "stock": 45,
        "price": 320.00,
        "currency": "EGP",
        "location": "Shelf D5",
        "warranty_months": 3,
        "aliases": ["oil filter", "filter"],
    },
}

# Customers keyed by phone number.
_CUSTOMERS: dict[str, dict] = {
    "01001234567": {
        "phone": "01001234567",
        "name": "Ahmed Saeed",
        "vehicle": "BMW F30 320i (2015)",
        "loyalty_tier": "Gold",
    },
    "01119876543": {
        "phone": "01119876543",
        "name": "Mona Khaled",
        "vehicle": "MINI F56 Cooper S (2018)",
        "loyalty_tier": "Silver",
    },
}

# Invoices keyed by invoice number (usually encoded in the barcode/QR).
# `purchase_date` drives warranty / return-eligibility checks.
_INVOICES: dict[str, dict] = {
    "INV-2025-0455": {
        "invoice_number": "INV-2025-0455",
        "customer_phone": "01001234567",
        "part_number": "32106868687",
        "purchase_date": (date.today() - timedelta(days=40)).isoformat(),
        "amount": 14250.00,
        "currency": "EGP",
    },
    "INV-2024-0912": {
        "invoice_number": "INV-2024-0912",
        "customer_phone": "01119876543",
        "part_number": "34116794300",
        "purchase_date": (date.today() - timedelta(days=400)).isoformat(),
        "amount": 2100.00,
        "currency": "EGP",
    },
    "INV-2025-0788": {
        "invoice_number": "INV-2025-0788",
        "customer_phone": "01001234567",
        "part_number": "11427953129",
        "purchase_date": (date.today() - timedelta(days=5)).isoformat(),
        "amount": 320.00,
        "currency": "EGP",
    },
}

# Company return policy (days). A return is only allowed within this window.
RETURN_WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase, strip and collapse whitespace for fuzzy matching."""
    return " ".join(text.lower().split())


def _find_part_by_query(query: str) -> Optional[dict]:
    """Best-effort match of a free-text query to a part.

    Tries, in order: exact part number, alias substring match, then a
    name/fits keyword match. Returns the part dict or None.
    """
    q = _normalize(query)

    # 1) Exact / contained part number (digits only comparison).
    digits = "".join(ch for ch in q if ch.isdigit())
    if digits and digits in _PARTS:
        return _PARTS[digits]

    # 2) Alias match.
    for part in _PARTS.values():
        for alias in part["aliases"]:
            if alias in q:
                return part

    # 3) Name / fitment keyword match.
    for part in _PARTS.values():
        if _normalize(part["name"]) in q:
            return part
        for model in part["fits"]:
            if _normalize(model) in q and any(
                alias in q for alias in part["aliases"]
            ):
                return part
    return None


# ---------------------------------------------------------------------------
# Public ERP-facing functions (these are what the AI logic calls)
# ---------------------------------------------------------------------------

def check_part_availability(query: str) -> dict:
    """Check stock for a part described by number or free text.

    Returns a dict the AI can turn into a spoken answer:
        {found, part_number, name, in_stock, stock, location, fits}
    """
    part = _find_part_by_query(query)
    if not part:
        return {"found": False, "query": query}
    return {
        "found": True,
        "part_number": part["part_number"],
        "name": part["name"],
        "in_stock": part["stock"] > 0,
        "stock": part["stock"],
        "location": part["location"],
        "fits": part["fits"],
    }


def check_price(query: str) -> dict:
    """Return the current price for a part described by number or free text."""
    part = _find_part_by_query(query)
    if not part:
        return {"found": False, "query": query}
    return {
        "found": True,
        "part_number": part["part_number"],
        "name": part["name"],
        "price": part["price"],
        "currency": part["currency"],
    }


def get_customer_by_phone(phone: str) -> dict:
    """Look up a customer by phone number."""
    key = "".join(ch for ch in phone if ch.isdigit())
    customer = _CUSTOMERS.get(key)
    if not customer:
        return {"found": False, "phone": phone}
    return {"found": True, **customer}


def validate_return(
    invoice_number: Optional[str] = None,
    phone: Optional[str] = None,
) -> dict:
    """Validate return / warranty eligibility for an invoice.

    An invoice can be located either directly by its number (usually decoded
    from the scanned barcode/QR) or by the customer's phone number (most
    recent invoice). Eligibility is decided by the purchase date vs the
    company return window and the part's warranty period.
    """
    invoice = None

    if invoice_number:
        invoice = _INVOICES.get(invoice_number.strip().upper())

    if invoice is None and phone:
        key = "".join(ch for ch in phone if ch.isdigit())
        candidates = [
            inv for inv in _INVOICES.values() if inv["customer_phone"] == key
        ]
        if candidates:
            invoice = max(candidates, key=lambda i: i["purchase_date"])

    if invoice is None:
        return {
            "found": False,
            "invoice_number": invoice_number,
            "phone": phone,
        }

    purchase_date = date.fromisoformat(invoice["purchase_date"])
    days_since = (date.today() - purchase_date).days
    part = _PARTS.get(invoice["part_number"], {})
    warranty_days = part.get("warranty_months", 0) * 30

    return_eligible = days_since <= RETURN_WINDOW_DAYS
    under_warranty = days_since <= warranty_days

    return {
        "found": True,
        "invoice_number": invoice["invoice_number"],
        "part_number": invoice["part_number"],
        "part_name": part.get("name", "Unknown part"),
        "customer_phone": invoice["customer_phone"],
        "purchase_date": invoice["purchase_date"],
        "days_since_purchase": days_since,
        "return_window_days": RETURN_WINDOW_DAYS,
        "return_eligible": return_eligible,
        "under_warranty": under_warranty,
        "warranty_months": part.get("warranty_months", 0),
        "amount": invoice["amount"],
        "currency": invoice["currency"],
    }


# ---------------------------------------------------------------------------
# Live ERP: same function names and return shapes, answered by the real
# Mouss Tec ERP when MOUSS_ERP_API + MOUSS_ROBOT_TOKEN are set (see
# erp_client.py). The mock tables above remain the offline/demo fallback.
# ---------------------------------------------------------------------------

from . import erp_client as _erp  # noqa: E402

if _erp.enabled():

    def check_part_availability(query: str) -> dict:  # noqa: F811
        res = _erp.get("kiosk/part", q=query)
        if not res.get("found"):
            return {"found": False, "query": query, **({"error": res["error"]} if "error" in res else {})}
        return {k: res.get(k) for k in
                ("found", "part_number", "name", "in_stock", "stock", "location", "fits")}

    def check_price(query: str) -> dict:  # noqa: F811
        res = _erp.get("kiosk/part", q=query)
        if not res.get("found"):
            return {"found": False, "query": query, **({"error": res["error"]} if "error" in res else {})}
        return {k: res.get(k) for k in ("found", "part_number", "name", "price", "currency")}

    def get_customer_by_phone(phone: str) -> dict:  # noqa: F811
        res = _erp.get("kiosk/customer", phone=phone)
        return res if res.get("found") else {"found": False, "phone": phone}

    def validate_return(invoice_number: Optional[str] = None,  # noqa: F811
                        phone: Optional[str] = None) -> dict:
        return _erp.get("kiosk/return-check", invoice_number=invoice_number or "",
                        phone=phone or "")
