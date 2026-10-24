"""
robot/entries.py — invoices and expenses the robot records, always on a branch
someone names.

Before ANY invoice or expense is written, the robot asks which branch it
belongs to. By voice:

    «يا موس، سجل مصروف 200 جنيه بنزين»
        → «تمام: مصروف 200 جنيه (بنزين). أسجله على أنهي فرع؟ …»
    «المعادي»   → recorded on the المعادي branch, paid from its cash treasury
    «هنا»       → recorded on the robot's own branch
    «الغي»      → nothing is recorded

Purchases work the same way («اشترينا 3 فلتر زيت من الأمل بسعر 150 كاش»), and
sales («بعت 2 فلتر زيت كاش», «بعت لأحمد محمود فلتر زيت آجل»): at the part's
retail price only, on credit only to a customer registered by name — never
«عميل نقدي» — and refused when the named branch hasn't got the pieces. The
`/sale/` endpoint asks too: without a `branch_id` it answers with the question
and the branch list instead of creating the invoice.

Amounts and counts are heard in words too, the way speech-to-text writes them
(«سجل مصروف ميتين وخمسين جنيه بنزين», «بعت اتنين فلتر زيت بأربعمية وخمسين»):
robot/brain/arabic_numbers.py turns them into digits, and leaves «طرمبة مية»
a water pump. The summary repeats the amount before anything is written.

Every request becomes a `RobotEntry`: pending until answered, then done (with
links to the invoice/transaction it created), cancelled, failed (with the
reason) or expired. The dashboard lists these, so it shows only what the robot
did and on which branch.

Money rules match the ERP screens: an expense or a cash purchase is paid from
the chosen branch's active cash treasury and is refused when that treasury
can't cover it; a cash sale is refused when the branch has no cash treasury
(otherwise the money would be recorded nowhere).
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from . import wakename
from .brain.arabic_numbers import to_digits

PENDING_MINUTES = 3

# Answers that mean "the branch the robot is in".
_HERE = {"هنا", "هنا يا موس", "الفرع ده", "الفرع دا", "الفرع هنا", "نفس الفرع",
         "فرعنا", "عندنا", "here", "this branch"}
_CANCEL = {"الغي", "الغيها", "الغيه", "لا", "لأ", "بلاش", "خلاص لا", "cancel", "no"}

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
_NUMBER = r"(\d+(?:[.,]\d+)?)"
# 🐛 [FIX]: «(?!\w)» — «مصروف 50 جوانات» كانت بتاكل «ج» من أول الكلمة اللي بعد الرقم.
_MONEY = r"\s*(?:جنيهاً|جنيها|جنيهات|جنيه|جنية|ج\.?م|ج|egp|pounds|pound)(?!\w)"
_CURRENCY = rf"(?:{_MONEY})?"


class EntryError(Exception):
    """A request the robot can't record — the message is spoken as-is."""


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

def all_branches() -> list:
    from inventory.models import Branch
    return list(Branch.objects.order_by("name"))


def branch_by_id(value):
    """The Branch with this id, or None for a missing/bad id."""
    from inventory.models import Branch
    try:
        return Branch.objects.filter(pk=int(value)).first()
    except (TypeError, ValueError):
        return None


def _branch_key(name: str) -> str:
    t = wakename.normalize(name)
    return re.sub(r"^(فرع|الفرع)\s+", "", t).strip()


def match_branch(text: str, branches: list, here):
    """The branch named in `text`, `here` for «هنا», or None.

    Matches whole words of the branch name, with or without «فرع». When a
    longer name contains a shorter one ("مدينة نصر 2" vs "مدينة نصر"), the
    longest match wins.
    """
    t = wakename.normalize(text)
    if not t:
        return None
    if t in {wakename.normalize(h) for h in _HERE}:
        return here
    padded = " " + re.sub(r"^(فرع|الفرع)\s+", "", t) + " "
    best, best_len = None, 0
    for b in branches:
        key = _branch_key(b.name)
        if key and f" {key} " in padded and len(key) > best_len:
            best, best_len = b, len(key)
    return best


