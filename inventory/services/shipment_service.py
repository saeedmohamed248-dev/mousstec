"""
📦 الشحنة المجمّعة — توزيع المصاريف المشتركة على كذا فاتورة شراء.

لما تشتري من كذا مورد وتشحنهم مع بعض (مثلاً 10 فواتير من الإمارات)، مصاريف
الشحن/الجمارك/التحميل/التأمين/السفر بتبقى على الشحنة كلها مش على فاتورة
واحدة. بدل ما المستخدم يقسمها بالآلة الحاسبة على كل فاتورة:

* بيكتب المصاريف مرة واحدة على الشحنة (PurchaseShipmentCost).
* ``apply_shipment`` بيوزّع كل بند على الفواتير **بنسبة قيمة كل فاتورة**،
  ويحطّ نصيب كل فاتورة كبند مصاريف عليها (PurchaseInvoiceExtraCost مع
  ``shipment=...``)، وبعدين يعيد اعتماد الفاتورة بنفس مسار «تعديل فاتورة
  شراء»: يعكس أثرها القديم (مخزون/متوسط تكلفة/مورد/خزنة/قيود) ويعتمدها تاني.
  جوه كل فاتورة النصيب بيتوزّع على أصنافها بالقيمة — فالنتيجة النهائية =
  توزيع بالقيمة على كل أصناف الشحنة.

إعادة الحفظ (تعديل المبالغ أو الفواتير) بتشيل الأنصبة القديمة بس — بنود
المصاريف المكتوبة على الفاتورة نفسها بتفضل زي ما هي.
"""
from decimal import Decimal, ROUND_DOWN

from django.core.exceptions import ValidationError
from django.db import transaction

CENT = Decimal('0.01')


def allocate(amount, bases):
    """يقسم ``amount`` على ``bases`` بالنسبة؛ كل نصيب مقرّب لتحت لأقرب قرش
    وآخر واحد بياخد الباقي — فالمجموع = المبلغ بالظبط ومفيش نصيب بالسالب."""
    amount = Decimal(str(amount or 0))
    bases = [Decimal(str(b or 0)) for b in bases]
    total = sum(bases, Decimal('0'))
    if not bases:
        return []
    if amount <= 0 or total <= 0:
        return [Decimal('0.00')] * len(bases)
    shares, given = [], Decimal('0')
    for i, base in enumerate(bases):
        if i == len(bases) - 1:
            share = amount - given
        else:
            share = (amount * base / total).quantize(CENT, rounding=ROUND_DOWN)
            given += share
        shares.append(share)
    return shares


def apply_shipment(shipment, invoice_ids, costs):
    """يربط الفواتير بالشحنة ويوزّع ``costs`` عليها ويعيد اعتمادها.

    ``costs``: list of dicts — kind, behavior, label, amount, treasury,
    expense_category (قيم متحقّق منها). بيرفع ValidationError برسالة عربي لو
    أي فاتورة مينفعش تتعدّل (اتباع منها/عليها مرتجع/مش معتمدة/تبع شحنة تانية)
    أو رصيد خزنة مش كفاية — وساعتها مفيش أي حاجة بتتغيّر.
    """
    from inventory.models import (
        PurchaseInvoice, PurchaseInvoiceExtraCost, PurchaseShipmentCost, Treasury,
    )
    from inventory.views_lightning import _reverse_purchase_posting

    new_ids = {int(i) for i in invoice_ids}
    with transaction.atomic():
        invoices = list(PurchaseInvoice.objects.select_for_update()
                        .filter(id__in=new_ids).select_related('vendor').order_by('id'))
        if len(invoices) != len(new_ids):
            raise ValidationError("فاتورة من اللي اخترتها مش موجودة.")
        for inv in invoices:
            if inv.branch_id != shipment.branch_id:
                raise ValidationError(f"فاتورة #{inv.id} تبع فرع تاني — كل فواتير الشحنة لازم تبقى في نفس الفرع.")
            if inv.status != 'posted' or not inv.is_applied:
                raise ValidationError(f"فاتورة #{inv.id} لسه مش معتمدة.")
            if inv.shipment_id and inv.shipment_id != shipment.pk:
                raise ValidationError(f"فاتورة #{inv.id} تبع شحنة مجمّعة تانية.")

        removed = list(PurchaseInvoice.objects.select_for_update()
                       .filter(shipment=shipment).exclude(id__in=new_ids))
        affected = invoices + removed

        # أرصدة الخزن قبل — عشان نرفض لو التوزيع الجديد هيوقّع خزنة تحت الصفر
        treasury_ids = {c['treasury'].pk for c in costs if c.get('treasury')}
        treasury_ids |= set(shipment.costs.exclude(treasury__isnull=True)
                            .values_list('treasury_id', flat=True))
        balance_before = {t.pk: t.balance or Decimal('0')
                          for t in Treasury.objects.filter(pk__in=treasury_ids)}

        # 1) اعكس كل فاتورة متأثرة + شيل أنصبتها القديمة من الشحنة
        snapshot = {}
        for inv in affected:
            snapshot[inv.pk] = (inv.treasury_id, inv.paid_amount)
            try:
                _reverse_purchase_posting(inv)
            except ValueError as exc:
                raise ValidationError(f"فاتورة #{inv.id}: {exc}")
            inv.extra_costs.filter(shipment=shipment).delete()

        # 2) بنود الشحنة الجديدة
        shipment.costs.all().delete()
        saved_costs = [PurchaseShipmentCost.objects.create(shipment=shipment, **c) for c in costs]

        # 3) وزّع كل بند على الفواتير بالقيمة (قيمة بضاعة المورد في الفاتورة)
        bases = [inv.total_amount for inv in invoices]
        for cost in saved_costs:
            for inv, share in zip(invoices, allocate(cost.amount, bases)):
                if share <= 0:
                    continue
                PurchaseInvoiceExtraCost.objects.create(
                    invoice=inv, shipment=shipment, kind=cost.kind, behavior=cost.behavior,
                    label=(cost.label or shipment.name)[:120], amount=share,
                    treasury=cost.treasury, expense_category=cost.expense_category)

        PurchaseInvoice.objects.filter(pk__in=[i.pk for i in removed]).update(shipment=None)
        PurchaseInvoice.objects.filter(pk__in=new_ids).update(shipment=shipment)

        # 4) أعد اعتماد كل فاتورة بدفعتها الأصلية (execute_purchase عبر الـ signal)
        for inv in affected:
            inv.refresh_from_db()
            inv.treasury_id, inv.paid_amount = snapshot[inv.pk]
            inv.status = 'posted'
            inv.save()

        for t in Treasury.objects.filter(pk__in=treasury_ids):
            before = balance_before.get(t.pk, Decimal('0'))
            if (t.balance or 0) < 0 and t.balance < before:
                raise ValidationError(
                    f"رصيد خزنة «{t.name}» مش كفاية لمصاريف الشحنة (متاح: {before}).")
    return shipment
