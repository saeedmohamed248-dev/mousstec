"""
robot/wizard.py — a sales invoice, a purchase or an expense, asked for step by step.

«يا موس، اعملي فاتورة مبيعات» used to go to the AI, which promised what the
robot couldn't do. The robot itself had to hear the whole thing in one go
(«بعت 2 فلتر زيت كاش», robot/entries.py). Now it asks, one question at a
time, the way someone behind the counter would:

    فاتورة مبيعات → اسم العميل؟ (أو «نقدي») → القطعة؟ → «فلتر زيت بوش، الحتة بـ 450 جنيه،
    حتة واحدة — أضيفها؟» → فيه صنف تاني؟ → كاش ولا آجل؟ → robot/entries.ask:
    «أسجله على أنهي فرع؟» → recorded, by the ERP's own posting.

Same for a purchase (the vendor, each part with what it cost, cash or on the
vendor) and an expense (how much, on what). Every money rule is entries'
own: retail prices only, no credit on «عميل نقدي» (whose name, when one is
said, goes on the invoice's notes), the branch's cash treasury.

Only staff may: the face the camera knows, or — no camera — their PIN (the
counter page's, robot/models.RobotStaffPin), said once and remembered for a
while. What's said for it is never kept (views._voice_turn).

«الغي» stops it anywhere. It's forgotten after STALE_S of silence.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.core.cache import cache

from . import entries, permissions, services, wakename
from .brain import talk
from .brain.arabic_numbers import to_digits

STALE_S = 180                      # a question unanswered this long: forgotten
ANSWER_S = 60                      # an answer to its question needs no «يا موس» this long
STAFF_S = 10 * 60                  # a PIN said once: the robot knows who's there this long
PIN_TRIES = 3
MOST_LINES = entries.MOST_LINES

_SALE = re.compile(r"فاتور[هة]\s+(?:ال)?(?:مبيعات|بيع)|(?:اعمل|اعملي|نعمل|تعمل|تعملي)\s+(?:عمليه|عملية)?\s*بيع\b")
_PURCHASE = re.compile(r"فاتور[هة]\s+(?:ال)?(?:مشتريات|شرا|شراء)")
_EXPENSE = re.compile(r"(?:^|\s)(?:مصروف|مصاريف)(?:\s|$)")
_WALK_IN = frozenset("نقدي نقدى كاش مفيش لا ولا".split())
_NO = frozenset("لا لأ لاء مش خلاص كفايه كفاية بس no".split())
_MORE = frozenset("اه ايوه ايوا فيه كمان تاني ضيف yes".split())
_CREDIT = re.compile(r"آجل|اجل|أجل|على الحساب|علي الحساب")
_CASH = re.compile(r"كاش|نقدي|نقدى|نقد|cash")
_NAME_FILLER = re.compile(r"^(?:اسمه|اسمها|اسم العميل|العميل|الاستاذ|الأستاذ|استاذ|أستاذ|المدام|مدام|يا)\s+")


def _key(device) -> str:
    return f"robot:wizard:{device.pk}"


def _staff_key(device) -> str:
    return f"robot:wizard:staff:{device.pk}"


def _get(device):
    try:
        return cache.get(_key(device))
    except Exception:
        return None


def _put(device, state: dict) -> None:
    import time
    state["asked_at"] = time.time()
    cache.set(_key(device), state, STALE_S)


def _end(device) -> None:
    cache.delete(_key(device))


def active(device) -> bool:
    """An answer to the wizard's own question, just asked: no «يا موس» needed."""
    import time
    state = _get(device)
    return bool(state) and time.time() - state.get("asked_at", 0) <= ANSWER_S


def wants_secret(device) -> bool:
    """The robot just asked for a PIN: what's said next is never kept."""
    state = _get(device)
    return bool(state) and state.get("step") == "pin"


def _said(text: str) -> str:
    return to_digits(wakename.normalize(text or "")).strip(" ،,.!؟?:-")