def is_cancel(text: str) -> bool:
    return wakename.normalize(text) in {wakename.normalize(c) for c in _CANCEL}


def branch_question(device, branches=None) -> str:
    """«أسجلها على أنهي فرع؟» with the branch names to choose from."""
    branches = all_branches() if branches is None else branches
    here = getattr(device.branch, "name", "")
    if len(branches) <= 1:
        return f"أسجلها على فرع {here}؟ قول «هنا» أو «الغي»."
    names = "، ".join(b.name for b in branches)
    return (f"أسجلها على أنهي فرع؟ الفروع: {names}. "
            f"قول اسم الفرع، أو «هنا» لفرع {here}، أو «الغي».")


def branch_choices(device) -> dict:
    """The /sale/ reply when no branch was chosen yet."""
    branches = all_branches()
    return {
        "needs_branch": True,
        "question": branch_question(device, branches),
        "branches": [{"id": b.pk, "name": b.name} for b in branches],
        "suggested_branch_id": device.branch_id,
    }


# ---------------------------------------------------------------------------
# Parsing what was said
# ---------------------------------------------------------------------------

_EXPENSE_RE = re.compile(
    r"^(?:(?:سجل|سجّل|سجلي|اكتب|ضيف|اضف|أضف)\s+)?(?:مصروف|مصاريف|صرف)\s+(.+)$")
_SPENT_RE = re.compile(r"^(?:صرفنا|صرفت|دفعنا|دفعت)\s+(.+)$")
_PURCHASE_RE = re.compile(
    r"^(?:(?:سجل|سجّل|سجلي|اعمل|اكتب)\s+)?"
    r"(?:فاتورة شراء|فاتوره شراء|شراء|مشتريات|اشترينا|اشتريت)\s+(.+)$")


_SALE_RE = re.compile(
    r"^(?:(?:سجل|سجّل|سجلي|اعمل|اكتب)\s+)?"
    r"(?:فاتورة بيع|فاتوره بيع|بيع|بعت|بعنا|بعتله|بعتلها)\s+(.+)$")
_CREDIT = r"آجل|اجل|أجل|على الحساب|علي الحساب|على حسابه|علي حسابه"
_CASH = r"كاش|نقدي|نقدا|نقداً|cash"
# «بعت اتنين فلتر زيت»: a count said in words.
_COUNT_WORDS = {"واحد": 1, "واحده": 1, "واحدة": 1, "اتنين": 2, "تلاته": 3, "تلاتة": 3, "ثلاثة": 3,
                "اربعه": 4, "اربعة": 4, "أربعة": 4, "خمسه": 5, "خمسة": 5, "سته": 6, "ستة": 6,
                "سبعه": 7, "سبعة": 7, "تمانيه": 8, "تمانية": 8, "تسعه": 9, "تسعة": 9,
                "عشره": 10, "عشرة": 10, "حتتين": 2, "قطعتين": 2}
_CUSTOMER_MARKERS = {"للعميل", "للزبون", "للاستاذ", "للأستاذ", "لـ"}


def _said(text: str) -> str:
    """The sentence with Arabic-Indic digits and amounts said in words as
    digits: Vosk writes «مية وخمسين جنيه», «تلات تلاف» (robot/brain/arabic_numbers.py)."""
    return to_digits((text or "").translate(_DIGITS)).strip()


