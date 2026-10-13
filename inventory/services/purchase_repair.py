"""
🩹 إصلاح فواتير شراء اتعدّلت ومخزونها مارجعش.

قبل الإصلاح، تعديل فاتورة شراء كان بيعكس أثرها (يشيل الكميات من المخزن
ويقلّل مستحقات المورد)، وبعدين مسح بنودها القديمة كان بيعتمدها «فاضية»
ويعلّمها معتمدة — فالاعتماد الحقيقي في الآخر بيتخطّى: الفاتورة تبان معتمدة
لكن بضاعتها مادخلتش المخزن ومستحقاتها مارجعتش للمورد.

الاكتشاف: فاتورة معتمدة اتعكست قبل كده (حركة PurchaseReverse) وصافي حركات
المخزون بتاعتها أقل من بنودها (ناقص المرتجع للمورد).

الإصلاح: يشيل أي أثر جزئي (دفعات/مصاريف/قيود) ويعيد اعتمادها بالمسار العادي
(execute_purchase): الكميات، متوسط التكلفة بالمصاريف، مستحقات المورد، الخزنة،
والقيود.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum


def _stock_gap(inv):
    """(المفروض, الموجود): قطع الفاتورة (ناقص المرتجع) مقابل صافي حركات مخزونها."""
    from inventory.models import InventoryMovement
    agg = inv.items.aggregate(q=Sum('quantity'), r=Sum('returned_quantity'))
    expected = (agg['q'] or 0) - (agg['r'] or 0)
    actual = (InventoryMovement.objects
              .filter(reference_id=inv.pk, reference_type__in=('PurchaseInvoice', 'PurchaseReverse'))
              .aggregate(s=Sum('quantity_change'))['s'] or 0)
    return expected, actual


def needs_repair(inv):
    """فاتورة معتمدة اتعكست قبل كده وبضاعتها ناقصة من المخزن؟"""
    from inventory.models import InventoryMovement
    if inv.status != 'posted' or not inv.is_applied:
        return False
    if not InventoryMovement.objects.filter(reference_type='PurchaseReverse', reference_id=inv.pk).exists():
        return False
    expected, actual = _stock_gap(inv)
    return expected > 0 and actual < expected


def broken_purchase_edits(branch=None):
    """[{invoice, expected, actual, missing}] — فواتير معتمدة بضاعتها ناقصة من المخزن."""
    from inventory.models import InventoryMovement, PurchaseInvoice
    edited = (InventoryMovement.objects.filter(reference_type='PurchaseReverse')
              .values_list('reference_id', flat=True).distinct())
    invoices = (PurchaseInvoice.objects.filter(pk__in=list(edited), status='posted', is_applied=True)
                .select_related('vendor', 'branch').order_by('id'))
    if branch is not None:
        invoices = invoices.filter(branch=branch)
    out = []
    for inv in invoices:
        expected, actual = _stock_gap(inv)
        if expected > 0 and actual < expected:
            out.append({'invoice': inv, 'expected': expected, 'actual': actual,
                        'missing': expected - actual})
    return out


@transaction.atomic
def repair_purchase(invoice):
    """يعيد اعتماد فاتورة اتعدّلت ومخزونها مارجعش. بيرجّع الفاتورة بعد الإصلاح."""
    from inventory.models import (AccountingEntry, FinancialTransaction, JournalEntry,
                                  PurchaseInvoice, Vendor)
    from inventory.services.accounting_service import AccountingService
    from inventory.services.vendor_payments import expected_balances

    inv = PurchaseInvoice.objects.select_for_update().get(pk=invoice.pk)
    if not needs_repair(inv):
        return inv   # ضغطتين «إصلاح» ورا بعض — التانية مالهاش شغل
    if inv.returned_amount or inv.items.filter(returned_quantity__gt=0).exists():
        raise ValueError("الفاتورة دي عليها مرتجع للمورد — إصلاحها لازم يتراجع يدوي مع المحاسب.")
    vendor_id = inv.vendor_id
    stored_before = Decimal(str(Vendor.objects.get(pk=vendor_id).balance or 0))
    was_fixed_by_hand = abs(stored_before - expected_balances([vendor_id]).get(vendor_id, Decimal('0'))) < Decimal('0.01')
    paid, treasury_id = inv.paid_amount, inv.treasury_id

    # أي أثر جزئي من الاعتماد المبتور: دفعات بنود المصاريف (بترجع للخزنة وبتتعمل
    # تاني مع الاعتماد) وقيود. دفعات الفاتورة نفسها (من «سداد») اتدفعت فعلاً ⇒ بتفضل.
    for ft in list(inv.payments.filter(purchase_extra_cost__isnull=False)):
        ft.delete()
    kept = inv.payments.filter(transaction_type='out').aggregate(s=Sum('amount'))['s'] or Decimal('0')
    AccountingService.unpost(JournalEntry.objects.filter(purchase_invoice=inv),
                             reason=f"إصلاح فاتورة الشراء #{inv.id} بعد تعديل مابتسجّلش")
    AccountingEntry.objects.filter(purchase_invoice=inv, journal_entry__isnull=True).delete()

    PurchaseInvoice.objects.filter(pk=inv.pk).update(
        is_applied=False, status='draft', treasury_id=treasury_id, paid_amount=paid)
    inv.refresh_from_db()
    inv.status = 'posted'
    inv.save()   # execute_purchase: المخزون + متوسط التكلفة + المورد + الخزنة + القيود
    inv.refresh_from_db()
    # الاعتماد بيعمل دفعة «المدفوع» بس لو مفيش دفعات خالص — لو فيه دفعات اتسابت
    # والمدفوع المسجّل أكتر منها، الفرق عمره ما خرج من الخزنة ⇒ نسجّله.
    missing = Decimal(str(paid or 0)) - kept
    if kept > 0 and missing > 0 and treasury_id:
        FinancialTransaction.objects.create(
            treasury_id=treasury_id, transaction_type='out', amount=missing,
            description=f"سداد فاتورة مشتريات #{inv.id} للمورد {inv.vendor.name} (إصلاح)",
            purchase_invoice=inv, vendor=inv.vendor)

    if was_fixed_by_hand:
        # الرصيد كان اتصحّح بزرار «صحّح الرصيد» قبل الإصلاح — مانزودش المستحق مرتين
        Vendor.objects.filter(pk=vendor_id).update(balance=stored_before)
    elif kept > 0:
        # الدفعات اللي اتدفعت بعد ما الفاتورة اتكسرت نزّلت رصيد المورد ساعتها، والاعتماد
        # دلوقتي بيحسبها تاني جوه «المدفوع» ⇒ نرجّع مرة من المرّتين.
        Vendor.objects.filter(pk=vendor_id).update(balance=F('balance') + kept)
    return inv
