"""
🚢 توزيع مصاريف الوصول («على التكلفة»: جمارك/شحن/تحميل/تأمين) على بنود فاتورة الشراء.

* ``value``  — بالقيمة: كل بند بياخد على قد سعر شرائه ⇒ نفس النسبة على كل الأصناف.
* ``margin`` — حسب الربح: كل بند بياخد على قد ربحه (سعر البيع − سعر الشراء) × الكمية.
  الصنف اللي بيكسّب 1000 في القطعة بيشيل ضعف اللي بيكسّب 500، والصنف اللي
  مابيكسّبش (البيع ≤ الشراء) مابيشيلش مصاريف. لو مفيش ولا بند ليه ربح
  (أسعار البيع مش متسجّلة) بيرجع للتوزيع بالقيمة.

النصيب مقرّب لأقرب قرش وآخر بند ليه وزن بياخد الباقي — فالمجموع = المصاريف
بالظبط (ونفس نتيجة التوزيع بالقيمة القديم حرفياً).
"""
from decimal import Decimal

VALUE, MARGIN = 'value', 'margin'
CENT = Decimal('0.01')


def _d(v):
    return Decimal(str(v or 0))


def line_weights(lines, method):
    """``lines``: [(qty, cost, sell)] → (أوزان البنود, الطريقة اللي اتطبّقت فعلاً)."""
    if method == MARGIN:
        weights = [_d(q) * max(_d(sell) - _d(cost), Decimal('0')) for q, cost, sell in lines]
        if sum(weights, Decimal('0')) > 0:
            return weights, MARGIN
    return [_d(q) * _d(cost) for q, cost, _sell in lines], VALUE


def manual_problem(lines, extra_total, fixed):
    """مشكلة في المصاريف اليدوي (``fixed``: {رقم البند: مصاريف القطعة}) → رسالة، أو None."""
    fixed = {i: v for i, v in (fixed or {}).items() if v is not None}
    if not fixed:
        return None
    extra_total = _d(extra_total)
    if any(_d(v) < 0 for v in fixed.values()):
        return "مصاريف القطعة مينفعش تبقى بالسالب."
    fixed_total = sum((_d(lines[i][0]) * _d(v)).quantize(CENT) for i, v in fixed.items())
    if fixed_total > extra_total:
        return (f"المصاريف اللي كتبتها يدوي ({fixed_total:,.2f}) أكتر من إجمالي المصاريف "
                f"على التكلفة ({extra_total:,.2f}).")
    if len(fixed) == len(lines) and fixed_total != extra_total:
        return (f"كل البنود يدوي ومجموعها {fixed_total:,.2f} — لازم يساوي إجمالي المصاريف "
                f"{extra_total:,.2f} (أو سيب بند واحد على الأقل تلقائي ياخد الباقي).")
    return None


def allocate(lines, extra_total, method=VALUE, fixed=None):
    """نصيب كل بند من ``extra_total`` → (الأنصبة, الطريقة اللي اتطبّقت).

    ``fixed``: {رقم البند: مصاريف القطعة يدوي} — البنود دي بتاخد مصاريفها بالظبط
    (× الكمية) والباقي بيتوزّع على باقي البنود بالطريقة. لو اليدوي مش متّسق
    (أكتر من الإجمالي…) بيتجاهَل ويتوزّع الكل تلقائي.
    """
    extra_total = _d(extra_total)
    fixed = {i: _d(v) for i, v in (fixed or {}).items() if v is not None}
    if manual_problem(lines, extra_total, fixed):
        fixed = {}
    shares = [Decimal('0.00')] * len(lines)
    for i, per in fixed.items():
        shares[i] = (_d(lines[i][0]) * per).quantize(CENT)
    rest = [i for i in range(len(lines)) if i not in fixed]
    remainder = extra_total - sum(shares, Decimal('0'))
    weights, used = line_weights([lines[i] for i in rest], method)
    total_w = sum(weights, Decimal('0'))
    if remainder <= 0 or total_w <= 0:
        return shares, used
    last = max(k for k, w in enumerate(weights) if w > 0)
    given = Decimal('0.00')
    for k, w in enumerate(weights):
        if k != last:
            shares[rest[k]] = (remainder * w / total_w).quantize(CENT)
            given += shares[rest[k]]
    shares[rest[last]] = remainder - given
    return shares, used


def landed_units(lines, extra_total, method=VALUE, fixed=None):
    """تكلفة الوصول للقطعة في كل بند = سعر الشراء + نصيبها ÷ الكمية → (القيم, الطريقة)."""
    shares, used = allocate(lines, extra_total, method, fixed)
    units = []
    for (q, cost, _sell), share in zip(lines, shares):
        qty = _d(q) or Decimal('1')
        units.append((_d(cost) + share / qty).quantize(CENT))
    return units, used


# =====================================================================
# 🔁 إعادة توزيع التكلفة على فاتورة معتمدة
# =====================================================================
# الكميات والمبالغ مابتتغيّرش — بس نصيب كل صنف من المصاريف. فبنعدّل متوسط
# تكلفة كل صنف بالفرق (نفس معادلة عكس الاعتماد) ونخزّن تكلفة الوصول الجديدة
# على البنود. إجمالي قيمة المخزون مابيتغيّرش ⇒ مفيش قيود محاسبية جديدة.

