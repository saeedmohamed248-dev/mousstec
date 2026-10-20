"""
📒 دفتر المطبعة المحاسبي — قيود يومية مزدوجة بالاستحقاق
==========================================================

كل حركة فلوس أو مخزون في المطبعة بتتقيّد في نفس دفتر الأستاذ العام بتاع
النظام (``inventory.JournalEntry`` / ``AccountingEntry`` عبر ``AccountingService``)،
فميزان المراجعة وقائمة الدخل والميزانية بيطلعوا من مصدر واحد متوازن.

الإيراد بيتعرف وقت التسليم (استحقاق) مش وقت التحصيل:

    صرف خامات على طلب       مدين أعمال تحت التشغيل   / دائن مخزون الخامات
    تسليم الطلب              مدين العملاء (المستحق)    / دائن إيرادات الطباعة + ض.ق.م
                             مدين تكلفة الخامات        / دائن مخزون الخامات
    إلغاء طلب مصروف عليه     مدين هالك خامات           / دائن مخزون الخامات
    تحصيل من عميل            مدين الخزنة               / دائن العملاء
    فاتورة شراء مستلمة       مدين مخزون الخامات + ض.ق.م مدخلات / دائن الموردين
    سداد مورد                مدين الموردين             / دائن الخزنة
    مصروف (إيجار/مرتبات…)    مدين المصروف              / دائن الخزنة
    تسوية جرد                مدين/دائن فروقات الجرد   / دائن/مدين مخزون الخامات

العميل اللي دافع أكتر من المستحق عليه (عربون على طلب لسه ماتسلّمش أو رصيد
مقدّم) بيتحوّل رصيده لحساب «دفعات مقدمة من العملاء» (التزام)، والمورد اللي
دفعناله مقدّم لـ «دفعات مقدمة للموردين» (أصل) — عشان الميزانية تبان صح.

كل مستند ليه مرجع ثابت (``PRN-ORD-12`` …). المزامنة بتحسب القيد «المفروض»
للمستند دلوقتي وتقارنه باللي في الدفتر فعلاً تحت نفس المرجع:
  * الفترة مفتوحة ← القيد القديم يتمسح ويتعمل قيد واحد صح بتاريخ المستند.
  * الفترة مقفولة ← قيد تسوية بالفرق بتاريخ النهارده (الفترة المقفولة ماتتلمسش).
فالمزامنة idempotent وبتصلّح نفسها: تشغيلها أكتر من مرة مابيكرّرش حاجة.
"""
import logging
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

ZERO = Decimal('0.00')

# key → (code, name, type). أي حساب ناقص بيتعمل أول ما يتقيّد عليه.
PRINT_ACCOUNTS = {
    'cash':              ('1001', 'النقدية والخزائن', 'asset'),
    'transfer':          ('1090', 'تحويلات بين الخزائن (حساب وسيط)', 'asset'),
    'ar':                ('1100', 'المدينون (ذمم العملاء)', 'asset'),
    'employee_advances': ('1150', 'سلف وعهد الموظفين', 'asset'),
    'materials':         ('1200', 'المخزون', 'asset'),
    'wip':               ('1220', 'أعمال تحت التشغيل (طلبات الطباعة)', 'asset'),
    'vat_input':         ('1250', 'ضريبة القيمة المضافة (مدخلات)', 'asset'),
    'supplier_advances': ('1260', 'دفعات مقدمة للموردين', 'asset'),
    'ap':                ('2100', 'الدائنون (ذمم الموردين)', 'liability'),
    'vat_output':        ('2200', 'ضريبة القيمة المضافة المستحقة', 'liability'),
    'customer_deposits': ('2400', 'دفعات مقدمة من العملاء', 'liability'),
    'capital':           ('3001', 'رأس المال', 'equity'),
    'opening_equity':    ('3005', 'رصيد افتتاحي (حقوق ملكية)', 'equity'),
    'revenue':           ('4010', 'إيرادات أعمال الطباعة والتصميم', 'revenue'),
    'other_income':      ('4099', 'إيرادات أخرى', 'revenue'),
    'cogs':              ('5001', 'تكلفة البضاعة المباعة', 'expense'),
    'materials_expense': ('5003', 'خامات ومستلزمات مستهلكة مباشرة', 'expense'),
    'other_expense':     ('5099', 'مصروفات عمومية', 'expense'),
    'salaries':          ('5100', 'رواتب وأجور', 'expense'),
    'variance':          ('5160', 'فروقات جرد المخزون (عجز/زيادة)', 'expense'),
    'spoilage':          ('5170', 'هالك خامات طلبات ملغية', 'expense'),
    'rent':              ('5201', 'إيجارات', 'expense'),
    'utilities':         ('5202', 'كهرباء ومياه ومرافق', 'expense'),
    'shipping':          ('5300', 'مصروفات شحن', 'expense'),
    'maintenance':       ('5400', 'صيانة الماكينات والمعدات', 'expense'),
    'marketing':         ('5500', 'دعاية وتسويق', 'expense'),
}

