"""
robot/restock.py — «أبلغك أول ما ييجي؟»: the sale that waits for the part.

When a part is out and the robot has nothing else to offer (no equivalent on
this shelf, not in another branch), it asks for a mobile number. The number
said next is kept (RobotRestockRequest) — never said back out loud. When the
part is back in this branch (checked on every heartbeat, so a purchase, a
transfer or a count that changed the stock any way is seen), the requests
turn `arrived`: an alert, the robot tells the staff, and the robot's page
has a ready WhatsApp message for each customer («اتبلّغ» once sent).

When the words are an answer, and what is said, are in robot/brain/talk.py:
the robot's brain asks and keeps the number the same way with the net down,
and sends it when the net is back (/sync/push/, kind «restock»).
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import memory
from .brain import talk

logger = logging.getLogger("mouss_tec_core")


def offer(device, product) -> str:
    """The question to add after «… مش متوفر»; the next sentence may answer it."""
    memory.remember(device, restock_for=product.pk)
    return talk.RESTOCK_ASK


def hear(text: str, device) -> Optional[tuple]:
    """(reply, payload) when `text` answers «قولي رقم موبايلك» — else None."""
    from inventory.models import Product

    pid = memory.recall(device).get("restock_for")
    if not pid:
        return None
    phone = talk.mobile_in(text)
    if phone:
        product = Product.objects.filter(pk=pid).first()
        if product is None:
            return None
        request = register(device, product, phone)
        memory.remember(device, restock_for=None)
        return talk.restock_saved(product.name)[0], {"action": "restock_saved", "request_id": request.pk}
    if talk.just_yes(text):
        return talk.RESTOCK_SAY_IT, {"action": "restock_ask_number"}
    if talk.tried_a_number(text):
        return talk.RESTOCK_AGAIN, {"action": "restock_bad_number"}
    return None


def _customer_for(phone: str):
    """The customer with this number — never the shared «عميل نقدي»."""
    from inventory.models import Customer

    found = Customer.find_by_phone(phone)
    return None if found is None or found.is_walk_in else found


def register(device, product, phone: str, *, branch=None):
    """The open request for this number and part (one, however often said)."""
    from .models import RobotRestockRequest

    branch = branch or device.branch
    open_ = RobotRestockRequest.objects.filter(product=product, branch=branch, phone=phone,
                                               status__in=("waiting", "arrived"))
    found = open_.first()
    if found is not None:
        return found
    try:
        with transaction.atomic():
            return RobotRestockRequest.objects.create(
                device=device, branch=branch, product=product, phone=phone,
                customer=_customer_for(phone))
    except IntegrityError:                   # said twice at once: the other one won
        return open_.first()


def sweep(device) -> int:
    """Requests in this robot's branch whose part is back: `arrived`, an
    alert and a word to the staff (once per part). → how many arrived."""
    from inventory.models import Inventory

    from .models import RobotAlert, RobotCommand, RobotRestockRequest

    branch = device.branch
    back = set(Inventory.objects.filter(
        branch=branch, quantity__gt=0,
        product__robot_restock_requests__branch=branch,
        product__robot_restock_requests__status="waiting",
    ).values_list("product_id", flat=True))
    if not back:
        return 0
    arrived = 0
    for request in (RobotRestockRequest.objects.filter(branch=branch, status="waiting", product_id__in=back)
                    .select_related("product")):
        request.status, request.arrived_at = "arrived", timezone.now()
        request.save(update_fields=["status", "arrived_at"])
        arrived += 1
    for pid in back:
        waiting = RobotRestockRequest.objects.filter(branch=branch, product_id=pid, status="arrived")
        n = waiting.count()
        if not n:
            continue
        name = waiting.first().product.name
        RobotAlert.objects.create(device=device, kind="other",
                                  message=f"📦 وصل {name}: {n} عميل مستنيه — ابعتلهم من صفحة «مستنيين أصناف».")
        RobotCommand.objects.create(device=device, kind="say", payload={
            "text": f"وصل {name}، وفيه {n} عميل مستنيه. ابعتلهم من صفحة الروبوت."})
    return arrived


def arrived_count(branch) -> int:
    from .models import RobotRestockRequest
    return RobotRestockRequest.objects.filter(branch=branch, status="arrived").count()


def whatsapp_link(request) -> str:
    """wa.me with the message ready — staff press send."""
    phone = request.phone
    digits = "2" + phone if phone.startswith("01") else phone.lstrip("+")
    price = request.product.retail_price or 0
    from .services import spoken_price
    text = (f"أهلاً 👋 معاك Mouss Tec — فرع {request.branch.name}. "
            f"{request.product.name} اللي سألت عليه وصل"
            + (f"، وسعره {spoken_price(price)}" if price > 0 else "") + ". مستنيينك!")
    return f"https://wa.me/{digits}?text={quote(text)}"


def waiting_for(customer) -> list:
    """Parts this customer was waiting for that are back (for the greeting)."""
    from .models import RobotRestockRequest

    if customer is None or getattr(customer, "is_walk_in", False):
        return []
    return list(RobotRestockRequest.objects.filter(customer=customer, status="arrived")
                .select_related("product").values_list("product__name", flat=True)[:2])
