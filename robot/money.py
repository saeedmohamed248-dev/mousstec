"""
robot/money.py — money the robot takes in and pays out by voice, besides invoices and expenses.

    «يا موس، حصّل 500 جنيه من أحمد محمود»     → تحصيل من عميل آجل (inventory.services.customer_payments)
    «يا موس، ادفع 1000 للمورد الأمل»          → دفعة لمورد (inventory.services.vendor_payments)
    «يا موس، سجل سلفة 500 للموظف محمد علي»    → سلفة موظف (hr.services.advance_service)

Each is held like an expense (robot/entries.py): the robot says what it understood and asks
«أسجلها على أنهي فرع؟»; the money moves through that branch's cash treasury, by the ERP's own
services — the same rules as its screens:

  • A collection: a customer registered by name, never «عميل نقدي» (shared by everyone: collected
    from the invoice itself), and never more than what they owe.
  • A vendor payment: never more than what the shop owes them (a number misheard would be an
    advance nobody meant) — advances stay on the vendors' screen.
  • An advance: HR's policy (the most of a salary, one at a time). Whoever may approve advances
    (an HR manager, the company's admin) has it approved and paid at once; anyone else's is a
    request, waiting for them.

Who may: robot/permissions (`collect`, `vendor_pay`, `advance`) — the face the camera knows, or a
PIN (robot/wizard.py).
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from . import wakename

_NUMBER = r"(\d+(?:[.,]\d+)?)"
_MONEY = r"\s*(?:جنيهات|جنيها|جنيه|جنية|ج\.?م|egp)?(?!\w)"
_LEAD = r"^(?:(?:سجل|سجلي|اعمل|اعملي|اكتب|ضيف|اضف)\s+)?"
# On normalized words (wakename.normalize: ة→ه، أ→ا، no tashkeel).
_COLLECT = re.compile(_LEAD + r"(?:تحصيل|حصل|حصلي|حصلت|حصلنا|استلمت|استلمنا|استلم|قبضت|قبضنا)\s+(.+)$")
_PAID_US = re.compile(r"^العميل\s+(.+?)\s+(?:دفع|دفعلي|دفعلنا|سدد|سددلنا)\s+(.+)$")
_VENDOR = re.compile(_LEAD + r"(?:ادفع|ادفعلي|ندفع|سدد|سددنا|سددت|دفعنا|دفعت|دفعه|سداد)\s+(.+)$")
_VENDOR_WORD = re.compile(r"(?:^|\s)(?:لل?مورد|مورد|المورد)(?:\s|$)")
_ADVANCE = re.compile(_LEAD + r"(?:(?:اصرف|ادي|اديله|ادي له)\s+)?(?:سلفه|سلفيه)\s+(.+)$")
_NAME_LEAD = re.compile(r"^(?:من|ل|لل|الي|علي|عند|باسم|العميل|للعميل|المورد|للمورد|لمورد|مورد|الموظف|للموظف|لموظف|"
                        r"الاستاذ|للاستاذ|استاذ|الاسطي|للاسطي|يا)\s+")
_NAME_TAIL = re.compile(r"\s+(?:كاش|نقدي|نقدا|من حسابه|من الحساب|علي الحساب|تحت الحساب|لو سمحت)$")

KINDS = ("collection", "vendor_pay", "advance")
EXAMPLES = {
    "collection": "«حصّل 500 جنيه من أحمد محمود»",
    "vendor_pay": "«ادفع 1000 جنيه للمورد الأمل»",
    "advance": "«سجل سلفة 500 جنيه للموظف محمد علي»",
}


def _said(text: str) -> str:
    from .entries import _said as digits        # amounts said in words: «خمسمية» → 500
    return wakename.normalize(digits(text))


def _amount_and_name(rest: str):
    """(amount or None, the name left) from «500 جنيه من احمد محمود» / «لمحمد علي 500»."""
    from .entries import _to_decimal

    num = re.search(_NUMBER + _MONEY, rest)
    amount = _to_decimal(num.group(1)) if num else None
    name = (rest[:num.start()] + " " + rest[num.end():]) if num else rest
    name = " ".join(name.split())
    for _ in range(3):                          # «من العميل الاستاذ احمد»
        name = _NAME_LEAD.sub("", name)
    name = _NAME_TAIL.sub("", name).strip()
    return amount, name


def parse(text: str):
    """{"kind", "amount", "name"} for a collection, a vendor payment or an advance — else None.
    `amount` None / `name` '' when it's clearly one and that wasn't said (asked for)."""
    t = _said(text)
    m = _ADVANCE.match(t)
    if m:
        amount, name = _amount_and_name(m.group(1))
        return {"kind": "advance", "amount": amount, "name": name}
    m = _COLLECT.match(t)
    if m:
        amount, name = _amount_and_name(m.group(1))
        # «استلمت الطلب من أحمد» is talk; «حصّل من أحمد» with no amount is asked for it.
        if amount is not None or re.match(_LEAD + r"(?:تحصيل|حصل|حصلي)\s", t):
            return {"kind": "collection", "amount": amount, "name": name}
    m = _VENDOR.match(t)
    if m and _VENDOR_WORD.search(m.group(1)):   # «دفعنا 200 كهربا» is an expense (robot/entries.py)
        amount, name = _amount_and_name(_VENDOR_WORD.sub(" ", m.group(1), count=1).strip())
        return {"kind": "vendor_pay", "amount": amount, "name": name}
    m = _PAID_US.match(t)
    if m and re.search(_NUMBER, m.group(2)) and not _VENDOR_WORD.search(t):
        amount, _rest = _amount_and_name(m.group(2))
        _none, name = _amount_and_name(m.group(1))
        if name and amount is not None and len(name.split()) <= 4:
            return {"kind": "collection", "amount": amount, "name": name}
    return None


def is_request(text: str) -> bool:
    return parse(text) is not None


# ---------------------------------------------------------------------------
# Who's named
# ---------------------------------------------------------------------------

def _loose(name: str) -> str:
    """A name the way it's compared: normalized, a leading «ل» of «لمحمد» let go by the caller."""
    return wakename.normalize(name)


