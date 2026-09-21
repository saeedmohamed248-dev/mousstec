"""
robot/customers.py — greet customers, recognize them, and never forget them.

Recognition channels (any of them):
  * face      — embedding matched against RobotCustomerFace
  * name      — Customer.name contains
  * phone     — Customer.phone (unique)
  * invoice   — SaleInvoice id / number → its customer

On a match the robot remembers the visit (visit_count/last_seen), enrolls the
face for next time when a photo is available, and builds a warm, personal
greeting: returning vs new, VIP/loyalty aware, with a recall of the last part
they bought and a gentle suggestion. Any outstanding balance is surfaced only to
the STAFF (`staff_note`), never spoken aloud — privacy first.

No wholesale/cost ever appears here; only the customer's own record and the
retail history they already paid.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Tuple

from django.utils import timezone

from .security import compare_embeddings

# Face match threshold for customers (a bit looser than staff access control —
# a wrong greeting is harmless, a wrong door unlock is not).
_CUSTOMER_FACE_THRESHOLD = 0.9


def recognize_customer(*, embedding=None, name: str = "", phone: str = "",
                       invoice_number: str = "") -> Tuple[Optional[object], str, float]:
    """Find a customer by face / name / phone / invoice. Returns (customer, method, score)."""
    from inventory.models import Customer, SaleInvoice
    from .models import RobotCustomerFace
    from .security import matching_available

    # 1) Face — strongest "they walked in and I know them" signal. Only trust it
    #    when a REAL face model is installed: the non-biometric fallback scores
    #    ~0.99 between different people, so it would greet the wrong customer by
    #    name (a privacy leak). Without it, fall through to name/phone/invoice.
    if embedding and matching_available():
        best, best_score = None, 0.0
        for row in RobotCustomerFace.objects.exclude(face_encoding__isnull=True).select_related("customer"):
            score = compare_embeddings(embedding, row.face_encoding)
            if score > best_score:
                best, best_score = row, score
        if best and best_score >= _CUSTOMER_FACE_THRESHOLD:
            return best.customer, "face", best_score

    # 2) Invoice number → its customer.
    inv_no = (invoice_number or "").strip().lstrip("#")
    if inv_no.isdigit():
        inv = SaleInvoice.objects.filter(pk=int(inv_no)).select_related("customer").first()
        if inv:
            return inv.customer, "invoice", 1.0

    # 3) Phone (unique).
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits:
        cust = Customer.objects.filter(phone=digits).first()
        if cust:
            return cust, "phone", 1.0

    # 4) Name contains.
    nm = (name or "").strip()
    if nm:
        cust = Customer.objects.filter(name__icontains=nm).first()
        if cust:
            return cust, "name", 0.8

    return None, "", 0.0


def remember_visit(customer, *, embedding=None):
    """Bump the customer's visit counters and enroll their face if we have one."""
    from .models import RobotCustomerFace

    face, _created = RobotCustomerFace.objects.get_or_create(customer=customer)
    face.visit_count = (face.visit_count or 0) + 1
    face.last_seen_at = timezone.now()
    # Enroll/refresh the face embedding so next time is recognized by sight.
    if embedding and not face.face_encoding:
        face.face_encoding = embedding
    face.save()
    return face


def last_purchase(customer):
    """(part_name, when) of the customer's most recent bought part, or (None, None)."""
    from inventory.models import SaleInvoiceItem
    item = (SaleInvoiceItem.objects
            .filter(invoice__customer=customer, invoice__status="posted")
            .select_related("product", "invoice")
            .order_by("-invoice__date_created")
            .first())
    if not item:
        return None, None
    return item.product.name, item.invoice.date_created


def customer_greeting(customer, *, method: str = "", visit_count: int = 0) -> dict:
    """Build the spoken greeting + a private staff note.

    Returns {greeting, staff_note, recall}. Wholesale/cost never appear.
    """
    name = customer.name
    tier = ""
    try:
        tier = customer.vip_tier or ""
    except Exception:
        tier = ""

    # New vs returning.
    if visit_count <= 1:
        greeting = f"أهلاً {name}! نورت مركز Mouss Tec. تحت أمرك، محتاج إيه النهاردة؟"
    else:
        greeting = f"أهلاً بيك تاني يا {name}! سعيد إنك رجعتلنا. "
        part, when = last_purchase(customer)
        if part:
            greeting += f"آخر مرة أخدت {part} — محتاج زيها تاني ولا حاجة تانية؟"
        else:
            greeting += "أقدر أساعدك في إيه النهاردة؟"

    # VIP / loyalty flourish (spoken, positive only).
    try:
        pts = int(customer.loyalty_points or 0)
    except Exception:
        pts = 0
    if pts >= 100:
        greeting += f" وعندك {pts} نقطة ولاء — تقدر تستبدلها."

    # Private staff note — outstanding balance is NEVER announced out loud.
    staff_note = ""
    try:
        bal = Decimal(str(customer.balance or 0))
        if bal > 0:
            staff_note = f"⚠️ العميل عليه رصيد آجل {bal:.0f} ج.م — راجع قبل البيع الآجل."
    except Exception:
        pass

    part, when = last_purchase(customer)
    return {
        "greeting": greeting,
        "staff_note": staff_note,
        "recall": {
            "vip_tier": tier,
            "loyalty_points": pts,
            "recognized_by": method,
            "visit_count": visit_count,
            "last_part": part,
        },
    }