def _to_decimal(s: str):
    try:
        d = Decimal(s.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None
    return d if d > 0 else None


def parse_expense(text: str):
    """{"amount", "description"} from «مصروف 200 جنيه بنزين», else None.

    Returns {"amount": None, ...} when it's clearly an expense but no amount
    was heard, so the robot can ask for it.
    """
    t = _said(text)
    m = _EXPENSE_RE.match(t) or _SPENT_RE.match(t)
    if not m:
        return None
    rest = m.group(1)
    # 🐛 [FIX]: the amount is the number said with «جنيه» when there is one:
    # «مصروف شاي 3 عمال 50 جنيه» is 50, not 3.
    num = re.search(_NUMBER + _MONEY, rest) or re.search(_NUMBER + _CURRENCY, rest)
    amount = _to_decimal(num.group(1)) if num else None
    desc = (rest[:num.start()] + " " + rest[num.end():]) if num else rest
    desc = re.sub(r"^\s*(?:على|علي|عشان|في|ل|لل)\s+", "", desc.strip())
    desc = " ".join(desc.split()).strip(" ،,.-") or "مصروف"
    return {"amount": amount, "description": desc[:200]}


def parse_purchase(text: str):
    """Parts of «اشترينا 3 فلتر زيت من الأمل بسعر 150 كاش», else None.

    {"quantity", "query", "vendor", "unit_cost", "cash"}. «بسعر X» is the
    price of one piece; «بإجمالي X» / «بـ X الكل» is the whole bill.
    """
    t = _said(text)
    m = _PURCHASE_RE.match(t)
    if not m:
        return None
    rest = f" {m.group(1)} "

    cash = bool(re.search(r"\s(كاش|نقدي|نقدا|نقداً|cash)\s", rest))
    rest = re.sub(r"\s(كاش|نقدي|نقدا|نقداً|cash|آجل|اجل|أجل|على الحساب)\s", " ", rest)

    unit_cost, total = None, None
    pm = re.search(r"\s(?:باجمالي|بإجمالي|اجمالي|إجمالي|الاجمالي|الإجمالي)\s*" + _NUMBER
                   + _CURRENCY, rest)
    if pm:
        total = _to_decimal(pm.group(1))
    else:
        pm = re.search(r"\s(?:بسعر|السعر|سعر|بـ|ب)\s*" + _NUMBER + _CURRENCY
                       + r"(\s+(?:الكل|للكل|كلهم))?", rest)
        if pm:
            value = _to_decimal(pm.group(1))
            if pm.group(2):
                total = value
            else:
                unit_cost = value
    if pm:
        rest = rest[:pm.start()] + " " + rest[pm.end():]

    vendor = None
    vm = re.search(r"\s(?:من عند|من)\s+(?:المورد\s+)?(.+?)\s*$", rest)
    if vm:
        vendor = vm.group(1).strip(" ،,.-")
        rest = rest[:vm.start()]

    rest = rest.strip()
    qty = 1
    qm = re.match(r"^(\d+)\s+(?:قطعة|قطع|حتة|حتت|عدد\s+)?\s*(.*)$", rest)
    if qm:
        qty = int(qm.group(1))
        rest = qm.group(2)
    query = rest.strip(" ،,.-") or None

    if total is not None and qty > 0:
        unit_cost = (total / qty).quantize(Decimal("0.01"))
    return {"quantity": qty, "query": query, "vendor": vendor,
            "unit_cost": unit_cost, "cash": cash}


def parse_sale(text: str):
    """Parts of «بعت 2 فلتر زيت لأحمد محمود آجل», else None.

    {"quantity", "query", "customer", "same_name", "credit", "price_said"}:
    `customer` is a registered customer named after «لـ» (checked against the
    customers, so «لمبة» stays a part), `same_name` the name when more than one
    customer has it, `price_said` the piece price when one was said.
    """
    t = _said(text)
    m = _SALE_RE.match(t)
    if not m:
        return None
    rest = f" {m.group(1)} "
    credit = bool(re.search(rf"\s(?:{_CREDIT})\s", rest))
    rest = re.sub(rf"\s(?:{_CREDIT}|{_CASH})(?=\s)", " ", rest)

    unit, total = None, None
    pm = re.search(r"\s(?:باجمالي|بإجمالي|اجمالي|إجمالي)\s*" + _NUMBER + _CURRENCY, rest)
    if pm:
        total = _to_decimal(pm.group(1))
    else:
        pm = re.search(r"\s(?:بسعر|السعر|سعر|بـ)\s*" + _NUMBER + _CURRENCY, rest)
        if pm:
            unit = _to_decimal(pm.group(1))
    if pm:
        rest = rest[:pm.start()] + " " + rest[pm.end():]

    words = rest.split()
    customer, same_name = None, ""
    for i, word in enumerate(words):
        if word in _CUSTOMER_MARKERS:
            start, first = i + 1, None
        elif word.startswith("ل") and len(word) > 2:
            start, first = i + 1, word[1:]
        else:
            continue
        for n in (3, 2, 1):
            said = ([first] if first else []) + words[start:start + n - (1 if first else 0)]
            if len(said) < n:
                continue
            found, how_many = find_customer(" ".join(said))
            if how_many > 1:
                same_name = " ".join(said)
                break
            if found is not None:
                customer = found
                del words[i:start + n - (1 if first else 0)]
                break
        if customer is not None or same_name:
            break

    rest = " ".join(words)
    qty = 1
    qm = re.match(r"^(\d+)\s+(?:قطعة|قطعه|قطع|حتة|حته|حتت|عدد)?\s*(.*)$", rest)
    if qm:
        qty, rest = int(qm.group(1)), qm.group(2)
    else:
        first, _, after = rest.partition(" ")
        if first in _COUNT_WORDS:
            qty = _COUNT_WORDS[first]
            rest = re.sub(r"^(?:قطعة|قطعه|قطع|حتة|حته|حتت)\s+", "", after)
    query = rest.strip(" ،,.-") or None
    if total is not None and qty > 0:
        unit = (total / qty).quantize(Decimal("0.01"))
    return {"quantity": qty, "query": query, "customer": customer, "same_name": same_name,
            "credit": credit, "price_said": unit}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_customer(name: str):
    """(the registered customer called `name`, how many are) — spelling
    variants aside (أ/ا، ة/ه، ى/ي). Never «عميل نقدي»: nothing is ever owed
    by everyone."""
    from inventory.models import Customer
    from inventory.models.customers import WALK_IN_PHONE
    from .wakename import normalize

    key = normalize(name)
    if len(key) < 2:
        return None, 0
    letters = {"ا": "[اأإآ]", "ه": "[هة]", "ي": "[يى]", "و": "[وؤ]"}
    pattern = r"\s+".join("".join(letters.get(ch, re.escape(ch)) for ch in word) for word in key.split())
    found = list(Customer.objects.exclude(phone=Customer.normalize_phone(WALK_IN_PHONE))
                 .filter(name__iregex=rf"^\s*{pattern}\s*$")[:2])
    return (found[0] if len(found) == 1 else None), len(found)


def find_vendor(name: str):
    """A Vendor whose name matches what was said, or None."""
    from inventory.models import Vendor
    if not name:
        return None
    exact = Vendor.objects.filter(name__iexact=name.strip()).first()
    if exact:
        return exact
    contains = Vendor.objects.filter(name__icontains=name.strip()).order_by("id").first()
    if contains:
        return contains
    # STT spells names loosely (ة/ه, أ/ا): compare normalized forms.
    want = wakename.normalize(name)
    for v in Vendor.objects.order_by("id")[:500]:
        if want and want in wakename.normalize(v.name):
            return v
    return None


def expense_category(description: str):
    """(category, salaries?) — the expense category named in the description.

    Falls back to the «أخرى» category. Salaries are not recorded by voice:
    the ERP needs the employee who's being paid.
    """
    from inventory.models import ExpenseCategory
    words = f" {wakename.normalize(description)} "
    best = None
    for c in ExpenseCategory.objects.all():
        key = wakename.normalize(c.name)
        if key and f" {key} " in words and (best is None or len(key) > len(best[1])):
            best = (c, key)
    if best:
        return best[0], best[0].system_key == "salaries"
    other = ExpenseCategory.objects.filter(system_key="other").first()
    return other, False


def cash_treasury(branch, *, lock=False):
    """The branch's active cash treasury, or None."""
    from inventory.models import Treasury
    qs = Treasury.objects.filter(branch=branch, type="cash", is_active=True).order_by("id")
    if lock:
        qs = qs.select_for_update()
    return qs.first()


# ---------------------------------------------------------------------------
# The pending → answered flow
# ---------------------------------------------------------------------------

def pending_for(device):
    """This device's open request, expiring one that waited too long."""
    from .models import RobotEntry
    entry = (RobotEntry.objects.filter(device=device, status="pending")
             .order_by("-created_at").first())
    if entry is None:
        return None
    if entry.created_at < timezone.now() - timedelta(minutes=PENDING_MINUTES):
        RobotEntry.objects.filter(device=device, status="pending").update(
            status="expired", completed_at=timezone.now())
        return None
    return entry


def is_answer(device, text: str) -> bool:
    """True when `text` names a branch while the question is open, so it
    needs no «يا موس».

    Only real branch names count: «لا» or «هنا» said to someone else nearby
    must not cancel or place the entry. Those still work within the 20 s
    conversation window, or with the name.
    """
    if pending_for(device) is None:
        return False
    return match_branch(text, all_branches(), None) is not None


def ask(device, *, kind: str, data: dict, summary: str, amount, employee=None) -> str:
    """Hold a request until a branch is named; returns the spoken question."""
    from .models import RobotEntry
    RobotEntry.objects.filter(device=device, status="pending").update(
        status="cancelled", completed_at=timezone.now(),
        message="اتلغت لما اتطلبت عملية جديدة")
    RobotEntry.objects.create(
        device=device, kind=kind, data=data, summary=summary[:255],
        amount=amount or 0, employee=employee,
    )
    return f"تمام: {summary}. {branch_question(device)}"


def answer(entry, text: str):
    """Handle a reply to the branch question.

    Returns (reply, payload) when `text` was an answer (branch or cancel), or
    None when it wasn't — the caller then treats it as a normal question.
    """
    if is_cancel(text):
        _finish(entry, "cancelled", message="اتلغت بالصوت")
        return "ماشي، لغيتها ومسجلتش حاجة.", {"action": "entry_cancelled", "entry_id": entry.pk}
    branch = match_branch(text, all_branches(), entry.device.branch)
    if branch is None:
        return None
    try:
        record(entry, branch)
    except EntryError as exc:
        _finish(entry, "failed", branch=branch, message=str(exc))
        return str(exc), {"action": "entry_failed", "entry_id": entry.pk}
    return (f"تمام، سجلت {entry.summary} على فرع {branch.name}.",
            {"action": "entry_done", "entry_id": entry.pk, "branch": branch.name})


def _finish(entry, status, *, branch=None, message="", **links):
    entry.status = status
    entry.completed_at = timezone.now()
    if branch is not None:
        entry.branch = branch
    entry.message = message[:255]
    fields = ["status", "completed_at", "branch", "message"]
    for k, v in links.items():
        setattr(entry, k, v)
        fields.append(k)
    entry.save(update_fields=fields)


def record(entry, branch):
    """Write the held request to the books on `branch` (raises EntryError)."""
    if entry.kind == "expense":
        tx = record_expense(branch=branch, amount=Decimal(str(entry.data["amount"])),
                            description=entry.data.get("description", ""),
                            category_id=entry.data.get("category_id"))
        _finish(entry, "done", branch=branch, transaction=tx)
    elif entry.kind == "purchase":
        invoice = record_purchase(
            branch=branch, product_id=entry.data["product_id"],
            vendor_id=entry.data["vendor_id"], quantity=int(entry.data["quantity"]),
            unit_cost=Decimal(str(entry.data["unit_cost"])), cash=bool(entry.data.get("cash")))
        _finish(entry, "done", branch=branch, purchase_invoice=invoice)
    elif entry.kind == "sale":
        invoice = record_sale(
            branch=branch, product_id=entry.data["product_id"], quantity=int(entry.data["quantity"]),
            unit_price=Decimal(str(entry.data["unit_price"])),
            customer_id=entry.data.get("customer_id"), credit=bool(entry.data.get("credit")))
        _finish(entry, "done", branch=branch, sale_invoice=invoice)
    else:
        raise EntryError("النوع ده مش بيتسجل بالصوت.")


# ---------------------------------------------------------------------------
# Writing to the ERP
# ---------------------------------------------------------------------------

def record_expense(*, branch, amount: Decimal, description: str, category_id=None):
    """An operating expense paid from the branch's cash treasury."""
    from inventory.models import ExpenseCategory, FinancialTransaction
    with transaction.atomic():
        treasury = cash_treasury(branch, lock=True)
        if treasury is None:
            raise EntryError(f"فرع {branch.name} مالوش خزنة كاش، فمسجلتش المصروف.")
        if (treasury.balance or Decimal("0")) < amount:
            raise EntryError(f"رصيد خزنة فرع {branch.name} مش مكفي "
                             f"(المتاح {treasury.balance:.0f} جنيه)، فمسجلتش المصروف.")
        category = (ExpenseCategory.objects.filter(pk=category_id).first()
                    if category_id else None)
        return FinancialTransaction.objects.create(
            treasury=treasury, transaction_type="out", amount=amount,
            description=f"🤖 {description}"[:255], category=category,
        )


def record_purchase(*, branch, product_id, vendor_id, quantity: int,
                    unit_cost: Decimal, cash: bool):
    """A posted purchase invoice received at `branch`.

    Cash → paid in full from the branch's cash treasury (refused when it can't
    cover it). Otherwise it's on credit and the vendor's balance grows. Posting
    runs the ERP's own pipeline (stock in, average cost, ledger).
    """
    from inventory.models import Product, PurchaseInvoice, PurchaseInvoiceItem, Vendor
    with transaction.atomic():
        product = Product.objects.get(pk=product_id)
        vendor = Vendor.objects.get(pk=vendor_id)
        total = (unit_cost * quantity).quantize(Decimal("0.01"))
        treasury = None
        if cash:
            treasury = cash_treasury(branch, lock=True)
            if treasury is None:
                raise EntryError(f"فرع {branch.name} مالوش خزنة كاش — "
                                 "قول «آجل» لو هتتسجل على حساب المورد.")
            if (treasury.balance or Decimal("0")) < total:
                raise EntryError(f"رصيد خزنة فرع {branch.name} مش مكفي "
                                 f"(المتاح {treasury.balance:.0f} جنيه).")
        invoice = PurchaseInvoice.objects.create(vendor=vendor, branch=branch, status="draft")
        PurchaseInvoiceItem.objects.create(invoice=invoice, product=product,
                                           quantity=quantity, cost_price=unit_cost)
        invoice.update_total()
        if treasury is not None:
            invoice.treasury = treasury
            invoice.paid_amount = invoice.total_amount
            invoice.save(update_fields=["treasury", "paid_amount"])
        invoice.status = "posted"
        invoice.save(update_fields=["status"])
        invoice.refresh_from_db()
        return invoice


def record_sale(*, branch, product_id, quantity: int, unit_price: Decimal, customer_id, credit: bool):
    """A posted sale invoice from `branch`, through the ERP's own posting
    (stock out, customer balance, treasury).

    Cash → paid in full into the branch's cash treasury, on the registered
    customer or «عميل نقدي». Credit → only on a registered customer, never
    «عميل نقدي». Refused when the branch hasn't got the pieces.
    """
    from django.core.exceptions import ValidationError
    from django.db import IntegrityError

    from inventory.models import Customer, Inventory, Product
    from inventory.models.customers import WALK_IN_PHONE

    from . import services

    try:
        with transaction.atomic():
            product = Product.objects.get(pk=product_id)
            if customer_id:
                customer = Customer.objects.get(pk=customer_id)
            else:
                customer, _new = Customer.get_or_create_by_phone(WALK_IN_PHONE, defaults={"name": "عميل نقدي"})
            if credit and (customer_id is None or customer.is_walk_in):
                raise EntryError("الآجل ممنوع على «عميل نقدي»: قول اسم العميل المتسجل.")
            if not credit and cash_treasury(branch) is None:
                raise EntryError(f"فرع {branch.name} مالوش خزنة كاش، فمسجلتش البيع.")
            # The pieces are held while they're checked: two sales at once
            # can't both take the last one.
            held = (Inventory.objects.select_for_update()
                    .filter(product=product, branch=branch).values_list("quantity", flat=True).first())
            if quantity > (held or 0):
                raise EntryError(f"المتاح في فرع {branch.name} {held or 0} بس من {product.name}، "
                                 "فمسجلتش البيع.")
            return services.create_robot_sale_lines(
                lines=[(product, quantity, unit_price)], branch=branch, customer=customer,
                payment="credit" if credit else "cash", post=True, notes="🤖 بيع بالصوت من الروبوت")
    except (ValueError, ValidationError, IntegrityError) as exc:
        detail = getattr(exc, "messages", None) or [str(exc)]
        raise EntryError(f"مقدرتش أسجل البيع: {detail[0]}"[:250]) from exc


def log_sale(*, device, branch, invoice, employee=None, summary=""):
    """Record a /sale/ invoice as a robot entry (for the dashboard)."""
    from .models import RobotEntry
    return RobotEntry.objects.create(
        device=device, kind="sale", status="done", branch=branch, employee=employee,
        sale_invoice=invoice, amount=invoice.total_amount or 0,
        summary=summary[:255], completed_at=timezone.now(),
    )


# ---------------------------------------------------------------------------
# Voice requests
# ---------------------------------------------------------------------------

def voice_request(text: str, device, employee):
    """Start an expense/purchase from speech. (reply, payload) or None.

    Checks permission and validates everything that doesn't depend on the
    branch, so the branch answer only has to pick where it goes.
    """
    from . import permissions, services

    exp = parse_expense(text)
    if exp is not None:
        if not permissions.employee_can(employee, "expense"):
            return permissions.denial_message("expense"), {"action": "denied"}
        if exp["amount"] is None:
            return ("قول المصروف بالمبلغ، مثلاً: «سجل مصروف 200 جنيه بنزين».",
                    {"action": "entry_needs_amount"})
        category, salaries = expense_category(exp["description"])
        if salaries:
            return ("المرتبات بتتسجل من شاشة المصروفات عشان محتاجة اسم الموظف.",
                    {"action": "entry_salaries"})
        summary = f"مصروف {exp['amount']:.0f} جنيه ({exp['description']})"
        reply = ask(device, kind="expense", summary=summary, amount=exp["amount"],
                    employee=employee,
                    data={"amount": str(exp["amount"]), "description": exp["description"],
                          "category_id": getattr(category, "pk", None)})
        return reply, {"action": "entry_ask_branch", "kind": "expense"}

    pur = parse_purchase(text)
    if pur is not None:
        if not permissions.employee_can(employee, "purchase"):
            return permissions.denial_message("purchase"), {"action": "denied"}
        if not pur["query"]:
            return ("قول الصنف، مثلاً: «اشترينا 3 فلتر زيت من الأمل بسعر 150».",
                    {"action": "entry_incomplete"})
        product = services.find_product(pur["query"])
        if product is None:
            return (f"مش لاقي «{pur['query']}» في الأصناف — قول رقم القطعة.",
                    {"action": "entry_incomplete"})
        if not pur["vendor"]:
            return "من أنهي مورد؟ قولها كاملة: «… من <اسم المورد> بسعر …».", {
                "action": "entry_incomplete"}
        vendor = find_vendor(pur["vendor"])
        if vendor is None:
            return (f"مش لاقي المورد «{pur['vendor']}» — ضيفه من شاشة الموردين الأول.",
                    {"action": "entry_incomplete"})
        if not pur["unit_cost"]:
            return "بكام؟ قولها كاملة: «… بسعر 150».", {"action": "entry_incomplete"}
        total = pur["unit_cost"] * pur["quantity"]
        pay = "كاش" if pur["cash"] else "آجل على المورد"
        summary = (f"شراء {pur['quantity']} {product.name} من {vendor.name} "
                   f"بإجمالي {total:.0f} جنيه ({pay})")
        reply = ask(device, kind="purchase", summary=summary, amount=total,
                    employee=employee,
                    data={"product_id": product.pk, "vendor_id": vendor.pk,
                          "quantity": pur["quantity"], "unit_cost": str(pur["unit_cost"]),
                          "cash": pur["cash"]})
        return reply, {"action": "entry_ask_branch", "kind": "purchase"}

    sale = parse_sale(text)
    if sale is not None:
        return _voice_sale(sale, device, employee)
    return None


def _voice_sale(sale: dict, device, employee):
    """«بعت 2 فلتر زيت كاش» → the summary and «أسجله على أنهي فرع؟»."""
    from . import permissions, services

    if not permissions.employee_can(employee, "sale"):
        return permissions.denial_message("sale"), {"action": "denied"}
    if not sale["query"]:
        return "قول الصنف، مثلاً: «بعت 2 فلتر زيت كاش».", {"action": "entry_incomplete"}
    if sale["same_name"]:
        return (f"فيه أكتر من عميل اسمه {sale['same_name']}. قول اسمه كامل زي ما هو متسجل.",
                {"action": "entry_incomplete"})
    customer = sale["customer"]
    if sale["credit"] and customer is None:
        return ("البيع الآجل لازم يكون على عميل متسجل باسمه، مثلاً: «بعت 2 فلتر زيت لأحمد محمود آجل». "
                "الآجل ممنوع على عميل نقدي.", {"action": "entry_incomplete"})
    product = services.find_product(sale["query"])
    if product is None:
        product, choices = services.match_part_in_sentence(sale["query"])
        if product is None:
            if choices:
                return ("عندي أكتر من صنف بالاسم ده: " + "، ".join(p.name for p in choices[:3])
                        + ". قول رقم القطعة.", {"action": "entry_incomplete"})
            return (f"مش لاقي «{sale['query']}» في الأصناف — قول رقم القطعة.",
                    {"action": "entry_incomplete"})
    price = Decimal(str(product.retail_price or 0)).quantize(Decimal("0.01"))
    if price <= 0:
        return (f"سعر {product.name} مش متسجل، فمقدرش أبيعه بالصوت. بيعه من الكاشير.",
                {"action": "entry_incomplete"})
    if sale["price_said"] is not None and sale["price_said"] != price:
        return (f"البيع بالصوت بسعر البيع بس: {product.name} بـ {services.spoken_price(price)}. "
                "لو فيه خصم اعمله من الكاشير.", {"action": "entry_incomplete"})
    qty = sale["quantity"]
    if qty < 1:
        return "الكمية لازم تكون حتة أو أكتر.", {"action": "entry_incomplete"}
    total = price * qty
    who = f" لـ{customer.name}" if customer is not None else ""
    summary = (f"بيع {qty} {product.name}{who} بإجمالي {services.spoken_price(total)} "
               + ("آجل" if sale["credit"] else "كاش"))
    reply = ask(device, kind="sale", summary=summary, amount=total, employee=employee,
                data={"product_id": product.pk, "quantity": qty, "unit_price": str(price),
                      "customer_id": getattr(customer, "pk", None), "credit": sale["credit"]})
    return reply, {"action": "entry_ask_branch", "kind": "sale"}