def find_employee(name: str):
    """(the active employee called `name`, how many are): the full name, or a part of it
    that only one has («محمد» with two Mohameds: asked for the whole name)."""
    from hr.models import Employee

    want = _loose(name)
    if len(want) < 2:
        return None, 0
    exact, partial = [], []
    for emp in Employee.objects.select_related("user").filter(is_active=True, user__is_active=True)[:2000]:
        full = _loose(emp.user.get_full_name() or "")
        names = {full, _loose(emp.user.first_name or ""), _loose(emp.user.username or "")} - {""}
        if want in names:
            exact.append(emp)
        elif full and all(w in full.split() for w in want.split()):
            partial.append(emp)
    found = exact or partial
    return (found[0] if len(found) == 1 else None), len(found)


def _person(kind: str, name: str):
    """(who, how many match, what they're called) for the name said — a second try without a
    leading «ل» («لمحمد علي»)."""
    from . import entries

    tries = [name] + ([name[1:]] if name.startswith("ل") and len(name) > 3 else [])
    for said in tries:
        if kind == "collection":
            who, count = entries.find_customer(said)
        elif kind == "vendor_pay":
            who = entries.find_vendor(said)
            count = 1 if who is not None else 0
        else:
            who, count = find_employee(said)
        if who is not None or count:
            return who, count
    return None, 0


def _money(value) -> str:
    from .services import spoken_price
    return spoken_price(value)


# ---------------------------------------------------------------------------
# Asked for by voice
# ---------------------------------------------------------------------------

_ACTION = {"collection": "collect", "vendor_pay": "vendor_pay", "advance": "advance"}
_WHO = {"collection": "العميل", "vendor_pay": "المورد", "advance": "الموظف"}


def voice_request(text: str, device, employee):
    """A collection, a vendor payment or an advance from speech: (reply, payload), or None."""
    from . import entries, permissions

    asked = parse(text)
    if asked is None:
        return None
    kind = asked["kind"]
    if not permissions.employee_can(employee, _ACTION[kind]):
        return permissions.denial_message(_ACTION[kind]), {"action": "denied"}
    if asked["amount"] is None or not asked["name"]:
        return f"قولها كاملة بالمبلغ والاسم، مثلاً: {EXAMPLES[kind]}.", {"action": "entry_incomplete", "kind": kind}
    amount = asked["amount"]
    who, count = _person(kind, asked["name"])
    if who is None:
        if count > 1:
            return (f"فيه أكتر من واحد اسمه {asked['name']}. قولها تاني باسمه كامل زي ما هو متسجل.",
                    {"action": "entry_incomplete", "kind": kind})
        where = {"collection": "العملاء", "vendor_pay": "الموردين", "advance": "الموظفين"}[kind]
        return (f"مش لاقي {_WHO[kind]} «{asked['name']}» — اتأكد من الاسم زي ما هو متسجل في شاشة {where}.",
                {"action": "entry_incomplete", "kind": kind})

    if kind == "collection":
        owed = Decimal(str(who.balance or 0))
        if owed <= 0:
            return f"{who.name} مش عليه حاجة دلوقتي، فمسجلتش تحصيل.", {"action": "entry_incomplete", "kind": kind}
        if amount > owed:
            return (f"المبلغ أكبر من اللي على {who.name}: عليه {_money(owed)} بس. قول المبلغ الصح.",
                    {"action": "entry_incomplete", "kind": kind})
        summary = f"تحصيل {_money(amount)} من {who.name} (عليه {_money(owed)})"
        data = {"customer_id": who.pk, "amount": str(amount)}
    elif kind == "vendor_pay":
        owed = Decimal(str(who.balance or 0))
        if owed <= 0:
            return (f"مفيش حاجة علينا للمورد {who.name} دلوقتي. الدفعة المقدمة بتتسجل من شاشة الموردين.",
                    {"action": "entry_incomplete", "kind": kind})
        if amount > owed:
            return (f"المبلغ أكبر من اللي علينا للمورد {who.name}: {_money(owed)} بس. قول المبلغ الصح.",
                    {"action": "entry_incomplete", "kind": kind})
        summary = f"دفعة {_money(amount)} للمورد {who.name} (علينا له {_money(owed)})"
        data = {"vendor_id": who.pk, "amount": str(amount)}
    else:
        called = who.user.get_full_name() or who.user.username
        summary = f"سلفة {_money(amount)} للموظف {called}"
        data = {"employee_id": who.pk, "amount": str(amount)}
    reply = entries.ask(device, kind=kind, summary=summary, amount=amount, employee=employee, data=data)
    return reply, {"action": "entry_ask_branch", "kind": kind}