def _recost_inputs(inv, sell_overrides=None, manual=None):
    """بنود الفاتورة + أسعار البيع + المصاريف اليدوي ({رقم البند: مصاريف القطعة}).

    ``manual``: {item_pk: مصاريف القطعة أو None} — None كله = المحفوظ على البنود."""
    sell_overrides = sell_overrides or {}
    items = list(inv.items.select_related('product').order_by('id'))
    sells = [_d(sell_overrides.get(it.product_id, it.product.retail_price)) for it in items]
    if manual is None:
        manual = {it.pk: it.manual_extra_unit for it in items}
    fixed = {i: _d(manual[it.pk]) for i, it in enumerate(items) if manual.get(it.pk) is not None}
    lines = [(it.quantity, it.cost_price, s) for it, s in zip(items, sells)]
    return items, sells, lines, fixed


def recost_problem(inv, sell_overrides=None, manual=None):
    """مشكلة في المصاريف اليدوي على الفاتورة دي → رسالة، أو None."""
    _items, _sells, lines, fixed = _recost_inputs(inv, sell_overrides, manual)
    return manual_problem(lines, inv.landed_costs_total, fixed)


def recost_rows(inv, method, sell_overrides=None, manual=None):
    """صفوف العرض: لكل بند السعر الحالي والجديد وربح القطعة قبل/بعد → (الصفوف, الطريقة)."""
    items, sells, lines, fixed = _recost_inputs(inv, sell_overrides, manual)
    units, used = landed_units(lines, inv.landed_costs_total, method, fixed)
    rows = []
    for i, (it, sell, unit) in enumerate(zip(items, sells, units)):
        old = _d(it.effective_unit_cost)
        rows.append({'item': it, 'product': it.product, 'qty': it.quantity, 'cost': _d(it.cost_price),
                     'sell': sell, 'old_unit': old, 'new_unit': unit,
                     'manual': fixed.get(i), 'extra_unit': unit - _d(it.cost_price),
                     'old_profit': sell - old, 'new_profit': sell - unit})
    return rows, used


def recost_blockers(inv):
    """أسباب تمنع إعادة التوزيع (فاضية = ينفع)."""
    from django.db.models import Sum
    from inventory.models import Inventory
    if inv.status != 'posted' or not inv.is_applied:
        return ["الفاتورة مش معتمدة — التوزيع بيتحسب لما تتعتمد."]
    if inv.returned_amount or inv.items.filter(returned_quantity__gt=0).exists():
        return ["الفاتورة عليها مرتجع للمورد — مينفعش نغيّر توزيع تكلفتها."]
    need = {}
    for it in inv.items.select_related('product'):
        need.setdefault(it.product_id, [it.product.name, 0])[1] += it.quantity
    sold = []
    for pid, (name, q) in need.items():
        have = (Inventory.objects.filter(product_id=pid, branch=inv.branch)
                .aggregate(s=Sum('quantity'))['s'] or 0)
        if have < q:
            sold.append(f"«{name}» (موجود {have} من {q})")
    if sold:
        return ["اتباع من أصناف الفاتورة دي، فتكلفتها دخلت في المبيعات خلاص: " + "، ".join(sold[:6])
                + ("…" if len(sold) > 6 else "")]
    return []


def recost_purchase(invoice, method, sell_prices=None, manual=None):
    """يعيد توزيع مصاريف الوصول على أصناف فاتورة معتمدة.

    ``sell_prices``: {product_id: سعر بيع جديد} — بيتحفظ على الصنف قبل الحساب.
    ``manual``: {item_pk: مصاريف القطعة يدوي أو None (تلقائي)} — None كله = سيب
    المحفوظ. بيرفع ValueError برسالة عربي لو الفاتورة مينفعش (مش معتمدة/مرتجع/
    اتباع منها) أو المصاريف اليدوي مش متّسقة.
    """
    from collections import defaultdict
    from django.db import transaction
    from inventory.models import Inventory, Product, PurchaseInvoice, PurchaseInvoiceItem

    method = MARGIN if method == MARGIN else VALUE
    with transaction.atomic():
        inv = PurchaseInvoice.objects.select_for_update().get(pk=invoice.pk)
        # اقفل صفوف المخزون قبل الفحص عشان بيع في نفس اللحظة مايعدّيش
        list(Inventory.objects.select_for_update()
             .filter(branch=inv.branch, product_id__in=inv.items.values('product_id')))
        blockers = recost_blockers(inv)
        if blockers:
            raise ValueError(blockers[0])
        # أسعار البيع لأصناف الفاتورة دي بس
        for prod in Product.objects.filter(pk__in=set(inv.items.values_list('product_id', flat=True))):
            price = _d((sell_prices or {}).get(prod.pk))
            if price > 0 and price != _d(prod.retail_price):
                prod.retail_price = price
                prod.save(update_fields=['retail_price'])
        problem = recost_problem(inv, manual=manual)
        if problem:
            raise ValueError(problem)
        rows, used = recost_rows(inv, method, manual=manual)
        delta = defaultdict(Decimal)
        for r in rows:
            delta[r['product'].pk] += _d(r['qty']) * (r['new_unit'] - r['old_unit'])
        for pid, d in delta.items():
            if d == 0:
                continue
            prod = Product.objects.select_for_update().get(pk=pid)
            on_hand = _d(prod.total_inventory_qty)
            if on_hand > 0:
                prod.average_cost = max((_d(prod.average_cost) * on_hand + d) / on_hand, Decimal('0'))
                prod.save(update_fields=['average_cost'])
        for r in rows:
            PurchaseInvoiceItem.objects.filter(pk=r['item'].pk).update(
                landed_unit_cost=r['new_unit'], manual_extra_unit=r['manual'])
        PurchaseInvoice.objects.filter(pk=inv.pk).update(cost_allocation=method)
    return {'rows': rows, 'used': used}
