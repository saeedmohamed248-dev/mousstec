"""
🛒 البوت يقفل البيعة — طلب العميل من الشات يتسجّل «عرض سعر» في موس تك
====================================================================
لما العميل يأكد في الشات إنه عاوز يشتري، الموديل بيحط في آخر رده سطر بالشكل ده:

    [[ORDER: <كود>×<كمية>; <كود>×<كمية> | <اسم العميل> | <موبايل>]]

هنا بنشيل السطر ده من الرد قبل ما يتبعت للعميل، ونسجّل فاتورة بيع بحالة
«عرض سعر» (quotation) — يعني المخزون **مابيتخصمش** لحد ما صاحب المحل يعتمدها
بضغطة من الداشبورد (نفس مسار أوردرات موقع FixIt).

قواعد الأمان:
  • السعر دايماً من المخزون (`retail_price`) — عمرنا ما بناخد سعر من كلام الموديل.
  • أي كود مش موجود في المخزون بيتسجّل في الملاحظات كـ «ناقص» ومابيتضافش كسطر.
  • طلب واحد مفتوح لكل عميل في اليوم: لو العميل عدّل طلبه، نفس عرض السعر بيتحدّث
    بدل ما نعمل فواتير مكررة.
  • كل الدوال هنا best-effort: أي خطأ بيتسجّل في اللوج والرد بيتبعت عادي.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger("mouss_tec_core")

# الكمية ممكن تيجي بأرقام عربي (٢) أو ×/x/*
_TAG_RE = re.compile(r"\[\[\s*ORDER\s*:(?P<body>.*?)\]\]", re.IGNORECASE | re.DOTALL)
_ITEM_RE = re.compile(r"^\s*(?P<code>[^×xX*]+?)\s*(?:[×xX*]\s*(?P<qty>[\d٠-٩]+))?\s*$")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_MAX_LINES = 20
_MAX_QTY = 50

ORDER_PROMPT_RULES = [
    "",
    "تسجيل الطلبات (مهم):",
    "- لما العميل يأكد صراحةً إنه عاوز يشتري صنف أو أكتر من الكتالوج (مش مجرد سؤال عن السعر)،"
    " اتأكد من الكمية، وخد اسمه، وخد رقم موبايله لو مش على واتساب.",
    "- بعد التأكيد بس، حط في آخر ردك سطر لوحده بالشكل ده بالظبط (العميل مش هيشوفه):",
    "  [[ORDER: كود×كمية; كود×كمية | اسم العميل | رقم الموبايل]]",
    "- الكود هو «كود» الصنف من الكتالوج بالظبط. متحطش السطر ده لأي صنف مش موجود في الكتالوج،"
    " ومتحطش أسعار فيه.",
    "- قول للعميل إن طلبه اتسجل وإن حد من الفريق هيكلمه يأكد الشحن والدفع.",
]


@dataclass
class OrderRequest:
    items: list[tuple[str, int]] = field(default_factory=list)  # [(كود, كمية)]
    name: str = ""
    phone: str = ""


def extract_order(reply: str) -> tuple[str, OrderRequest | None]:
    """يرجّع (الرد من غير سطر الطلب، الطلب أو None). مابيرميش أبداً."""
    if not reply or "[[" not in reply:
        return reply, None
    match = _TAG_RE.search(reply)
    # نشيل أي تاج حتى لو بايظ — العميل مايشوفش أكواد داخلية
    clean = _TAG_RE.sub("", reply).strip()
    if not match:
        return clean, None
    parts = [p.strip() for p in match.group("body").split("|")]
    order = OrderRequest(
        name=(parts[1] if len(parts) > 1 else "")[:120],
        phone=re.sub(r"[^\d+]", "", (parts[2] if len(parts) > 2 else "").translate(_AR_DIGITS))[:20],
    )
    for raw in re.split(r"[;؛\n]", parts[0] if parts else ""):
        m = _ITEM_RE.match(raw)
        if not m or not m.group("code").strip():
            continue
        qty = int((m.group("qty") or "1").translate(_AR_DIGITS))
        order.items.append((m.group("code").strip()[:64], max(1, min(qty, _MAX_QTY))))
        if len(order.items) >= _MAX_LINES:
            break
    return clean, (order if order.items else None)


def _customer_phone(channel: str, sender_id: str, order: OrderRequest) -> str:
    # على واتساب رقم الراسل هو الموبايل نفسه — أصدق من أي رقم كتبه الموديل
    if channel == "whatsapp" and sender_id:
        return sender_id
    return order.phone


def create_draft_order(*, channel: str, sender_id: str, sender_name: str,
                       order: OrderRequest):
    """يسجّل/يحدّث عرض سعر للعميل. لازم يتنادى جوه schema_context بتاع التينانت.

    يرجّع الفاتورة أو None لو مفيش ولا صنف موجود أو مفيش رقم عميل.
    """
    from django.db import transaction
    from django.utils import timezone

    from inventory.models import Branch, Customer, Product, SaleInvoice, SaleInvoiceItem

    phone = _customer_phone(channel, sender_id, order)
    if not phone:
        return None
    branch = Branch.objects.first()
    if not branch:
        logger.warning("omnichannel: bot order skipped — no branch configured")
        return None

    products, missing = [], []
    for code, qty in order.items:
        product = (Product.objects.filter(part_number__iexact=code).first()
                   or Product.objects.filter(part_number__iexact=code.replace(" ", "")).first())
        if product is None:
            missing.append(f"{code} ×{qty}")
        else:
            products.append((product, qty))
    if not products:
        return None

    marker = f"[Bot {channel}:{sender_id}]"
    with transaction.atomic():
        customer, _ = Customer.get_or_create_by_phone(
            phone, defaults={"name": order.name or sender_name or "عميل الشات"})
        invoice = (SaleInvoice.objects
                   .filter(notes__contains=marker, status="quotation",
                           created_at__date=timezone.localdate())
                   .order_by("-pk").first())
        if invoice is None:
            invoice = SaleInvoice.objects.create(
                invoice_type="sale", sales_channel="chatbot", status="quotation",
                customer=customer, branch=branch, notes="",
            )
        else:
            invoice.items.all().delete()  # العميل عدّل طلبه ⇒ نفس عرض السعر يتحدّث

        for product, qty in products:
            SaleInvoiceItem.objects.create(
                invoice=invoice, product=product, quantity=qty,
                unit_price=product.retail_price or Decimal("0"),
            )
        lines = [f"{marker} 🤖 طلب من {channel} — اعتمده بعد ما تكلّم العميل وتأكد الشحن والدفع."]
        if order.name:
            lines.append(f"الاسم في الشات: {order.name}")
        if missing:
            lines.append("⚠️ أكواد مش موجودة في المخزون: " + " | ".join(missing))
        invoice.notes = "\n".join(lines)
        invoice.save(update_fields=["notes"])
        invoice.update_total()
    return invoice