# ---------------------------------------------------------------------------
# Writing to the ERP
# ---------------------------------------------------------------------------

def _why(exc: ValidationError) -> str:
    return " ".join(str(m) for m in getattr(exc, "messages", None) or [str(exc)])


def record(entry, branch):
    """Write a held collection / vendor payment / advance on `branch` (raises entries.EntryError).
    Returns the words to say when they aren't the usual «تمام، سجلت …» — else ''."""
    from . import entries
    from .brain import talk

    amount = Decimal(str(entry.data["amount"]))
    with transaction.atomic():
        treasury = entries.cash_treasury(branch, lock=True)
        if treasury is None:
            raise entries.EntryError(f"فرع {talk.branch_said(branch.name)} مالوش خزنة كاش، فمسجلتش حاجة.")
        try:
            if entry.kind == "collection":
                from inventory.models import Customer
                from inventory.services import customer_payments

                customer = Customer.objects.filter(pk=entry.data["customer_id"]).first()
                if customer is None:
                    raise entries.EntryError("العميل ده مبقاش موجود، فمسجلتش حاجة.")
                customer_payments.collect(customer, amount, treasury, note="🤖 بالصوت")
                return ""
            if entry.kind == "vendor_pay":
                from inventory.models import Vendor
                from inventory.services import vendor_payments

                vendor = Vendor.objects.select_for_update().filter(pk=entry.data["vendor_id"]).first()
                if vendor is None:
                    raise entries.EntryError("المورد ده مبقاش موجود، فمسجلتش حاجة.")
                if amount > Decimal(str(vendor.balance or 0)):      # paid meanwhile, from the screen
                    raise entries.EntryError(f"اللي علينا للمورد {vendor.name} بقى أقل من المبلغ، فمسجلتش حاجة.")
                vendor_payments.pay_vendor(vendor, amount, treasury, note="🤖 بالصوت")
                return ""
            if entry.kind == "advance":
                return _advance(entry, amount, treasury)
        except ValidationError as exc:
            raise entries.EntryError(f"{_why(exc)} فمسجلتش حاجة.") from exc
    raise entries.EntryError("النوع ده مش بيتسجل بالصوت.")


def _advance(entry, amount: Decimal, treasury) -> str:
    from hr.models import Employee
    from hr.services.advance_service import AdvanceService
    from hr.services.leave_service import is_company_admin

    from . import entries

    who = Employee.objects.select_related("user").filter(pk=entry.data["employee_id"]).first()
    if who is None:
        raise entries.EntryError("الموظف ده مبقاش موجود، فمسجلتش حاجة.")
    called = who.user.get_full_name() or who.user.username
    asker = entry.employee
    advance = AdvanceService.request_advance(who, amount, 1, reason="🤖 اتطلبت بالصوت من الروبوت")
    user = getattr(asker, "user", None)
    may_approve = asker is not None and (is_company_admin(user) or asker.is_hr_manager
                                         or getattr(user, "is_superuser", False))
    if not may_approve:
        return (f"سجلت طلب سلفة {_money(amount)} للموظف {called}، ومستني موافقة مدير الموارد البشرية "
                "من شاشة السلف. الفلوس مخرجتش من الخزنة لسه.")
    AdvanceService.approve_advance(advance.pk, asker, by_user=user)
    AdvanceService.disburse_advance(advance.pk, treasury_id=treasury.pk, by_user=user)
    return ""
