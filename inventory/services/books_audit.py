"""
🔍 فحص الدفاتر — هل الأرقام متّسقة مع بعضها؟ (قراءة بس، مابيعدّلش حاجة)

* رصيد كل صنف في كل فرع = مجموع حركاته؛
* قيمة المخزون (متوسط التكلفة × الكمية) = رصيد حساب المخزون في الدفتر؛
* رصيد كل مورد = اللي على فواتيره − دفعاته على الحساب؛
* رصيد كل خزنة = إيداعاتها − مسحوباتها (+ رصيد افتتاحي لو اتبعت)؛
* الدفتر متوازن (المدين = الدائن)؛
* مفيش فاتورة شراء متعدّلة ومخزونها مارجعش.

كل مشكلة بترجع كسطر عربي واضح؛ القايمة الفاضية = كله مظبوط.
"""
from decimal import Decimal

from django.db.models import Sum

ZERO = Decimal('0')


def books_problems(opening=None, value_tolerance=Decimal('1.00')):
    """[رسالة لكل مشكلة] — ``opening``: {treasury_id: رصيد افتتاحي من غير حركة}."""
    from inventory.models import (AccountingEntry, ChartOfAccount, FinancialTransaction, Inventory,
                                  InventoryMovement, Product, Treasury)
    from inventory.services.purchase_repair import broken_purchase_edits
    from inventory.services.vendor_payments import balance_mismatches

    opening = opening or {}
    out = []

    agg = AccountingEntry.objects.aggregate(d=Sum('debit'), c=Sum('credit'))
    if (agg['d'] or ZERO) != (agg['c'] or ZERO):
        out.append(f"الدفتر مش متوازن: مدين {agg['d']} ≠ دائن {agg['c']}")

    moves = {(p, b): q for p, b, q in InventoryMovement.objects.values_list('product_id', 'branch_id')
             .annotate(q=Sum('quantity_change')).values_list('product_id', 'branch_id', 'q')}
    for row in Inventory.objects.select_related('product', 'branch'):
        mv = moves.get((row.product_id, row.branch_id), 0) or 0
        if mv != row.quantity:
            out.append(f"رصيد «{row.product.name}» في {row.branch.name}: {row.quantity} "
                       f"بس حركاته بتقول {mv}")

    from inventory.services.inventory_service import InventoryService
    on_road = InventoryService.in_transit_quantities()   # خرجت من فرع ولسه ماوصلتش
    value = sum((Decimal(str(p.average_cost or 0)) * ((p.total_inventory_qty or 0) + on_road.get(p.pk, 0))
                 for p in Product.objects.all()), ZERO)
    # المخزون + «بضاعة في الطريق بين الفروع» (قيود التحويلات)
    led = (AccountingEntry.objects.filter(account__in=ChartOfAccount.objects.filter(code__in=('1200', '1210')))
           .aggregate(d=Sum('debit'), c=Sum('credit')))
    ledger_value = (led['d'] or ZERO) - (led['c'] or ZERO)
    if abs(value - ledger_value) > value_tolerance:
        out.append(f"قيمة المخزون {value:,.2f} ≠ حساب المخزون في الدفتر {ledger_value:,.2f} "
                   f"(فرق {value - ledger_value:,.2f}) — reconcile_inventory_ledger بيسوّيه")

    for vendor, stored, expected in balance_mismatches():
        out.append(f"رصيد المورد «{vendor.name}» {stored:,.2f} بس فواتيره ودفعاته بتقول {expected:,.2f}")

    for t in Treasury.objects.all():
        ft = FinancialTransaction.objects.filter(treasury=t)
        inn = ft.filter(transaction_type='in').aggregate(s=Sum('amount'))['s'] or ZERO
        outt = ft.filter(transaction_type='out').aggregate(s=Sum('amount'))['s'] or ZERO
        expected = Decimal(str(opening.get(t.pk, ZERO))) + inn - outt
        if Decimal(str(t.balance or 0)) != expected:
            out.append(f"رصيد خزنة «{t.name}» {t.balance} بس حركاتها بتقول {expected} "
                       f"— recompute_treasury بيسوّيه")

    for row in broken_purchase_edits():
        out.append(f"فاتورة شراء #{row['invoice'].pk} اتعدّلت وناقص من مخزونها {row['missing']} قطعة "
                   f"— «إصلاح» من صفحة المشتريات")
    return out
