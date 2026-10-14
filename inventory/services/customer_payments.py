"""
💵 تحصيل العملاء — دفعة على الحساب بتتوزّع على الفواتير المفتوحة، وإلغاء دفعة.

القاعدة (نفس قاعدة الموردين في vendor_payments):
    رصيد العميل = مجموع المتبقّي على فواتيره (بعد المرتجعات)
                 − التحصيل على الحساب اللي مش مربوط بفاتورة

* التحصيل من «كشف حساب العميل» بيتوزّع على الفواتير المفتوحة من الأقدم
  للأحدث (كل جزء حركة خزنة مربوطة بفاتورتها فـ«المدفوع» في الفاتورة بيزيد
  وتختفي من «عليها متبقّي» لما تتسدّد)، واللي يفضل بعد كده بيتسجّل دفعة
  مقدّمة على الحساب.
  🐛 [FIX]: التحصيل كان بيقلّل رصيد العميل بس — الفواتير تفضل «عليها متبقّي»
  في كشف الحساب وقائمة الفواتير، وزرار التحصيل على الفاتورة كان بيسمح يتحصّل
  نفس المبلغ تاني (والعميل يطلع له رصيد دائن وهمي).
* إلغاء دفعة (تحصيل غلط) بيمسح حركة الخزنة (الرصيد والقيد بيترجعوا عبر
  الـ signals) ويرجّع المبلغ على العميل وعلى فاتورته لو مربوطة.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q, Sum

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


def _q(v):
    d = Decimal(str(v or 0))
    if not d.is_finite():
        raise ValidationError('المبلغ لازم يبقى رقم صحيح.')
    return d.quantize(CENT)


def open_invoices(customer):
    """فواتير العميل اللي عليها متبقّي فعلي (بعد المرتجعات)، من الأقدم."""
    from inventory.models import SaleInvoice
    qs = (SaleInvoice.objects
          .filter(customer=customer, is_return=False, is_applied=True,
                  maintenance_contract__isnull=True)
          .exclude(status='quotation')
          .prefetch_related('return_invoices')
          .order_by('date_created', 'id'))
    return [inv for inv in qs if inv.open_due > ZERO]


def _resync_paid(invoice_id):
    """المدفوع = Σ دفعات (in) − Σ تسويات (out) المربوطة بالفاتورة."""
    from inventory.models import FinancialTransaction, SaleInvoice
    agg = FinancialTransaction.objects.filter(sale_invoice_id=invoice_id).aggregate(
        ins=Sum('amount', filter=Q(transaction_type='in')),
        outs=Sum('amount', filter=Q(transaction_type='out')))
    net = (agg['ins'] or ZERO) - (agg['outs'] or ZERO)
    last_in = (FinancialTransaction.objects
               .filter(sale_invoice_id=invoice_id, transaction_type='in')
               .order_by('-id').values_list('treasury_id', flat=True).first())
    SaleInvoice.objects.filter(pk=invoice_id).update(
        paid_amount=net if net > 0 else ZERO, treasury_id=last_in)


@transaction.atomic
def collect(customer, amount, treasury, note=''):
    """تحصيل من العميل: بيسدّد فواتيره المفتوحة من الأقدم، والباقي مقدّم.

    بيرجّع ([(فاتورة, المبلغ)…], المبلغ اللي اتسجّل على الحساب).
    بيرفض أي مبلغ أكبر من مديونية العميل (حماية من صفر زيادة).
    """
    from inventory.models import Customer, FinancialTransaction, SaleInvoice

    amount = _q(amount)
    if amount <= 0:
        raise ValidationError('اكتب مبلغ أكبر من صفر.')
    # 🔒 صف العميل بيسلسل التحصيلات على نفس العميل، وصفوف فواتيره المفتوحة
    #    بتتقفل قبل ما نقرا المتبقّي (تحصيل على الفاتورة في نفس اللحظة).
    locked = Customer.objects.select_for_update().get(pk=customer.pk)
    list(SaleInvoice.objects.select_for_update()
         .filter(customer=locked, is_return=False, is_applied=True).values_list('pk', flat=True))
    if amount > _q(locked.balance):
        raise ValidationError(f'المبلغ أكبر من مديونية العميل ({_q(locked.balance)}).')
    if not treasury.is_active:
        raise ValidationError('الخزنة دي متوقفة.')

    base = f"تحصيل من العميل {locked.name}" + (f" — {note}" if note else '')
    left, allocated = amount, []
    for inv in open_invoices(locked):
        if left <= 0:
            break
        part = min(_q(inv.open_due), left)
        if part <= 0:
            continue
        FinancialTransaction.objects.create(
            treasury=treasury, transaction_type='in', amount=part,
            description=f"{base} (فاتورة #{inv.pk})"[:255],
            sale_invoice=inv, customer=locked)
        fields = {'paid_amount': F('paid_amount') + part}
        if not inv.treasury_id:
            fields['treasury'] = treasury
        SaleInvoice.objects.filter(pk=inv.pk).update(**fields)
        allocated.append((inv, part))
        left -= part
    if left > 0:
        FinancialTransaction.objects.create(
            treasury=treasury, transaction_type='in', amount=left,
            description=f"{base} (على الحساب)"[:255], customer=locked)
    Customer.objects.filter(pk=locked.pk).update(balance=F('balance') - amount)
    return allocated, left


def can_void(ft):
    """(مسموح؟, السبب) — إلغاء تحصيل عميل (in) مربوط بعميل حقيقي."""
    from inventory.models import Customer
    if ft.transaction_type != 'in' or not ft.customer_id:
        return False, 'الإلغاء هنا للتحصيل من العملاء بس.'
    if ft.sale_invoice_id and ft.sale_invoice.is_return:
        return False, 'دي حركة مرتجع — بتتظبط من المرتجع نفسه.'
    walk_in = Customer.normalize_phone('0000000000')
    if ft.customer.phone == walk_in:
        return False, 'دفعة «عميل نقدي» — اتصرّف فيها من تعديل الفاتورة.'
    return True, ''


@transaction.atomic
def void_collection(ft):
    """يلغي تحصيل من العميل: الفلوس بتخرج من الخزنة، والمبلغ يرجع على العميل
    (وعلى فاتورته لو الدفعة مربوطة بفاتورة). بيرجّع المبلغ."""
    from inventory.models import Customer, FinancialTransaction, Treasury

    ft = (FinancialTransaction.objects.select_for_update(of=('self',))
          .select_related('customer', 'sale_invoice', 'treasury').get(pk=ft.pk))
    ok, why = can_void(ft)
    if not ok:
        raise ValidationError(why)
    till = Treasury.objects.select_for_update().get(pk=ft.treasury_id)
    amount = _q(ft.amount)
    if _q(till.balance) < amount:
        raise ValidationError(
            f'رصيد خزنة «{till.name}» ({_q(till.balance)}) أقل من الدفعة ({amount}) — '
            f'الفلوس اتصرفت من الخزنة.')
    invoice_id, customer_id = ft.sale_invoice_id, ft.customer_id
    ft.delete()   # الرصيد بيرجع (post_delete) والقيد بيتشال/يتعكس (pre_delete)
    if invoice_id:
        _resync_paid(invoice_id)
    Customer.objects.filter(pk=customer_id).update(balance=F('balance') + amount)
    return amount