# بند حركة الخزنة ← الحساب المقابل للنقدية.
TXN_COUNTER_ACCOUNT = {
    'customer': 'ar',
    'supplier': 'ap',
    'advance': 'employee_advances',
    'capital': 'capital',
    'drawings': 'capital',
    'transfer': 'transfer',
    'other_income': 'other_income',
    'materials': 'materials_expense',
    'salaries': 'salaries',
    'rent': 'rent',
    'utilities': 'utilities',
    'maintenance': 'maintenance',
    'shipping': 'shipping',
    'marketing': 'marketing',
    'other_expense': 'other_expense',
}


def _q(value):
    from inventory.services.accounting_service import _q as q
    return q(value)


def account(key):
    from inventory.services.accounting_service import AccountingService
    code, name, acc_type = PRINT_ACCOUNTS[key]
    return AccountingService.account(code, name, acc_type)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------
def ref_order(pk):
    return f'PRN-ORD-{pk}'


def ref_txn(pk):
    return f'PRN-TXN-{pk}'


def ref_purchase(pk):
    return f'PRN-PUR-{pk}'


def ref_adjustment(pk):
    return f'PRN-ADJ-{pk}'


def ref_customer(pk):
    return f'PRN-CUST-{pk}'


def ref_supplier(pk):
    return f'PRN-SUP-{pk}'


def ref_treasury_opening(pk):
    return f'PRN-TRS-{pk}-OPEN'


def ref_material_opening(pk):
    return f'PRN-MAT-{pk}-OPEN'


# ---------------------------------------------------------------------------
# Core: bring the ledger under ``reference`` in line with ``target``
# ---------------------------------------------------------------------------
def _ledger_by_account(reference):
    from inventory.models import AccountingEntry
    rows = (AccountingEntry.objects.filter(journal_entry__reference=reference)
            .values('account_id').annotate(d=Sum('debit'), c=Sum('credit')))
    return {r['account_id']: (r['d'] or ZERO) - (r['c'] or ZERO) for r in rows}


def _is_open(je):
    from inventory.models import AccountingPeriod
    period = je.period or AccountingPeriod.for_date(je.date)
    return period is None or not period.is_closed


def sync(reference, target, *, description, journal_type='general', date=None, created_by=None):
    """
    Make the ledger hold exactly ``target`` under ``reference``.

    ``target`` is an iterable of ``(account_key, signed_amount)`` — positive is a
    debit, negative a credit — and must net to zero. Returns the JournalEntry
    posted (or None when the ledger already matches).
    """
    from inventory.models import ChartOfAccount, JournalEntry
    from inventory.services.accounting_service import AccountingService

    wanted = defaultdict(lambda: ZERO)
    for key, amount in target:
        amount = _q(amount)
        if amount:
            wanted[account(key).pk] += amount
    current = _ledger_by_account(reference)
    accounts = set(wanted) | set(current)
    if all(_q(wanted.get(a, ZERO) - current.get(a, ZERO)) == 0 for a in accounts):
        return None

    existing = list(JournalEntry.objects.filter(reference=reference).select_related('period'))
    with transaction.atomic():
        if all(_is_open(je) for je in existing):
            # الفترة مفتوحة: نعيد كتابة قيد المستند صح بتاريخه.
            for je in existing:
                je.delete()
            lines = [{'account': ChartOfAccount.objects.get(pk=a), 'debit': amt if amt > 0 else 0,
                      'credit': -amt if amt < 0 else 0}
                     for a, amt in wanted.items() if _q(amt)]
            if not lines:
                return None
            return AccountingService.post_journal(
                description=description, lines=lines, journal_type=journal_type,
                date=AccountingService.open_date(date) if date else None,
                reference=reference, created_by=created_by)
        # فيه قيد في فترة مقفولة: الفرق بس، بتاريخ النهارده.
        lines = []
        for a in accounts:
            delta = _q(wanted.get(a, ZERO) - current.get(a, ZERO))
            if delta:
                lines.append({'account': ChartOfAccount.objects.get(pk=a),
                              'debit': delta if delta > 0 else 0, 'credit': -delta if delta < 0 else 0})
        return AccountingService.post_journal(
            description=f'تسوية: {description}', lines=lines, journal_type='adjustment',
            reference=reference, created_by=created_by)


