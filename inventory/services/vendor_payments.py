"""
💸 سداد الموردين — دفعة على فاتورة (جزئية أو كاملة) أو دفعة على الحساب.

القاعدة اللي بتربط الرقمين:
    رصيد المورد = مجموع المتبقّي على فواتيره المعتمدة
                 − الدفعات على الحساب اللي مش مربوطة بفاتورة

* الدفعة على فاتورة بتزوّد «المدفوع» في الفاتورة نفسها وبتقلّل رصيد المورد —
  فالفاتورة بتختفي من «عليها متبقّي» لما تتسدّد.
* الدفعة على الحساب بتتوزّع على الفواتير المفتوحة من الأقدم للأحدث (كل جزء
  حركة خزنة مربوطة بفاتورتها)، واللي يفضل بعد كده بيتسجّل دفعة مقدّمة.
* كل حركة خزنة (out + vendor) بتقلّل الخزنة وبتسوّي الدائنين في الدفتر عبر
  الـ signals الموجودة.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


def _q(v):
    return Decimal(str(v or 0)).quantize(CENT)


def _take_from_treasury(treasury, amount):
    from inventory.models import Treasury
    locked = Treasury.objects.select_for_update().get(pk=treasury.pk)
    if not locked.is_active:
        raise ValidationError('الخزنة دي متوقفة.')
    if _q(locked.balance) < amount:
        raise ValidationError(f'رصيد خزنة «{locked.name}» مش كفاية (متاح: {_q(locked.balance)}).')
    return locked


def _pay(inv, amount, treasury, note, label):
    from inventory.models import FinancialTransaction, PurchaseInvoice
    desc = f"{label} فاتورة مشتريات #{inv.id} للمورد {inv.vendor.name}" + (f" — {note}" if note else '')
    FinancialTransaction.objects.create(
        treasury=treasury, transaction_type='out', amount=amount, description=desc[:255],
        purchase_invoice=inv, vendor=inv.vendor)
    fields = {'paid_amount': F('paid_amount') + amount}
    if not inv.treasury_id:
        fields['treasury'] = treasury
    PurchaseInvoice.objects.filter(pk=inv.pk).update(**fields)


@transaction.atomic
def pay_invoice(invoice, amount, treasury, note=''):
    """دفعة على فاتورة شراء معتمدة (لحد المتبقّي عليها). بيرجّع المبلغ المدفوع."""
    from inventory.models import PurchaseInvoice, Vendor
    inv = PurchaseInvoice.objects.select_for_update().select_related('vendor').get(pk=invoice.pk)
    if not inv.is_applied:
        raise ValidationError('الفاتورة لسه مسودة (ما اتعتمدتش) — افتحها واحفظها الأول.')
    amount = _q(amount)
    due = _q(inv.net_due)
    if amount <= 0:
        raise ValidationError('اكتب مبلغ أكبر من صفر.')
    if due <= 0:
        raise ValidationError('الفاتورة دي متسدّدة بالكامل.')
    if amount > due:
        raise ValidationError(f'المبلغ أكبر من المتبقّي على الفاتورة ({due}).')
    locked = _take_from_treasury(treasury, amount)
    _pay(inv, amount, locked, note, 'سداد')
    Vendor.objects.filter(pk=inv.vendor_id).update(balance=F('balance') - amount)
    return amount


@transaction.atomic
def pay_vendor(vendor, amount, treasury, note=''):
    """دفعة على حساب المورد: بتسدّد فواتيره المفتوحة من الأقدم، والباقي مقدّم.

    بيرجّع [(فاتورة, المبلغ)…] + المبلغ اللي اتسجّل مقدّم.
    """
    from inventory.models import FinancialTransaction, PurchaseInvoice, Vendor
    amount = _q(amount)
    if amount <= 0:
        raise ValidationError('اكتب مبلغ أكبر من صفر.')
    locked = _take_from_treasury(treasury, amount)
    left, allocated = amount, []
    open_invs = (PurchaseInvoice.objects.select_for_update().select_related('vendor')
                 .filter(vendor=vendor, is_applied=True).order_by('date_created', 'id'))
    for inv in open_invs:
        if left <= 0:
            break
        due = _q(inv.net_due)
        if due <= 0:
            continue
        part = min(due, left)
        _pay(inv, part, locked, note, 'سداد (على الحساب)')
        allocated.append((inv, part))
        left -= part
    if left > 0:
        desc = f"دفعة مقدّمة للمورد {vendor.name}" + (f" — {note}" if note else '')
        FinancialTransaction.objects.create(
            treasury=locked, transaction_type='out', amount=left, description=desc[:255], vendor=vendor)
    Vendor.objects.filter(pk=vendor.pk).update(balance=F('balance') - amount)
    return allocated, left


def expected_balances(vendor_ids=None):
    """{vendor_id: الرصيد حسب الفواتير والدفعات} — عشان نكشف الأرصدة اللي اتلخبطت."""
    from inventory.models import FinancialTransaction, PurchaseInvoice
    dec = DecimalField(max_digits=14, decimal_places=2)
    inv = PurchaseInvoice.objects.filter(is_applied=True)
    ft = FinancialTransaction.objects.filter(transaction_type='out', purchase_invoice__isnull=True,
                                             vendor__isnull=False)
    if vendor_ids is not None:
        inv = inv.filter(vendor_id__in=vendor_ids)
        ft = ft.filter(vendor_id__in=vendor_ids)
    out = {}
    for row in (inv.values('vendor_id')
                .annotate(d=Coalesce(Sum(F('total_amount') - F('returned_amount') - F('paid_amount'),
                                         output_field=dec), Value(ZERO, output_field=dec)))):
        out[row['vendor_id']] = _q(row['d'])
    for row in ft.values('vendor_id').annotate(s=Sum('amount')):
        out[row['vendor_id']] = out.get(row['vendor_id'], ZERO) - _q(row['s'])
    return out


def balance_mismatches():
    """[(مورد, الرصيد المسجّل, الرصيد حسب الفواتير)] للموردين اللي الرقمين فيهم مختلفين."""
    from inventory.models import Vendor
    exp = expected_balances()
    rows = []
    for v in Vendor.objects.all().only('id', 'name', 'balance'):
        e = exp.get(v.id, ZERO)
        if abs(_q(v.balance) - e) >= CENT:
            rows.append((v, _q(v.balance), e))
    return rows
