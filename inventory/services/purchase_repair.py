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
from django.db.models import Sum


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
        agg = inv.items.aggregate(q=Sum('quantity'), r=Sum('returned_quantity'))
        expected = (agg['q'] or 0) - (agg['r'] or 0)
        actual = (InventoryMovement.objects
                  .filter(reference_id=inv.pk, reference_type__in=('PurchaseInvoice', 'PurchaseReverse'))
                  .aggregate(s=Sum('quantity_change'))['s'] or 0)
        if expected > 0 and actual < expected:
            out.append({'invoice': inv, 'expected': expected, 'actual': actual,
                        'missing': expected - actual})
    return out


@transaction.atomic
def repair_purchase(invoice):
    """يعيد اعتماد فاتورة اتعدّلت ومخزونها مارجعش. بيرجّع الفاتورة بعد الإصلاح."""
    from inventory.models import AccountingEntry, JournalEntry, PurchaseInvoice, Vendor
    from inventory.services.accounting_service import AccountingService
    from inventory.services.vendor_payments import expected_balances
    from inventory.views_lightning import _purge_invoice_payments

    inv = PurchaseInvoice.objects.select_for_update().get(pk=invoice.pk)
    vendor_id = inv.vendor_id
    stored_before = Decimal(str(Vendor.objects.get(pk=vendor_id).balance or 0))
    was_fixed_by_hand = abs(stored_before - expected_balances([vendor_id]).get(vendor_id, Decimal('0'))) < Decimal('0.01')
    paid, treasury_id = inv.paid_amount, inv.treasury_id

    # أي أثر جزئي من الاعتماد المبتور: دفعات/مصاريف من خزن (بترجع للخزنة) وقيود
    _purge_invoice_payments(inv)
    AccountingService.unpost(JournalEntry.objects.filter(purchase_invoice=inv),
                             reason=f"إصلاح فاتورة الشراء #{inv.id} بعد تعديل مابتسجّلش")
    AccountingEntry.objects.filter(purchase_invoice=inv, journal_entry__isnull=True).delete()

    PurchaseInvoice.objects.filter(pk=inv.pk).update(
        is_applied=False, status='draft', treasury_id=treasury_id, paid_amount=paid)
    inv.refresh_from_db()
    inv.status = 'posted'
    inv.save()   # execute_purchase: المخزون + متوسط التكلفة + المورد + الخزنة + القيود
    inv.refresh_from_db()

    if was_fixed_by_hand:
        # الرصيد كان اتصحّح بزرار «صحّح الرصيد» قبل الإصلاح — مانزودش المستحق مرتين
        Vendor.objects.filter(pk=vendor_id).update(balance=stored_before)
    return inv