# «لا» is an answer here («فيه صنف تاني؟» — «لا»): only these stop it.
_STOP = frozenset({"الغي", "الغيها", "الغيه", "الغاء", "بلاش", "كنسل", "cancel", "مش عايز", "مش عاوز",
                   "خلاص مش عايز", "خلاص مش عاوز", "لا الغي", "لا الغيها"})


def _cancel(text: str) -> bool:
    return wakename.normalize(text or "").strip(" ،,.!؟?:-") in {wakename.normalize(w) for w in _STOP}


def _no(text: str) -> bool:
    words = _said(text).split()
    return bool(words) and words[0] in _NO


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------

def start(text: str, device, employee):
    """«اعملي فاتورة مبيعات» (or a purchase, an expense) → its first question;
    None when the sentence isn't asking for one."""
    said = wakename.normalize(text or "")
    if _PURCHASE.search(said):
        kind, action = "purchase", "purchase"
    elif _SALE.search(said):
        kind, action = "sale", "sale"
    elif _EXPENSE.search(said) and (entries.parse_expense(text) or {}).get("amount") is None:
        kind, action = "expense", "expense"           # «سجل مصروف 200 بنزين» is entries' at once
    else:
        return None
    state = {"kind": kind, "action": action, "lines": [], "step": ""}
    staff = _staff(device, employee)
    if staff is None:
        state["step"] = "pin"
        _put(device, state)
        return ("تمام. قولي الرقم السري بتاعك الأول (بتاع صفحة الكاشير)؟",
                {"action": "wizard_pin", "kind": kind})
    return _begin(device, state, staff)


def _staff(device, employee):
    """Who's asking: the face the camera knows, or the one who said their PIN lately."""
    if employee is not None:
        return employee
    try:
        emp_id = cache.get(_staff_key(device))
    except Exception:
        emp_id = None
    if not emp_id:
        return None
    from hr.models import Employee
    return Employee.objects.filter(pk=emp_id).select_related("user").first()


def _begin(device, state: dict, staff):
    if not permissions.employee_can(staff, state["action"]):
        _end(device)
        return permissions.denial_message(state["action"]), {"action": "denied"}
    state["employee_id"] = staff.pk
    kind = state["kind"]
    if kind == "sale":
        state["step"] = "customer"
        _put(device, state)
        return "تمام، فاتورة مبيعات. اسم العميل إيه؟ ولو نقدي قول «نقدي».", {"action": "wizard_customer"}
    if kind == "purchase":
        state["step"] = "vendor"
        _put(device, state)
        return "تمام، فاتورة مشتريات. من أنهي مورد؟", {"action": "wizard_vendor"}
    state["step"] = "amount"
    _put(device, state)
    return "تمام، مصروف. المبلغ كام؟", {"action": "wizard_amount"}


# ---------------------------------------------------------------------------
# Each answer
# ---------------------------------------------------------------------------

def answer(text: str, device, employee):
    """The wizard's next question (reply, payload), or None when none is open."""
    state = _get(device)
    if not state:
        return None
    if _cancel(text):
        _end(device)
        return "ماشي، لغيت ومسجلتش حاجة.", {"action": "wizard_cancelled"}
    step = state.get("step")
    handler = {"pin": _on_pin, "customer": _on_customer, "walk_in_name": _on_walk_in_name,
               "vendor": _on_vendor, "item": _on_item, "choose": _on_choose, "confirm": _on_confirm,
               "cost": _on_cost, "more": _on_more, "payment": _on_payment, "amount": _on_amount,
               "description": _on_description}.get(step)
    if handler is None:
        _end(device)
        return None
    return handler(text, device, state, employee)


def _on_pin(text, device, state, employee):
    pin = "".join(re.findall(r"\d", _said(text)))
    staff = _employee_for_pin(pin) if 4 <= len(pin) <= 8 else None
    if staff is None:
        state["pin_tries"] = state.get("pin_tries", 0) + 1
        if state["pin_tries"] >= PIN_TRIES:
            _end(device)
            return "الرقم مش صح، فلغيت. اطلب من المدير يظبط الرقم السري من صفحة الروبوت.", {"action": "wizard_pin_failed"}
        _put(device, state)
        return "الرقم ده مش صح. قوله تاني رقم رقم؟", {"action": "wizard_pin"}
    cache.set(_staff_key(device), staff.pk, STAFF_S)
    return _begin(device, state, staff)


