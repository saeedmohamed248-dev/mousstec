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
    d = Decimal(str(v or 0))
    if not d.is_finite():
        raise ValidationError('المبلغ لازم يبقى رقم صحيح.')
    return d.quantize(CENT)


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


# ---------------------------------------------------------------------------
# 🔁 خصم رصيد المورد الدائن من فواتيره المفتوحة
# ---------------------------------------------------------------------------
CREDIT_TAG = "[من الرصيد الدائن]"


def _simple_journals(ft):
    """قيود حركة الخزنة لو كلها «سطرين بنفس المبلغ» (سداد مورد عادي) — وإلا None."""
    from inventory.models import AccountingEntry, JournalEntry
    jes = list(JournalEntry.objects.filter(financial_transaction=ft))
    if AccountingEntry.objects.filter(financial_transaction=ft, journal_entry__isnull=True).exists():
        return None
    amount = _q(ft.amount)
    for je in jes:
        if je.status != 'posted':
            return None
        for ln in je.lines.all():
            if _q(ln.debit + ln.credit) != amount:
                return None
    return jes


def _move_payment(ft, part, src_inv, dst_inv):
    """ينقل ``part`` من دفعة ``ft`` (على الحساب أو على فاتورة ``src_inv``) لفاتورة
    ``dst_inv`` — من غير ما فلوس تتحرك: الخزنة والدفتر ورصيد المورد زي ما هم.
    لو جزء من الدفعة بس، بتتقسم لحركتين (وقيدها كمان) بنفس التاريخ والخزنة.
    بيرجّع True لو اتنقلت."""
    from inventory.models import FinancialTransaction, JournalEntry, PurchaseInvoice
    part = _q(part)
    tag = lambda d: (d if d.startswith(CREDIT_TAG) else f"{CREDIT_TAG} {d}")[:255]  # noqa: E731
    desc = f"{ft.description} ← اتخصمت من فاتورة #{dst_inv.pk}"
    if part >= _q(ft.amount):
        FinancialTransaction.objects.filter(pk=ft.pk).update(purchase_invoice=dst_inv, description=tag(desc))
    else:
        jes = _simple_journals(ft)
        if jes is None:
            return False
        new = FinancialTransaction.objects.get(pk=ft.pk)
        new.pk = new.id = None
        new.amount = part
        new.purchase_invoice = dst_inv
        new.description = tag(desc)
        # bulk_create: من غير signals — الفلوس اتحسبت على الخزنة والدفتر مرة خلاص
        new = FinancialTransaction.objects.bulk_create([new])[0]
        FinancialTransaction.objects.filter(pk=ft.pk).update(amount=F('amount') - part)
        for je in jes:
            lines = list(je.lines.all())
            clone = JournalEntry.objects.get(pk=je.pk)
            clone.pk = clone.id = None
            clone.financial_transaction = new
            clone.save()
            clone.number = f"JE-{clone.date:%Y%m}-{clone.pk:06d}"
            clone.save(update_fields=['number'])
            for ln in lines:
                debit, credit = (part, ZERO) if ln.debit > 0 else (ZERO, part)
                ln_new = type(ln).objects.get(pk=ln.pk)
                ln_new.pk = ln_new.id = None
                ln_new.journal_entry, ln_new.financial_transaction = clone, new
                ln_new.reference, ln_new.debit, ln_new.credit = clone.number, debit, credit
                ln_new.save()
                type(ln).objects.filter(pk=ln.pk).update(debit=F('debit') - debit, credit=F('credit') - credit)
    if src_inv is not None:
        PurchaseInvoice.objects.filter(pk=src_inv.pk).update(paid_amount=F('paid_amount') - part)
    PurchaseInvoice.objects.filter(pk=dst_inv.pk).update(paid_amount=F('paid_amount') + part)
    return True


@transaction.atomic
def apply_vendor_credit(vendor):
    """🔁 رصيد المورد الدائن (دفعة مقدّمة، أو فلوس زيادة على فاتورة بعد مرتجع) بيتخصم
    من فواتيره المفتوحة من الأقدم — فالفاتورة تبان مسدّدة على قد اللي اتدفع فعلاً.

    🐛 [FIX]: الرصيد الدائن كان بيقلّل رصيد المورد بس — فواتيره تفضل «عليها
    متبقّي» والمورد رصيده صفر أو بالسالب، و«سداد» عليها كان بيدفع له تاني.
    مفيش فلوس بتتحرك: الدفعة نفسها بتتربط بالفاتورة. بيرجّع [(فاتورة, مبلغ)].
    """
    from inventory.models import FinancialTransaction, PurchaseInvoice
    invoices = list(PurchaseInvoice.objects.select_for_update()
                    .filter(vendor_id=vendor.pk, is_applied=True).order_by('date_created', 'id'))
    open_invs = [[inv, _q(inv.net_due)] for inv in invoices if _q(inv.net_due) > 0]
    if not open_invs:
        return []
    payments = (FinancialTransaction.objects.select_for_update()
                .filter(vendor_id=vendor.pk, transaction_type='out', purchase_extra_cost__isnull=True,
                        sale_invoice__isnull=True, customer__isnull=True, amount__gt=0))
    # (الدفعة، المتاح منها، فاتورتها لو مدفوعة زيادة)
    sources = [(ft, _q(ft.amount), None)
               for ft in payments.filter(purchase_invoice__isnull=True).order_by('date', 'id')]
    for inv in invoices:
        extra = -_q(inv.net_due)
        for ft in payments.filter(purchase_invoice=inv).order_by('-date', '-id'):
            if extra <= 0:
                break
            take = min(extra, _q(ft.amount))
            sources.append((ft, take, inv))
            extra -= take
    applied = []
    for ft, available, src in sources:
        for row in open_invs:
            if available <= 0:
                break
            inv, due = row
            if due <= 0 or inv.pk == getattr(src, 'pk', None):
                continue
            part = min(available, due)
            ft.refresh_from_db()
            if not _move_payment(ft, part, src, inv):
                break
            row[1] -= part
            available -= part
            applied.append((inv, part))
    return applied
