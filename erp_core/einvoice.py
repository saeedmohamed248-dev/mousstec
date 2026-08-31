"""
🧾 الفاتورة الضريبية (GCC e-invoice) — TLV QR + تفصيل ضريبة القيمة المضافة.

يُنتج رمز QR بصيغة TLV (Tag-Length-Value) المعتمدة خليجياً للفاتورة الضريبية
المبسّطة (نفس نمط ZATCA Phase-1 المستخدم في السعودية والمتوافق مع متطلبات
هيئة الضرائب الإماراتية FTA لعرض بيانات الفاتورة):

  Tag 1 = اسم البائع
  Tag 2 = الرقم الضريبي للبائع (TRN)
  Tag 3 = تاريخ/وقت الفاتورة (ISO-8601)
  Tag 4 = الإجمالي شامل الضريبة
  Tag 5 = قيمة ضريبة القيمة المضافة

الاستخدام في view الطباعة: build_tax_invoice_context(invoice, tenant) → dict
جاهز للقالب (فيه is_tax_invoice, trn, vat breakdown, qr_data_uri).
"""
import base64
from decimal import Decimal, ROUND_HALF_UP

try:
    import qrcode
    from io import BytesIO
except ImportError:  # pragma: no cover
    qrcode = None


def _tlv(tag: int, value: str) -> bytes:
    """عنصر TLV واحد: [tag][length][utf-8 value]."""
    raw = (value or "").encode("utf-8")
    return bytes([tag, len(raw)]) + raw


def build_tlv_base64(seller_name, trn, timestamp_iso, total, vat) -> str:
    """يرجّع سلسلة TLV مُرمّزة base64 لوضعها في الـ QR."""
    payload = (
        _tlv(1, str(seller_name or ""))
        + _tlv(2, str(trn or ""))
        + _tlv(3, str(timestamp_iso or ""))
        + _tlv(4, f"{Decimal(str(total or 0)):.2f}")
        + _tlv(5, f"{Decimal(str(vat or 0)):.2f}")
    )
    return base64.b64encode(payload).decode("ascii")


def qr_png_data_uri(payload: str) -> str:
    """يحوّل نص الـ payload لصورة QR كـ data-URI (PNG). فارغ لو qrcode غير متاح."""
    if qrcode is None or not payload:
        return ""
    img = qrcode.make(payload)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def vat_breakdown(total_amount, tax_percentage):
    """
    يفكّ الإجمالي (شامل الضريبة) إلى صافٍ + ضريبة، من الإجمالي والنسبة فقط.
    يستخدم القيم المخزّنة على الفاتورة — لا يعيد تجميع البنود.
    """
    rate = Decimal(str(tax_percentage or 0))
    total = Decimal(str(total_amount or 0))
    if rate <= 0:
        return {"net": total, "vat": Decimal("0.00"), "rate": rate, "total": total}
    net = (total / (1 + rate / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    vat = (total - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"net": net, "vat": vat, "rate": rate, "total": total}


def build_tax_invoice_context(invoice, tenant):
    """
    يبني سياق الفاتورة الضريبية للقالب.

    is_tax_invoice = True فقط لو البائع مسجّل ضريبياً (عنده TRN). حينها نعرض
    وسم «فاتورة ضريبية» + TRN + تفصيل الضريبة + QR. غير كده الفاتورة عادية.
    """
    trn = (getattr(tenant, "tax_registration_number", "") or "").strip()
    seller = getattr(tenant, "name", "") or ""
    bd = vat_breakdown(getattr(invoice, "total_amount", 0), getattr(invoice, "tax_percentage", 0))

    ctx = {
        "is_tax_invoice": bool(trn),
        "seller_trn": trn,
        "vat_net": bd["net"],
        "vat_amount": bd["vat"],
        "vat_rate": bd["rate"],
        "invoice_total": bd["total"],
        "tax_qr_data_uri": "",
    }
    if trn:
        ts = getattr(invoice, "date_created", None)
        ts_iso = ts.isoformat() if ts is not None else ""
        payload = build_tlv_base64(seller, trn, ts_iso, bd["total"], bd["vat"])
        ctx["tax_qr_data_uri"] = qr_png_data_uri(payload)
    return ctx