def _employee_for_pin(pin: str):
    """The employee whose counter-page PIN this is (robot/brain/pins.py hashes), or None."""
    from django.contrib.auth.hashers import check_password

    from .models import RobotStaffPin

    for row in RobotStaffPin.objects.select_related("employee__user"):
        if check_password(pin, row.pin_hash):
            return row.employee
    return None


def _on_customer(text, device, state, employee):
    said = _said(text)
    if not said:
        return "اسم العميل إيه؟ ولو نقدي قول «نقدي».", {"action": "wizard_customer"}
    words = said.split()
    if words[0] in _WALK_IN or said in ("عميل نقدي", "من غير اسم"):
        state.update(customer_id=None, walk_in_name="", step="item")
        _put(device, state)
        return "تمام، نقدي. القطعة إيه؟", {"action": "wizard_item"}
    name = _NAME_FILLER.sub("", text.strip(" ،,.!؟?:-")).strip()
    customer, how_many = entries.find_customer(name)
    if how_many > 1:
        _put(device, state)
        return f"فيه أكتر من عميل اسمه {name}. قول اسمه كامل زي ما هو متسجل؟", {"action": "wizard_customer"}
    if customer is None:
        state.update(step="walk_in_name", walk_in_name=name[:100])
        _put(device, state)
        return (f"مش لاقي عميل متسجل اسمه {name}. أكمّلها نقدي باسمه؟ ولو عايز عميل تاني قول اسمه.",
                {"action": "wizard_customer_unknown"})
    state.update(customer_id=customer.pk, customer_name=customer.name, walk_in_name="", step="item")
    _put(device, state)
    return f"تمام، {customer.name}. القطعة إيه؟", {"action": "wizard_item"}


def _on_walk_in_name(text, device, state, employee):
    if talk.just_yes(text):
        state["step"] = "item"
        _put(device, state)
        return f"تمام، نقدي باسم {state['walk_in_name']}. القطعة إيه؟", {"action": "wizard_item"}
    if _no(text):
        state.update(step="customer", walk_in_name="")
        _put(device, state)
        return "اسم العميل إيه؟ ولو نقدي قول «نقدي».", {"action": "wizard_customer"}
    return _on_customer(text, device, state, employee)


def _on_vendor(text, device, state, employee):
    name = text.strip(" ،,.!؟?:-")
    name = re.sub(r"^(?:من|المورد|مورد)\s+", "", name).strip()
    vendor = entries.find_vendor(name) if name else None
    if vendor is None:
        _put(device, state)
        return (f"مش لاقي مورد اسمه {name or 'كده'}. قول اسمه زي ما هو متسجل؟ ولو جديد ضيفه من شاشة الموردين.",
                {"action": "wizard_vendor"})
    state.update(vendor_id=vendor.pk, vendor_name=vendor.name, step="item")
    _put(device, state)
    return f"تمام، {vendor.name}. القطعة إيه؟", {"action": "wizard_item"}


def _on_item(text, device, state, employee):
    qty, query = entries._counted(_said(text))
    if not query:
        return "القطعة إيه؟ قول اسمها أو رقمها.", {"action": "wizard_item"}
    product = services.find_product(query)
    choices = []
    if product is None:
        product, choices = services.match_part_in_sentence(query)
    if product is None and choices:
        state.update(step="choose", choices=[p.pk for p in choices[:3]], quantity=qty)
        _put(device, state)
        listed = "، ".join(f"{talk_ordinal(i)} {p.name}" for i, p in enumerate(choices[:3]))
        return f"عندي أكتر من صنف: {listed}. تقصد أنهي؟", {"action": "wizard_choose"}
    if product is None:
        _put(device, state)
        return f"مش لاقي «{query}» في الأصناف. قول اسمها تاني أو رقمها؟", {"action": "wizard_item"}
    return _offer(device, state, product, qty)