def unpost(reference, reason=''):
    """Take a deleted document's entries out of the ledger: deleted while the
    period is open, reversed (dated today) once it is closed."""
    return sync(reference, [], description=reason or f'إلغاء قيود {reference}')


def _safe(fn, *args, **kwargs):
    """القيد مايوقفش الشغل: لو فشل (فترة مقفولة…) بيتسجّل والمزامنة الجاية تصلّحه."""
    try:
        with transaction.atomic():
            return fn(*args, **kwargs)
    except Exception:
        logger.exception('[PRINT LEDGER] %s failed for %s', getattr(fn, '__name__', fn), args)
        return None


# ---------------------------------------------------------------------------
# Documents → target entries
# ---------------------------------------------------------------------------
def order_target(order):
    """القيد المفروض للطلب حسب حالته دلوقتي."""
    cost = _q(order.material_cost)
    if order.status == 'delivered':
        lines = [('ar', _q(order.net_total)), ('revenue', -_q(order.revenue)),
                 ('vat_output', -_q(order.tax_amount))]
        lines += [('cogs', cost), ('materials', -cost)]
        return lines, 'sales', order.date_delivered
    if order.status == 'cancelled':
        return [('spoilage', cost), ('materials', -cost)], 'adjustment', order.date_cancelled
    return [('wip', cost), ('materials', -cost)], 'general', order.date_created


def sync_order(order):
    from printing.models import PrintOrder
    order = PrintOrder.objects.select_related('customer').filter(pk=getattr(order, 'pk', order)).first()
    if order is None:
        return None
    lines, jtype, date = order_target(order)
    status = order.get_status_display()
    return sync(ref_order(order.pk), lines, journal_type=jtype, date=date,
                description=f'طلب طباعة #{order.order_number} — {order.customer.name} ({status})')


def txn_target(txn):
    amount = _q(txn.amount)
    sign = 1 if txn.transaction_type == 'in' else -1
    counter = TXN_COUNTER_ACCOUNT.get(txn.category or '', 'other_expense' if sign < 0 else 'other_income')
    return [('cash', sign * amount), (counter, -sign * amount)]


def sync_txn(txn):
    from printing.models import PrintTransaction
    txn = PrintTransaction.objects.select_related('treasury').filter(pk=getattr(txn, 'pk', txn)).first()
    if txn is None:
        return None
    jtype = 'cash_receipt' if txn.transaction_type == 'in' else 'cash_payment'
    desc = txn.description or f'{txn.get_category_display()} — {txn.treasury.name}'
    return sync(ref_txn(txn.pk), txn_target(txn), journal_type=jtype, date=txn.date,
                description=desc, created_by=txn.created_by)


def sync_purchase(purchase):
    from printing.models import MaterialPurchase
    p = MaterialPurchase.objects.select_related('supplier').filter(pk=getattr(purchase, 'pk', purchase)).first()
    if p is None:
        return None
    lines = []
    if p.status == 'received':
        lines = [('materials', _q(p.inventory_value)), ('vat_input', _q(p.tax_amount)),
                 ('ap', -_q(p.net_total))]
    return sync(ref_purchase(p.pk), lines, journal_type='purchase', date=p.date,
                description=f'فاتورة شراء خامات #{p.number} — {p.supplier.name}', created_by=p.created_by)


def sync_adjustment(adj):
    from printing.models import PrintStockAdjustment
    adj = PrintStockAdjustment.objects.select_related('material').filter(pk=getattr(adj, 'pk', adj)).first()
    if adj is None:
        return None
    value = adj.value
    return sync(ref_adjustment(adj.pk), [('materials', value), ('variance', -value)],
                journal_type='adjustment', date=adj.created_at, created_by=adj.created_by,
                description=f'تسوية مخزن — {adj.material.name} ({adj.get_reason_display()})')


