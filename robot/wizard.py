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
while. What's said for it is never kept (views._voice_turn). Words that hold
no number aren't a wrong PIN (re-asked); three wrong ones end it, and
PIN_LOCK_TRIES on one robot lock the PIN out for a while.

«عايز أعمل فاتورة» — which one not said — asks «مبيعات ولا مشتريات؟». Asked
for again mid-way, it begins again; something else asked for in a «الغي…»
(«الغي الفاتورة وحط لي منتج في المخزون») begins that instead.

Pieces into the stock («ضيف 5 فلتر زيت للمخزون», «حط لي منتج في المخزون»):
a part the ERP has → «عندك منه 5. أزود 3 تبقى 8؟»; one it hasn't → its part
number, its price, what it cost → a new part. Recorded by entries.record_stock
(the ERP's own stock setting: the movement, the average cost, the ledger).

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
# «عايز أعمل فاتورة» — which one isn't said: asked. Vosk often drops the alef («عايز عمل فاتورة»).
_ANY_INVOICE = re.compile(r"(?:^|\s)[اوتن]?عمل(?:ي|لي)?\s+(?:لي\s+|ليا\s+)?فاتوره(?:\s|$)")
# «ضيف 5 فلتر زيت للمخزون», «حطها لي في المخزون», «زود عدد البوجيه», «صنف جديد».
_STOCK = re.compile(
    r"(?:^|\s)(?:ضيف|اضيف|اضف|ضيفلي|زود|زودلي|دخل|ادخل|دخللي|حط|حطلي|حطها|حطه|نزل|نزللي|سجل)\s+(?:.*\s)?"
    r"(?:المخزون|المخزن|للمخزون|للمخزن|بالمخزون|بالمخزن)(?:\s|$)"
    r"|(?:^|\s)(?:منتج|منتجات|صنف|اصناف|بضاعه)\s+(?:في\s+|ف\s+|علي\s+)?(?:المخزون|المخزن|للمخزون|للمخزن)(?:\s|$)"
    r"|(?:^|\s)(?:منتج|صنف|قطعه)\s+جديد(?:ه)?(?:\s|$)"
    r"|(?:^|\s)(?:زود|زودلي)\s+(?:لي\s+)?(?:عدد|كميه)(?:\s|$)")
# What a stock request says besides the part: left out of «ضيف 5 فلتر زيت للمخزون».
_STOCK_WORDS = frozenset(
    "ضيف اضيف اضف ضيفلي زود زودلي دخل ادخل دخللي حط حطلي حطها حطه نزل نزللي سجل لي ليا في ف علي على "
    "المخزون المخزن للمخزون للمخزن بالمخزون بالمخزن منتج منتجات صنف اصناف بضاعه قطعه جديد جديده عدد كميه "
    "من لو سمحت عايز عاوز عايزك عاوزك انا يا و كمان".split())
MOST_STOCK = 9999                  # pieces of one part at once, by voice
MISSES = 3                         # a name or a part not found this many times: let go
_KIND_SALE = re.compile(r"مبيع|بيع")
_KIND_PURCHASE = re.compile(r"مشتري|شرا")
PIN_LOCK_TRIES = 6                 # wrong PINs on one robot, whatever was asked for: locked a while
PIN_LOCK_S = 15 * 60
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


def _pin_fail_key(device) -> str:
    return f"robot:wizard:pinfail:{device.pk}"


def _pin_locked(device) -> bool:
    try:
        return (cache.get(_pin_fail_key(device)) or 0) >= PIN_LOCK_TRIES
    except Exception:
        return False


def _pin_failed(device) -> None:
    key = _pin_fail_key(device)
    try:
        cache.add(key, 0, PIN_LOCK_S)
        cache.incr(key)
    except Exception:              # the cache down: the per-request limit still holds
        pass


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
    state = _get(device)
    if state and state.get("step") == "pin":
        # 🐛 [FIX]: from the shop's log — the invoice off after «wrong PINs», the real one said next
        # («The رقم 9988.») was kept on «🎓 تعلّم السمع» and in the log. What follows a PIN
        # step let go of isn't kept either, for as long as an answer to it would come.
        try:
            cache.set(_hush_key(device), 1, ANSWER_S)
        except Exception:
            pass
    cache.delete(_key(device))


def _hush_key(device) -> str:
    return f"robot:wizard:hush:{device.pk}"


def active(device) -> bool:
    """An answer to the wizard's own question, just asked: no «يا موس» needed."""
    import time
    state = _get(device)
    return bool(state) and time.time() - state.get("asked_at", 0) <= ANSWER_S


def wants_secret(device) -> bool:
    """The robot just asked for a PIN (or let go of asking, a moment ago): what's said
    next is never kept."""
    state = _get(device)
    if state and state.get("step") == "pin":
        return True
    try:
        return bool(cache.get(_hush_key(device)))
    except Exception:
        return False


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


_QUIT_WORDS = frozenset({"الغي", "الغيها", "الغيه", "الغاء", "كنسل", "cancel"})
_QUIT_SAID = ("مش عايز حاجه", "مش عاوز حاجه", "خلاص مش عايز", "خلاص مش عاوز")
_THANKS = frozenset({"شكرا", "متشكر", "متشكرين", "تسلم", "تسلملي"})
_THANKS_TOO = frozenset({"لك", "ليك", "خلاص", "لا", "يا", "موس", "اوي", "جدا", "كده", "بقولك", "انا", "اقولك"})
# Steps that wait for a name, a part or a number: «شكرا» there means they're done.
_ASKS_A_NAME = frozenset({"kind", "pin", "customer", "walk_in_name", "vendor", "item", "choose", "new_ok",
                          "new_number", "new_price", "new_cost", "stock_qty", "amount", "description"})


def _quits(text: str) -> bool:
    said = wakename.normalize(text or "")
    return bool(_QUIT_WORDS & set(said.split())) or any(phrase in said for phrase in _QUIT_SAID)


def _thanks(text: str) -> bool:
    words = wakename.normalize(text or "").split()
    return bool(_THANKS & set(words)) and all(w in _THANKS or w in _THANKS_TOO for w in words)


def _missed(device, state, reply: str, action: str):
    """A name or a part not found: asked again — MISSES of those and it's let go."""
    state["misses"] = state.get("misses", 0) + 1
    if state["misses"] >= MISSES:
        _end(device)
        return "ملقتش اللي بتقوله، فلغيت ومسجلتش حاجة. جرب تاني لما تحب.", {"action": "wizard_cancelled"}
    _put(device, state)
    return reply, {"action": action}


def _not_a_name(name: str) -> bool:
    """A sentence, not a name («لا اعملها لي، حطها لي في المخزون…»): never read back to them."""
    return len(name.split()) > 4


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------

def _kind_of(text: str, expense: bool = True):
    """'sale', 'purchase', 'expense', '' (an invoice, which one not said) or None."""
    said = wakename.normalize(text or "")
    if _PURCHASE.search(said):
        return "purchase"
    if _SALE.search(said):
        return "sale"
    if _STOCK.search(said):
        return "stock"
    if expense and _EXPENSE.search(said) and (entries.parse_expense(text) or {}).get("amount") is None:
        return "expense"                              # «سجل مصروف 200 بنزين» is entries' at once
    if _ANY_INVOICE.search(said):
        return ""
    return None


def start(text: str, device, employee):
    """«اعملي فاتورة مبيعات» (or a purchase, an expense) → its first question;
    None when the sentence isn't asking for one."""
    kind = _kind_of(text)
    if kind is None:
        return None
    if not kind:
        # 🐛 [FIX]: «عايز أعمل فاتورة» (from the shop's log) went to the AI: which one is asked.
        _put(device, {"kind": "", "action": "", "lines": [], "step": "kind"})
        return "تمام، فاتورة إيه؟ مبيعات ولا مشتريات؟", {"action": "wizard_kind"}
    state = {"kind": kind, "action": _ACTION.get(kind, kind), "lines": [], "step": ""}
    if kind == "stock":
        state["first"] = _stock_rest(text)              # «ضيف 5 فلتر زيت للمخزون»: the part, said at once
    return _open(device, state, employee)


_ACTION = {"stock": "intake"}                         # robot/permissions: «إدخال بضاعة»


def _stock_rest(text: str) -> str:
    words = [w for w in _said(text).split() if w not in _STOCK_WORDS and w not in _RAMBLE]
    if len(words) > 5 or _QUIT_WORDS & set(words) or {"فاتوره", "الفاتوره"} & set(words):
        return ""                                     # a sentence about something else, not a part's name
    return " ".join(words) if any(not w.isdigit() for w in words) else ""


_RAMBLE = frozenset("لا اه ااا يعني اعمل اعملها اعملي واعمل خلاص طيب".split())


def _open(device, state: dict, employee):
    staff = _staff(device, employee)
    if staff is None:
        if _pin_locked(device):
            _end(device)
            return ("اتقال رقم سري غلط كذا مرة. استنى ربع ساعة، أو خلي الكاميرا تشوفك.",
                    {"action": "wizard_pin_locked"})
        state["step"] = "pin"
        _put(device, state)
        return ("تمام. قولي الرقم السري بتاعك الأول (بتاع صفحة الكاشير)؟",
                {"action": "wizard_pin", "kind": state["kind"]})
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
    if kind == "stock":
        state["step"] = "item"
        first = state.pop("first", "")
        if first:
            return _on_item(first, device, state, staff)
        _put(device, state)
        return "تمام، إضافة للمخزون. القطعة إيه؟ والعدد كام؟", {"action": "wizard_item"}
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
    step = state.get("step")
    # «فاتورة مبيعات» said again, mid-way (the first try misheard, or another one wanted):
    # begun again — the PIN's wrong tries kept, never a way round them.
    again = _kind_of(text, expense=False) if step != "kind" else None
    if _cancel(text) or _quits(text):
        # 🐛 [FIX]: from the shop's log — «الغي الفاتوره واعمل لي… حط لي منتج في المخزون» at «من أنهي
        # مورد؟» was looked up as a vendor's name, and so was «خلاص، مش عايز حاجة». «الغي» stops it
        # anywhere in the sentence — or, when something else is asked for with it, begins that.
        if again in (None, "", state.get("kind")):
            _end(device)
            return "ماشي، لغيت ومسجلتش حاجة.", {"action": "wizard_cancelled"}
    elif step in _ASKS_A_NAME and _thanks(text):
        _end(device)                                  # «شكرا» to «من أنهي مورد؟»: they're done
        return "ماشي، لغيت ومسجلتش حاجة.", {"action": "wizard_cancelled"}
    if again == "stock" and state.get("kind") == "stock":
        # «ضيف 3 بوجيه للمخزون» while adding: the next part, not a fresh start (what's heard stays)
        rest = _stock_rest(text)
        if rest:
            return _on_item(rest, device, state, employee)
        again = None
    if again is not None:
        fresh = start(text, device, employee)
        if fresh is not None and step == "pin" and (_get(device) or {}).get("step") == "pin":
            renewed = _get(device)
            renewed["pin_tries"] = state.get("pin_tries", 0)
            _put(device, renewed)
        return fresh
    handler = {"kind": _on_kind, "pin": _on_pin, "new_ok": _on_new_ok, "new_number": _on_new_number,
               "new_price": _on_new_price, "new_cost": _on_new_cost, "stock_qty": _on_stock_qty, "customer": _on_customer, "walk_in_name": _on_walk_in_name,
               "vendor": _on_vendor, "item": _on_item, "choose": _on_choose, "confirm": _on_confirm,
               "cost": _on_cost, "more": _on_more, "payment": _on_payment, "amount": _on_amount,
               "description": _on_description}.get(step)
    if handler is None:
        _end(device)
        return None
    return handler(text, device, state, employee)


def _on_kind(text, device, state, employee):
    said = wakename.normalize(text or "")
    if _KIND_PURCHASE.search(said):
        kind = "purchase"
    elif _KIND_SALE.search(said):
        kind = "sale"
    elif _EXPENSE.search(said):
        kind = "expense"
    else:
        state["kind_tries"] = state.get("kind_tries", 0) + 1
        if state["kind_tries"] >= 2:
            _end(device)
            return ("ماشي. لما تحب قول «فاتورة مبيعات» أو «فاتورة مشتريات».",
                    {"action": "wizard_cancelled"})
        _put(device, state)
        return "مبيعات ولا مشتريات؟", {"action": "wizard_kind"}
    state.update(kind=kind, action=kind)
    return _open(device, state, employee)


# Said digit by digit, a PIN's «واحد» and «صفر» are digits too (to_digits keeps «واحد»: «حتة واحدة»).
_PIN_WORDS = {"واحد": "1", "واحده": "1", "صفر": "0", "زيرو": "0", "zero": "0", "one": "1"}


def _pin_digits(text: str) -> str:
    words = [_PIN_WORDS.get(w, w) for w in wakename.normalize(text or "").split()]
    return "".join(re.findall(r"\d", to_digits(" ".join(words))))


def _on_pin(text, device, state, employee):
    pin = _pin_digits(text)
    if len(pin) < 4:
        # 🐛 [FIX]: «طرق مبيع» (a misheard «فاتورة مبيعات») counted as a wrong PIN: three of those
        # and the invoice was off before the PIN was ever said. Words with no number aren't a try.
        state["pin_misses"] = state.get("pin_misses", 0) + 1
        if state["pin_misses"] >= PIN_TRIES:
            _end(device)
            return ("ماشي، لغيت الفاتورة. لما تحب قول «فاتورة مبيعات» تاني.",
                    {"action": "wizard_cancelled"})
        _put(device, state)
        return ("مستني الرقم السري بس: قوله رقم رقم، أربع أرقام أو أكتر. ولو عايز تلغي قول «الغي».",
                {"action": "wizard_pin"})
    if _pin_locked(device):
        _end(device)
        return ("اتقال رقم سري غلط كذا مرة. استنى ربع ساعة، أو خلي الكاميرا تشوفك.",
                {"action": "wizard_pin_locked"})
    staff = _employee_for_pin(pin) if len(pin) <= 8 else None
    if staff is None:
        _pin_failed(device)
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
    if _not_a_name(name):
        return _missed(device, state, "قول اسم العميل بس، ولو نقدي قول «نقدي».", "wizard_customer")
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
    vendor = entries.find_vendor(name) if name and not _not_a_name(name) else None
    if vendor is None:
        if not name or _not_a_name(name):
            return _missed(device, state, "قول اسم المورد بس زي ما هو متسجل، أو «الغي».", "wizard_vendor")
        return _missed(device, state, f"مش لاقي مورد اسمه {name}. قول اسمه زي ما هو متسجل؟ "
                                      "ولو جديد ضيفه من شاشة الموردين.", "wizard_vendor")
    state.update(vendor_id=vendor.pk, vendor_name=vendor.name, step="item")
    _put(device, state)
    return f"تمام، {vendor.name}. القطعة إيه؟", {"action": "wizard_item"}


def _on_item(text, device, state, employee):
    if state["kind"] == "stock":
        qty, query = _stock_counted(_said(text))
    else:
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
    if product is None and state["kind"] == "stock" and not _not_a_name(query):
        # Not in the ERP: a new part, asked for step by step.
        state.update(step="new_ok", pending={"product_id": None, "name": query[:200], "quantity": qty})
        _put(device, state)
        return f"مش لاقي «{query}» في الأصناف. أضيفه صنف جديد؟", {"action": "wizard_new"}
    if product is None:
        if _not_a_name(query):
            return _missed(device, state, "مش لاقي القطعة دي. قول اسمها أو رقمها بس، أو «الغي».", "wizard_item")
        return _missed(device, state, f"مش لاقي «{query}» في الأصناف. قول اسمها تاني أو رقمها؟", "wizard_item")
    return _offer(device, state, product, qty)


def _stock_counted(said: str):
    """(how many or None when not said, the part): «3 بوجيه», «بوجيه 3», «بوجيه»."""
    said = " ".join(w for w in said.split() if w not in _STOCK_WORDS or w.isdigit())
    words = said.split()
    if words and words[-1].isdigit() and len(words[-1]) <= 4 and len(words) > 1:
        return int(words[-1]), " ".join(words[:-1])
    told = bool(re.match(r"^\d{1,4}\s", said)) or (words and (words[0] in entries._COUNT_WORDS
                                                             or entries.two_of(words[0])))
    qty, query = entries._counted(said)
    return (qty if told else None), query


def talk_ordinal(i: int) -> str:
    return ("الأولاني", "التاني", "التالت")[i] if i < 3 else ""


def _on_choose(text, device, state, employee):
    from inventory.models import Product

    products = Product.objects.in_bulk(state.get("choices") or [])
    choices = [products[pk] for pk in state.get("choices") or [] if pk in products]
    picked = talk.pick_choice(text, [{"name": p.name, "model": ""} for p in choices])
    if picked is None:
        return _on_item(text, device, state, employee)        # a part's name said instead
    qty = state.get("quantity")
    return _offer(device, state, choices[picked], qty if state["kind"] == "stock" else (qty or 1))


def _offer(device, state, product, qty):
    if state["kind"] == "stock":
        pending = {"product_id": product.pk, "name": product.name, "quantity": qty}
        if not qty:
            state.update(step="stock_qty", pending=pending)
            _put(device, state)
            return f"{product.name}. تزود كام حتة؟", {"action": "wizard_qty"}
        return _stock_confirm(device, state, pending)
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
    if state["kind"] == "stock":
        return _hand_over(device, state, False)      # no money changes hands: the branch next
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
    if state["kind"] == "stock":
        said = [f"{line['quantity']} {line['name']}" + (" (صنف جديد)" if not line.get("product_id") else "")
                for line in lines]
        reply = entries.ask(device, kind="stock", summary=f"إضافة للمخزون: {' و'.join(said)}", amount=0,
                            employee=staff, data={"lines": lines})
        return reply, {"action": "entry_ask_branch", "kind": "stock", "lines": len(lines)}
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


# ---------------------------------------------------------------------------
# Pieces into the stock
# ---------------------------------------------------------------------------

def _count(text: str):
    """How many pieces were said («3», «تلاتة», «حتتين»), or None."""
    said = _said(text)
    numbers = re.findall(r"\d+", said)
    if numbers:
        return int(numbers[0])
    words = said.split()
    if words and (words[0] in entries._COUNT_WORDS or entries.two_of(words[0])):
        return entries._counted(said + " x")[0]
    return None


def _money(text: str):
    numbers = re.findall(r"\d+(?:\.\d+)?", _said(text))
    return Decimal(numbers[0]).quantize(Decimal("0.01")) if numbers else None


def _stock_confirm(device, state, pending: dict):
    from inventory.models import Inventory, Product

    qty = pending["quantity"]
    if not 1 <= qty <= MOST_STOCK:
        state["step"] = "stock_qty"
        state["pending"] = pending
        _put(device, state)
        return f"العدد لازم يكون من حتة لـ {MOST_STOCK}. تزود كام حتة؟", {"action": "wizard_qty"}
    state.update(step="confirm", pending=pending)
    _put(device, state)
    count = "حتة واحدة" if qty == 1 else f"{qty} حتت"
    if not pending.get("product_id"):
        number = f"، رقمه {pending['part_number']}" if pending.get("part_number") else ""
        cost = (f"، والشرا {services.spoken_price(Decimal(pending['cost']))}"
                if Decimal(pending.get("cost") or 0) > 0 else "")
        return (f"صنف جديد: {pending['name']}{number}، سعر البيع {services.spoken_price(Decimal(pending['retail']))}"
                f"{cost}، {count}. أسجله؟", {"action": "wizard_confirm"})
    product = Product.objects.filter(pk=pending["product_id"]).first()
    branch = getattr(device, "branch", None)
    have = (Inventory.objects.filter(product=product, branch=branch).values_list("quantity", flat=True).first()
            if branch is not None else None)
    if have is None:
        return f"{pending['name']}: أزود {count}؟", {"action": "wizard_confirm", "product_id": product.pk}
    return (f"{pending['name']}: عندك منه {have} في الفرع ده. أزود {count} تبقى {have + qty}؟",
            {"action": "wizard_confirm", "product_id": product.pk})


def _on_stock_qty(text, device, state, employee):
    qty = _count(text)
    if qty is None:
        return "تزود كام حتة؟ قول العدد.", {"action": "wizard_qty"}
    pending = state["pending"]
    pending["quantity"] = qty
    return _stock_confirm(device, state, pending)


def _on_new_ok(text, device, state, employee):
    if talk.just_yes(text) or _said(text) in ("ضيفه", "ضيفها", "اضيفه", "ضيف", "جديد"):
        state["step"] = "new_number"
        _put(device, state)
        return "رقم القطعة إيه؟ ولو مش عارفه قول «مفيش».", {"action": "wizard_new_number"}
    if _no(text):
        state.pop("pending", None)
        state["step"] = "item"
        _put(device, state)
        return "ماشي. القطعة إيه؟", {"action": "wizard_item"}
    return _on_item(text, device, state, employee)            # the part's name said again, better


_NO_NUMBER = frozenset("مفيش مافيش لا معرفش مش معنديش ملهاش مالهاش".split())


def _on_new_number(text, device, state, employee):
    from inventory.models import Product

    words = _said(text).split()
    pending = state["pending"]
    if words and words[0] in _NO_NUMBER:
        number = ""
    else:
        spoken = [_PIN_WORDS.get(w, w) for w in wakename.normalize(text or "").split()]
        number = re.sub(r"[^0-9A-Za-z]", "", to_digits(" ".join(spoken))).upper()[:100]
        if len(number) < 3:
            return "رقم القطعة إيه؟ قوله رقم رقم، ولو مش عارفه قول «مفيش».", {"action": "wizard_new_number"}
        product = Product.objects.filter(part_number__iexact=number).first()
        if product is not None:                       # it was there all along, under its number
            return _offer(device, state, product, pending.get("quantity"))
    pending["part_number"] = number
    state["step"] = "new_price"
    _put(device, state)
    return "سعر البيع كام؟", {"action": "wizard_new_price"}


def _on_new_price(text, device, state, employee):
    price = _money(text)
    if price is None or price <= 0:
        return "سعر البيع كام؟ قول المبلغ.", {"action": "wizard_new_price"}
    state["pending"]["retail"] = str(price)
    state["step"] = "new_cost"
    _put(device, state)
    return "واشتريت الحتة بكام؟ ولو مش عارف قول «مش عارف».", {"action": "wizard_new_cost"}


def _on_new_cost(text, device, state, employee):
    words = _said(text).split()
    cost = _money(text)
    if cost is None and not (words and words[0] in _NO_NUMBER):
        return "اشتريت الحتة بكام؟ ولو مش عارف قول «مش عارف».", {"action": "wizard_new_cost"}
    pending = state["pending"]
    pending["cost"] = str(cost or Decimal("0.00"))
    if not pending.get("quantity"):
        state["step"] = "stock_qty"
        _put(device, state)
        return "وهتضيف كام حتة؟", {"action": "wizard_qty"}
    return _stock_confirm(device, state, pending)