def talk_ordinal(i: int) -> str:
    return ("الأولاني", "التاني", "التالت")[i] if i < 3 else ""


def _on_choose(text, device, state, employee):
    from inventory.models import Product

    products = Product.objects.in_bulk(state.get("choices") or [])
    choices = [products[pk] for pk in state.get("choices") or [] if pk in products]
    picked = talk.pick_choice(text, [{"name": p.name, "model": ""} for p in choices])
    if picked is None:
        return _on_item(text, device, state, employee)        # a part's name said instead
    return _offer(device, state, choices[picked], state.get("quantity") or 1)


def _offer(device, state, product, qty):
    if qty < 1:
        return "العدد لازم يكون حتة أو أكتر. القطعة إيه؟", {"action": "wizard_item"}
    count = "حتة واحدة" if qty == 1 else f"{qty} حتت"
    if state["kind"] == "purchase":
        state.update(step="cost", pending={"product_id": product.pk, "name": product.name, "quantity": qty})
        _put(device, state)
        return f"{product.name}، {count}. اشتريت الحتة بكام؟", {"action": "wizard_cost"}
    price = Decimal(str(product.retail_price or 0)).quantize(Decimal("0.01"))
    if price <= 0:
        state["step"] = "item"
        _put(device, state)
        return (f"سعر {product.name} مش متسجل، فمقدرش أبيعه بالصوت. قول قطعة تانية أو «الغي».",
                {"action": "wizard_item"})
    state.update(step="confirm", pending={"product_id": product.pk, "name": product.name, "quantity": qty,
                                          "price": str(price)})
    _put(device, state)
    return (f"{product.name}، الحتة بـ {services.spoken_price(price)}، {count}. أضيفها؟",
            {"action": "wizard_confirm", "product_id": product.pk})


def _on_cost(text, device, state, employee):
    numbers = re.findall(r"\d+(?:\.\d+)?", _said(text))
    if not numbers:
        return "بكام الحتة؟ قول المبلغ.", {"action": "wizard_cost"}
    cost = Decimal(numbers[0]).quantize(Decimal("0.01"))
    if cost <= 0:
        return "المبلغ لازم يكون أكتر من صفر. بكام الحتة؟", {"action": "wizard_cost"}
    pending = state["pending"]
    pending["price"] = str(cost)
    state["step"] = "confirm"
    _put(device, state)
    count = "حتة واحدة" if pending["quantity"] == 1 else f"{pending['quantity']} حتت"
    return (f"{pending['name']}، {count}، الحتة بـ {services.spoken_price(cost)}. أضيفها؟",
            {"action": "wizard_confirm"})


def _on_confirm(text, device, state, employee):
    if talk.just_yes(text) or _said(text) in ("صح", "مظبوط", "ضيفها", "اضيفها", "ضيف"):
        state["lines"].append(state.pop("pending"))
        if len(state["lines"]) >= MOST_LINES:
            return _payment_question(device, state)
        state["step"] = "more"
        _put(device, state)
        return "تمام. فيه صنف تاني؟", {"action": "wizard_more"}
    if _no(text):
        state.pop("pending", None)
        state["step"] = "item"
        _put(device, state)
        return "ماشي. القطعة إيه؟", {"action": "wizard_item"}
    return _on_item(text, device, state, employee)            # another part said instead


def _on_more(text, device, state, employee):
    said = _said(text)
    if _no(text):
        return _payment_question(device, state)
    if said.split() and all(w in _MORE for w in said.split()):
        state["step"] = "item"
        _put(device, state)
        return "القطعة إيه؟", {"action": "wizard_item"}
    return _on_item(text, device, state, employee)            # «و2 بوجيه»: the next part, said at once