def sync_customer(customer):
    """رصيد العميل الدائن (دافع أكتر من المستحق المسلّم) ← دفعات مقدمة من العملاء."""
    from printing.models import PrintCustomer
    cust = PrintCustomer.objects.filter(pk=getattr(customer, 'pk', customer)).first()
    if cust is None:
        return None
    delivered = sum((o.net_total for o in cust.printorder_set.filter(status='delivered')), ZERO)
    agg = cust.payments_qs().aggregate(ins=Sum('amount', filter=Q(transaction_type='in')),
                                       outs=Sum('amount', filter=Q(transaction_type='out')))
    paid = (agg['ins'] or ZERO) - (agg['outs'] or ZERO)
    excess = _q(paid - delivered)
    lines = [('ar', excess), ('customer_deposits', -excess)] if excess > 0 else []
    return sync(ref_customer(cust.pk), lines, journal_type='general',
                description=f'تبويب الرصيد المقدّم للعميل {cust.name} (دفعات مقدمة)')


def sync_supplier(supplier):
    """اللي دفعناه للمورد زيادة عن فواتيره المستلمة ← دفعات مقدمة للموردين."""
    from printing.models import PrintSupplier
    sup = PrintSupplier.objects.filter(pk=getattr(supplier, 'pk', supplier)).first()
    if sup is None:
        return None
    summary = sup.account_summary()
    excess = _q(-summary['balance'])
    lines = [('supplier_advances', excess), ('ap', -excess)] if excess > 0 else []
    return sync(ref_supplier(sup.pk), lines, journal_type='general',
                description=f'تبويب الدفعات المقدمة للمورد {sup.name}')


def post_treasury_opening(treasury, amount, date=None):
    amount = _q(amount)
    return sync(ref_treasury_opening(treasury.pk), [('cash', amount), ('opening_equity', -amount)],
                journal_type='opening', date=date,
                description=f'رصيد افتتاحي — خزنة {treasury.name}')


def post_material_opening(material, value, date=None):
    value = _q(value)
    return sync(ref_material_opening(material.pk), [('materials', value), ('opening_equity', -value)],
                journal_type='opening', date=date,
                description=f'مخزون افتتاحي — {material.name}')


# ---------------------------------------------------------------------------
# Safe entry points for signals (never break the business operation)
# ---------------------------------------------------------------------------
def on_order_changed(order_id, customer_id=None):
    from printing.models import PrintOrder
    order = PrintOrder.objects.filter(pk=order_id).first()
    if order is None:
        _safe(unpost, ref_order(order_id), 'حذف طلب طباعة')
    else:
        _safe(sync_order, order)
        customer_id = order.customer_id
    if customer_id:
        _safe(sync_customer, customer_id)


def on_txn_changed(txn_id, customer_id=None, supplier_id=None):
    from printing.models import PrintTransaction
    txn = PrintTransaction.objects.filter(pk=txn_id).first()
    if txn is None:
        _safe(unpost, ref_txn(txn_id), 'حذف حركة خزنة')
    else:
        _safe(sync_txn, txn)
    for cid in {customer_id, getattr(txn, 'customer_id', None)} - {None}:
        _safe(sync_customer, cid)
    for sid in {supplier_id, getattr(txn, 'supplier_id', None)} - {None}:
        _safe(sync_supplier, sid)


def on_purchase_changed(purchase_id, supplier_id=None):
    from printing.models import MaterialPurchase
    p = MaterialPurchase.objects.filter(pk=purchase_id).first()
    if p is None:
        _safe(unpost, ref_purchase(purchase_id), 'حذف فاتورة شراء')
    else:
        _safe(sync_purchase, p)
        supplier_id = p.supplier_id
    if supplier_id:
        _safe(sync_supplier, supplier_id)


def on_adjustment_changed(adj_id):
    from printing.models import PrintStockAdjustment
    if PrintStockAdjustment.objects.filter(pk=adj_id).exists():
        _safe(sync_adjustment, adj_id)
    else:
        _safe(unpost, ref_adjustment(adj_id), 'حذف تسوية مخزن')


# ---------------------------------------------------------------------------
# Bootstrap: opening balances + every existing document (idempotent)
# ---------------------------------------------------------------------------
def material_implied_opening_value(material):
    """قيمة المخزون الافتتاحي اللي تفسّر رصيد الخامة الحالي بعد كل حركاتها المسجّلة."""
    from printing.models import MaterialPurchase, PrintJobMaterial
    value = _q(material.quantity * material.cost_per_unit)
    for p in MaterialPurchase.objects.filter(status='received', lines__material=material).distinct():
        lines = list(p.lines.order_by('pk'))
        eff = p.effective_line_totals(lines)
        value -= sum((eff[l.pk] for l in lines if l.material_id == material.pk), ZERO)
    for use in PrintJobMaterial.objects.filter(material=material):
        value += _q(use.total_cost)
    for adj in material.adjustments.all():
        value -= adj.value
    return _q(value)


