"""
Build a compact, LLM-ready snapshot of the tenant's priced catalogue.

IMPORTANT: every function here assumes the caller has already switched into the
tenant's schema (via django_tenants.utils.schema_context). Product / stock /
service tables live in the tenant schema, so calling these from the public
schema would read the wrong (or empty) tables.

The snapshot is deliberately small and text-only — we feed it to the LLM as
grounding context, so it must stay well under the model's context budget. We
prioritise items whose name/part-number matches the customer's question, then
back-fill with best-selling / in-stock items.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("mouss_tec_core")

_MAX_ITEMS = 25
_MIN_TOKEN_LEN = 3


def _keywords(text: str) -> list[str]:
    # Arabic letters/digits only — the old range ؀-ۿ also swallowed Arabic
    # punctuation (؟ ، ؛), so "F30؟" never matched a product.
    tokens = re.findall(r"[\w\u0621-\u064A\u0660-\u0669]+", (text or "").lower())
    return [t for t in tokens if len(t) >= _MIN_TOKEN_LEN][:8]


def build_catalog_context(query_text: str, *, currency: str = "") -> str:
    """Return a plain-text catalogue excerpt relevant to `query_text`.

    Best-effort and never raises: any per-source failure is logged and skipped,
    so a missing table or app can't break the auto-reply pipeline.
    """
    blocks: list[str] = []

    parts = _automotive_products(query_text, currency)
    if parts:
        blocks.append("قطع الغيار في الكتالوج (Parts catalogue — check the stock note on each line):\n" + parts)

    services = _service_catalog(query_text, currency)
    if services:
        blocks.append("الخدمات والمصنعيات (Services):\n" + services)

    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _fmt_price(value, currency: str) -> str:
    try:
        amount = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        amount = str(value)
    return f"{amount} {currency}".strip()


def _automotive_products(query_text: str, currency: str) -> str:
    try:
        from django.db.models import Q, Sum
        from inventory.models.catalog import Product
    except Exception:  # app/table not present in this tenant
        return ""

    try:
        qs = Product.objects.filter(is_active=True)

        keywords = _keywords(query_text)
        if keywords:
            q = Q()
            for kw in keywords:
                q |= (
                    Q(name__icontains=kw)
                    | Q(part_number__icontains=kw)
                    | Q(car_model__icontains=kw)
                    | Q(brand__icontains=kw)
                )
            matched = list(qs.filter(q)[: _MAX_ITEMS])
        else:
            matched = []

        if len(matched) < _MAX_ITEMS:
            fill = qs.exclude(pk__in=[p.pk for p in matched])[: _MAX_ITEMS - len(matched)]
            matched.extend(fill)

        lines = []
        for p in matched[:_MAX_ITEMS]:
            try:
                qty = p.total_inventory_qty
            except Exception:
                qty = None
            price = _fmt_price(p.retail_price, currency)
            # 🐛 [FIX]: الصنف اللي رصيده صفر كان بيطلع «متوفر: 0» تحت عنوان
            #    «القطع المتوفرة» — والبوت ممكن يقول للعميل إنه موجود.
            if qty is None:
                stock = ""
            elif qty > 0:
                stock = f"متوفر: {qty}"
            else:
                stock = "غير متوفر حالياً (نفد)"
            lines.append(
                f"- {p.name} (كود {p.part_number}"
                + (f" | {p.car_model}" if p.car_model else "")
                + f") — السعر {price}"
                + (f" — {stock}" if stock else "")
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("omnichannel: automotive catalog lookup failed: %s", exc)
        return ""


def _service_catalog(query_text: str, currency: str) -> str:
    try:
        from inventory.models.catalog import ServiceCatalog
    except Exception:
        return ""
    try:
        services = list(ServiceCatalog.objects.all()[:_MAX_ITEMS])
        lines = [
            f"- {s.name} — {_fmt_price(s.labor_price, currency)}"
            for s in services
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("omnichannel: service catalog lookup failed: %s", exc)
        return ""


_CHAT_STOPWORDS = {
    'السلام', 'عليكم', 'عندكم', 'عندك', 'عندنا', 'عايز', 'عاوز', 'محتاج', 'بكام', 'سعر', 'كام',
    'موجود', 'متوفر', 'متاح', 'لو', 'سمحت', 'ممكن', 'هل', 'فيه', 'اهلا', 'مرحبا', 'صباح', 'مساء',
    'الخير', 'النور', 'شكرا', 'يا', 'باشا', 'حضرتك', 'the', 'price', 'have', 'you', 'for',
}


def quick_catalog_reply(query_text: str, *, currency: str = "") -> str:
    """
    A plain answer for "do you have X / how much is X" when the AI can't reply.

    Only parts that match the customer's words AND are in stock — never the
    filler list, never a guess. Returns "" when there's nothing solid to say
    (the caller then sends the human-handoff message).
    """
    try:
        from django.db.models import Q
        from inventory.models.catalog import Product
    except Exception:
        return ""
    keywords = [k for k in _keywords(query_text) if k not in _CHAT_STOPWORDS]
    if not keywords:
        return ""
    try:
        q = Q()
        for kw in keywords:
            q |= (Q(name__icontains=kw) | Q(part_number__icontains=kw)
                  | Q(car_model__icontains=kw) | Q(brand__icontains=kw))
        scored = []
        for p in Product.objects.filter(is_active=True).filter(q)[:40]:
            name = (p.name or '').lower()
            score = sum(3 if kw in name else
                        1 if kw in (p.car_model or '').lower() or kw in (p.brand or '').lower() else 0
                        for kw in keywords)
            if score <= 0:          # matched only through the part-number prefix
                continue
            try:
                qty = p.total_inventory_qty
            except Exception:
                qty = 0
            if qty and qty > 0:
                scored.append((score, p))
        if not scored:
            return ""
        best = max(sc for sc, _p in scored)
        hits = [p for sc, p in sorted(scored, key=lambda t: -t[0]) if sc == best][:3]
    except Exception as exc:
        logger.warning("omnichannel: quick catalog reply failed: %s", exc)
        return ""
    if not hits:
        return ""
    lines = ["أيوه متوفر عندنا 👌"]
    for p in hits:
        lines.append(f"• {p.name}" + (f" ({p.car_model})" if p.car_model else "")
                     + f" — السعر {_fmt_price(p.retail_price, currency)}")
    lines.append("هيتواصل معاك حد من الفريق حالاً لتأكيد الطلب 🙏")
    return "\n".join(lines)