def _payment_question(device, state):
    state["step"] = "payment"
    _put(device, state)
    if state["kind"] == "purchase":
        return "كاش ولا آجل على المورد؟", {"action": "wizard_payment"}
    return "كاش ولا آجل؟", {"action": "wizard_payment"}


def _on_payment(text, device, state, employee):
    said = _said(text)
    credit = bool(_CREDIT.search(said))
    if not credit and not _CASH.search(said):
        return "كاش ولا آجل؟", {"action": "wizard_payment"}
    if state["kind"] == "sale" and credit and not state.get("customer_id"):
        return ("الآجل لازم يكون على عميل متسجل باسمه، ومينفعش على «عميل نقدي». يبقى كاش؟",
                {"action": "wizard_payment"})
    return _hand_over(device, state, credit)


def _hand_over(device, state, credit: bool):
    """Everything heard: entries' own question — «أسجله على أنهي فرع؟» — takes it from here."""
    from hr.models import Employee

    _end(device)
    staff = Employee.objects.filter(pk=state.get("employee_id")).first()
    lines = state["lines"]
    total = sum((Decimal(line["price"]) * line["quantity"] for line in lines), Decimal("0"))
    said = [line["name"] if line["quantity"] == 1 else f"{line['quantity']} {line['name']}" for line in lines]
    if state["kind"] == "purchase":
        pay = "آجل على المورد" if credit else "كاش"
        summary = (f"شراء {' و'.join(said)} من {state['vendor_name']} بإجمالي "
                   f"{services.spoken_price(total)} ({pay})")
        reply = entries.ask(device, kind="purchase", summary=summary, amount=total, employee=staff,
                            data={"lines": [{"product_id": line["product_id"], "quantity": line["quantity"],
                                             "unit_cost": line["price"]} for line in lines],
                                  "vendor_id": state["vendor_id"], "cash": not credit})
        return reply, {"action": "entry_ask_branch", "kind": "purchase", "lines": len(lines)}
    who = state.get("customer_name") or state.get("walk_in_name") or ""
    summary = (f"بيع {' و'.join(said)}{f' لـ{who}' if who else ''} بإجمالي {services.spoken_price(total)} "
               + ("آجل" if credit else "كاش"))
    reply = entries.ask(device, kind="sale", summary=summary, amount=total, employee=staff,
                        data={"lines": [{"product_id": line["product_id"], "quantity": line["quantity"],
                                         "unit_price": line["price"]} for line in lines],
                              "customer_id": state.get("customer_id"), "credit": credit,
                              "walk_in_name": state.get("walk_in_name", "")})
    return reply, {"action": "entry_ask_branch", "kind": "sale", "lines": len(lines)}


def _on_amount(text, device, state, employee):
    numbers = re.findall(r"\d+(?:\.\d+)?", _said(text))
    if not numbers or Decimal(numbers[0]) <= 0:
        return "المبلغ كام؟ قول الرقم.", {"action": "wizard_amount"}
    state.update(amount=numbers[0], step="description")
    _put(device, state)
    return f"{services.spoken_price(Decimal(numbers[0]))}. على إيه؟", {"action": "wizard_description"}


def _on_description(text, device, state, employee):
    from hr.models import Employee

    description = text.strip(" ،,.!؟?:-")[:200]
    if not description:
        return "المصروف على إيه؟", {"action": "wizard_description"}
    category, salaries = entries.expense_category(description)
    if salaries:
        _end(device)
        return ("المرتبات بتتسجل من شاشة المصروفات عشان محتاجة اسم الموظف.", {"action": "entry_salaries"})
    _end(device)
    amount = Decimal(state["amount"])
    staff = Employee.objects.filter(pk=state.get("employee_id")).first()
    summary = f"مصروف {amount:.0f} جنيه ({description})"
    reply = entries.ask(device, kind="expense", summary=summary, amount=amount, employee=staff,
                        data={"amount": str(amount), "description": description,
                              "category_id": getattr(category, "pk", None)})
    return reply, {"action": "entry_ask_branch", "kind": "expense"}