def bootstrap(force=False):
    """
    Post opening balances and sync every existing printing document.

    Runs once per tenant (``PrintLedgerState``) unless ``force``; each step is
    idempotent, so a forced re-run only repairs what drifted.
    """
    from inventory.models import JournalEntry
    from printing.models import (
        MaterialPurchase, PrintCustomer, PrintLedgerState, PrintMaterial, PrintOrder,
        PrintStockAdjustment, PrintSupplier, PrintTransaction, PrintTreasury,
    )
    state = PrintLedgerState.get()
    if state.bootstrapped_at and not force:
        return False

    for t in PrintTreasury.objects.all():
        if JournalEntry.objects.filter(reference=ref_treasury_opening(t.pk)).exists():
            continue
        agg = PrintTransaction.objects.filter(treasury=t).aggregate(
            ins=Sum('amount', filter=Q(transaction_type='in')),
            outs=Sum('amount', filter=Q(transaction_type='out')))
        opening = t.balance - (agg['ins'] or ZERO) + (agg['outs'] or ZERO)
        first = PrintTransaction.objects.filter(treasury=t).order_by('date').values_list('date', flat=True).first()
        _safe(post_treasury_opening, t, opening, first)

    for m in PrintMaterial.objects.all():
        if JournalEntry.objects.filter(reference=ref_material_opening(m.pk)).exists():
            continue
        _safe(post_material_opening, m, material_implied_opening_value(m), m.created_at)

    for pk in PrintOrder.objects.values_list('pk', flat=True):
        _safe(sync_order, pk)
    for pk in PrintTransaction.objects.values_list('pk', flat=True):
        _safe(sync_txn, pk)
    for pk in MaterialPurchase.objects.values_list('pk', flat=True):
        _safe(sync_purchase, pk)
    for pk in PrintStockAdjustment.objects.values_list('pk', flat=True):
        _safe(sync_adjustment, pk)
    for pk in PrintCustomer.objects.values_list('pk', flat=True):
        _safe(sync_customer, pk)
    for pk in PrintSupplier.objects.values_list('pk', flat=True):
        _safe(sync_supplier, pk)

    _bootstrap_payroll_advance_recoveries()

    state.bootstrapped_at = timezone.now()
    state.save(update_fields=['bootstrapped_at'])
    return True


def _bootstrap_payroll_advance_recoveries():
    """
    أقساط السلف اللي اتخصمت من مرتبات قديمة (قبل الدفتر) — من غيرها «سلف الموظفين»
    بيفضل شايل سلف اتسدّدت، ومصروف المرتبات في الشهور دي ناقص. بتتقيّد بتاريخ صرف
    المرتب نفسه، وبنفس مرجع PayrollService فمابتتكرّرش.
    """
    from inventory.models import JournalEntry
    from inventory.services.accounting_service import AccountingService
    try:
        from hr.models import PayrollRun
    except ImportError:
        return
    for run in PayrollRun.objects.filter(status='paid'):
        for entry in run.entries.select_related('employee__user'):
            reference = f"PAYROLL-{run.pk}-ADV-{entry.pk}"
            if JournalEntry.objects.filter(reference=reference).exists():
                continue
            recovered = (entry.advance_installments_deducted
                         .filter(status='deducted', advance__disbursed_at__isnull=False)
                         .aggregate(s=Sum('amount'))['s'] or ZERO)
            if recovered <= 0:
                continue
            name = entry.employee.user.get_full_name() or entry.employee.user.username
            _safe(AccountingService.post_journal,
                  description=f"خصم قسط سلفة من راتب {run.period_month}/{run.period_year} — {name}",
                  lines=[{'account': account('salaries'), 'debit': recovered, 'credit': 0},
                         {'account': account('employee_advances'), 'debit': 0, 'credit': recovered}],
                  journal_type='general', reference=reference,
                  date=AccountingService.open_date(run.paid_at) if run.paid_at else None)


def ensure_bootstrapped():
    from printing.models import PrintLedgerState
    if not PrintLedgerState.get().bootstrapped_at:
        bootstrap()
