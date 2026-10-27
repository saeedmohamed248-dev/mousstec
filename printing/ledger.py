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
    إشعار دائن (مرتجع بيع)   مدين مردودات المبيعات + ض.ق.م / دائن العملاء
    مرتجع مشتريات            مدين الموردين             / دائن مخزون الخامات + ض.ق.م مدخلات
    فاتورة مصروف/خدمة        مدين المصروف + ض.ق.م مدخلات / دائن الموردين
    شراء أصل ثابت            مدين الأصول الثابتة       / دائن الخزنة أو الموردين
    إهلاك شهري               مدين مصروف الإهلاك        / دائن مجمع الإهلاك
    بيع/استبعاد أصل          مدين مجمع الإهلاك + المتحصلات / دائن الأصل ± أرباح/خسائر
    اعتماد دورة رواتب        مدين رواتب وأجور          / دائن رواتب مستحقة (وبتتقفل بالصرف)
    إقرار ضريبة              مدين ض.ق.م المستحقة / دائن ض.ق.م مدخلات + صافي مستحق السداد
    رصيد افتتاحي عميل/مورد   مدين العملاء / دائن حقوق ملكية افتتاحية (والمورد بالعكس)
    إعدام دين / خصم مسموح    مدين ديون معدومة أو خصم مسموح به / دائن العملاء
    شيك وارد / صادر          مدين أوراق قبض / دائن العملاء — مدين الموردين / دائن أوراق دفع
    خصم من المنبع            مدين ضرائب خصم لينا / دائن العملاء — مدين الموردين / دائن خصم وإضافة مستحق

الشيكات والخصم من المنبع «حوافظ» زي الخزن بالظبط (حساب فرعي لكل حافظة)، فالتحصيل
والتوريد تحويلات عادية للبنك. كل قيد ليه فرع المطبعة بتاع مستنده (PrintJournalBranch)
عشان قائمة دخل لكل فرع.

كل خزنة/بنك/محفظة ليها حساب فرعي في الدليل (1001-<id> للخزن النقدية، 1002-<id>
للبنوك والمحافظ) عشان رصيد كل واحدة يطلع من الدفتر وتتطابق مع كشف البنك.

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

# بيزيد لما شكل القيود يتغيّر — أول فتح للدفاتر بعده بيعيد مزامنة كل المستندات مرة واحدة.
#   2: حساب لكل خزنة + مرتجعات/أصول/فواتير مصروفات/رواتب مستحقة/إقرارات ضريبة.
#   3: فرع لكل قيد + أرصدة افتتاحية للعملاء والموردين + إهلاك لكل فرع.
LEDGER_VERSION = 3

# key → (code, name, type). أي حساب ناقص بيتعمل أول ما يتقيّد عليه.
PRINT_ACCOUNTS = {
    'cash':              ('1001', 'النقدية والخزائن', 'asset'),
    'bank':              ('1002', 'البنوك والمحافظ الإلكترونية', 'asset'),
    'transfer':          ('1090', 'تحويلات بين الخزائن (حساب وسيط)', 'asset'),
    'ar':                ('1100', 'المدينون (ذمم العملاء)', 'asset'),
    'cheques_receivable': ('1120', 'أوراق قبض (شيكات تحت التحصيل)', 'asset'),
    'employee_advances': ('1150', 'سلف وعهد الموظفين', 'asset'),
    'materials':         ('1200', 'المخزون', 'asset'),
    'wip':               ('1220', 'أعمال تحت التشغيل (طلبات الطباعة)', 'asset'),
    'vat_input':         ('1250', 'ضريبة القيمة المضافة (مدخلات)', 'asset'),
    'supplier_advances': ('1260', 'دفعات مقدمة للموردين', 'asset'),
    'wht_receivable':    ('1270', 'ضرائب خصم من المنبع (لينا عند الضرايب)', 'asset'),
    'fixed_assets':      ('1300', 'الأصول الثابتة (ماكينات ومعدات)', 'asset'),
    'accum_depreciation': ('1390', 'مجمع إهلاك الأصول الثابتة', 'asset'),
    'asset_disposal':    ('1395', 'متحصلات بيع أصول (حساب وسيط)', 'asset'),
    'ap':                ('2100', 'الدائنون (ذمم الموردين)', 'liability'),
    'cheques_payable':   ('2130', 'أوراق دفع (شيكات صادرة)', 'liability'),
    'salaries_payable':  ('2150', 'رواتب مستحقة الصرف', 'liability'),
    'vat_output':        ('2200', 'ضريبة القيمة المضافة المستحقة', 'liability'),
    'vat_settlement':    ('2210', 'ضريبة القيمة المضافة — صافي مستحق السداد', 'liability'),
    'wht_payable':       ('2230', 'ضرائب خصم وإضافة مستحقة التوريد', 'liability'),
    'customer_deposits': ('2400', 'دفعات مقدمة من العملاء', 'liability'),
    'capital':           ('3001', 'رأس المال', 'equity'),
    'opening_equity':    ('3005', 'رصيد افتتاحي (حقوق ملكية)', 'equity'),
    'revenue':           ('4010', 'إيرادات أعمال الطباعة والتصميم', 'revenue'),
    'sales_returns':     ('4090', 'مردودات ومسموحات المبيعات', 'revenue'),
    'asset_gain':        ('4095', 'أرباح بيع أصول ثابتة', 'revenue'),
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
    'depreciation':      ('5600', 'مصروف إهلاك الأصول الثابتة', 'expense'),
    'asset_loss':        ('5650', 'خسائر بيع / استبعاد أصول ثابتة', 'expense'),
    'bad_debts':         ('5800', 'ديون معدومة', 'expense'),
    'discount_allowed':  ('5820', 'خصم مسموح به وفروق كسور', 'expense'),
}

# نوع الإشعار الدائن ← الحساب المدين (المرتجع بيقلّل الإيراد، الباقي مصروف).
CREDIT_NOTE_ACCOUNT = {'return': 'sales_returns', 'discount': 'discount_allowed', 'bad_debt': 'bad_debts'}

# نوع الخزنة/الحافظة ← الحساب الأب في الدليل.
TREASURY_PARENT = {
    'cash': 'cash', 'bank': 'bank', 'wallet': 'bank',
    'cheques_in': 'cheques_receivable', 'cheques_out': 'cheques_payable',
    'wht_in': 'wht_receivable', 'wht_out': 'wht_payable',
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
    'fixed_asset': 'fixed_assets',
    'asset_sale': 'asset_disposal',
    'payroll': 'salaries_payable',
    'vat': 'vat_settlement',
}

# بند فاتورة المصروف ← حساب المصروف.
BILL_EXPENSE_ACCOUNT = {
    'rent': 'rent',
    'utilities': 'utilities',
    'maintenance': 'maintenance',
    'shipping': 'shipping',
    'marketing': 'marketing',
    'materials': 'materials_expense',
    'other_expense': 'other_expense',
}

TREASURY_LABELS = {'cash': 'خزنة', 'bank': 'بنك', 'wallet': 'محفظة'}


def _q(value):
    from inventory.services.accounting_service import _q as q
    return q(value)


def account(key):
    from inventory.services.accounting_service import AccountingService
    if hasattr(key, 'pk'):
        return key
    code, name, acc_type = PRINT_ACCOUNTS[key]
    return AccountingService.account(code, name, acc_type)


def treasury_account(treasury):
    """
    الحساب الفرعي للخزنة في الدليل (تحت النقدية أو البنوك حسب نوعها).

    🐛 [FIX]: كل الخزن والبنوك كانت على حساب «النقدية» واحد — مفيش رصيد لكل
    خزنة في الدفتر، ومطابقة البنك مع كشفه مستحيلة.
    """
    from inventory.models import ChartOfAccount
    from printing.models import PrintTreasury
    if not hasattr(treasury, 'kind'):
        treasury = PrintTreasury.objects.get(pk=treasury)
    parent = account(TREASURY_PARENT.get(treasury.kind, 'cash'))
    label = TREASURY_LABELS.get(treasury.kind)
    name = (f'{label}: {treasury.name}' if label else treasury.name)[:200]
    acc, created = ChartOfAccount.objects.get_or_create(
        code=f'{parent.code}-{treasury.pk}',
        defaults={'name': name, 'account_type': parent.account_type, 'parent': parent})
    if not created and (acc.name != name or acc.parent_id != parent.pk or acc.account_type != parent.account_type):
        ChartOfAccount.objects.filter(pk=acc.pk).update(name=name, parent=parent, account_type=parent.account_type)
    return acc



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


def ref_credit_note(pk):
    return f'PRN-CN-{pk}'


def ref_asset(pk):
    return f'PRN-FA-{pk}'


def ref_asset_disposal(pk):
    return f'PRN-FA-{pk}-DISP'


def ref_depreciation(year, month):
    return f'PRN-DEP-{year}{month:02d}'


def ref_bill(pk):
    return f'PRN-BILL-{pk}'


def ref_purchase_return(pk):
    return f'PRN-PRET-{pk}'


def ref_payroll(pk):
    return f'PRN-PAYROLL-{pk}'


def ref_vat_return(pk):
    return f'PRN-VAT-{pk}'


def ref_customer_opening(pk):
    return f'PRN-CUST-{pk}-OPEN'


def ref_supplier_opening(pk):
    return f'PRN-SUP-{pk}-OPEN'


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


def _tag_branch(reference, branch_id):
    """فرع المطبعة لقيود المستند (القيود في فترة مقفولة بتفضل على فرعها القديم)."""
    from inventory.models import JournalEntry
    from printing.models import PrintJournalBranch
    entries = list(JournalEntry.objects.filter(reference=reference).select_related('period'))
    if not entries:
        return
    tags = {t.journal_entry_id: t for t in PrintJournalBranch.objects.filter(journal_entry__in=entries)}
    for je in entries:
        tag = tags.get(je.pk)
        if branch_id is None:
            if tag is not None and _is_open(je):
                tag.delete()
        elif tag is None:
            PrintJournalBranch.objects.create(journal_entry=je, branch_id=branch_id)
        elif tag.branch_id != branch_id and _is_open(je):
            PrintJournalBranch.objects.filter(pk=tag.pk).update(branch_id=branch_id)


def sync(reference, target, *, description, journal_type='general', date=None, created_by=None, branch=None):
    """
    Make the ledger hold exactly ``target`` under ``reference``.

    ``target`` is an iterable of ``(account_key, signed_amount)`` — positive is a
    debit, negative a credit — and must net to zero. ``branch`` (PrintBranch id)
    tags the entries for per-branch reports. Returns the JournalEntry posted (or
    None when the ledger already matches).
    """
    je = _sync(reference, target, description=description, journal_type=journal_type, date=date,
               created_by=created_by)
    _tag_branch(reference, branch)
    return je


def _sync(reference, target, *, description, journal_type='general', date=None, created_by=None):
    from inventory.models import ChartOfAccount, JournalEntry
    from inventory.services.accounting_service import AccountingService

    wanted = defaultdict(lambda: ZERO)
    for key, amount in target:      # key: مفتاح من PRINT_ACCOUNTS أو ChartOfAccount نفسه
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
    return sync(ref_order(order.pk), lines, journal_type=jtype, date=date, branch=order.branch_id,
                description=f'طلب طباعة #{order.order_number} — {order.customer.name} ({status})')


def txn_target(txn):
    amount = _q(txn.amount)
    sign = 1 if txn.transaction_type == 'in' else -1
    counter = TXN_COUNTER_ACCOUNT.get(txn.category or '', 'other_expense' if sign < 0 else 'other_income')
    return [(treasury_account(txn.treasury), sign * amount), (counter, -sign * amount)]


def sync_txn(txn):
    from printing.models import PrintTransaction
    txn = (PrintTransaction.objects.select_related('treasury', 'order', 'purchase', 'bill')
           .filter(pk=getattr(txn, 'pk', txn)).first())
    if txn is None:
        return None
    jtype = 'cash_receipt' if txn.transaction_type == 'in' else 'cash_payment'
    desc = txn.description or f'{txn.get_category_display()} — {txn.treasury.name}'
    doc = txn.order or txn.purchase or txn.bill
    branch = getattr(doc, 'branch_id', None) or txn.treasury.branch_id
    return sync(ref_txn(txn.pk), txn_target(txn), journal_type=jtype, date=txn.date, branch=branch,
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
    return sync(ref_purchase(p.pk), lines, journal_type='purchase', date=p.date, branch=p.branch_id,
                description=f'فاتورة شراء خامات #{p.number} — {p.supplier.name}', created_by=p.created_by)


def sync_adjustment(adj):
    from printing.models import PrintStockAdjustment
    adj = PrintStockAdjustment.objects.select_related('material').filter(pk=getattr(adj, 'pk', adj)).first()
    if adj is None:
        return None
    value = adj.value
    return sync(ref_adjustment(adj.pk), [('materials', value), ('variance', -value)],
                journal_type='adjustment', date=adj.created_at, created_by=adj.created_by,
                branch=adj.material.branch_id,
                description=f'تسوية مخزن — {adj.material.name} ({adj.get_reason_display()})')


def sync_customer(customer):
    """رصيد العميل الدائن (دافع أكتر من المستحق المسلّم) ← دفعات مقدمة من العملاء."""
    from printing.models import PrintCustomer
    cust = PrintCustomer.objects.filter(pk=getattr(customer, 'pk', customer)).first()
    if cust is None:
        return None
    # المسلَّم بعد المردودات + الرصيد الافتتاحي (بعد الإعدام/الخصم على الحساب نفسه)
    delivered = sum((o.due_amount for o in cust.printorder_set.filter(status='delivered')), ZERO)
    delivered += cust.account_level_debt()
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
    return sync(ref_treasury_opening(treasury.pk),
                [(treasury_account(treasury), amount), ('opening_equity', -amount)],
                journal_type='opening', date=date, branch=treasury.branch_id,
                description=f'رصيد افتتاحي — خزنة {treasury.name}')


def sync_customer_opening(customer):
    """مديونية العميل قبل السيستم: مدين العملاء / دائن رصيد افتتاحي (والعكس لو ليه)."""
    from printing.models import PrintCustomer
    cust = PrintCustomer.objects.filter(pk=getattr(customer, 'pk', customer)).first()
    if cust is None:
        return None
    amount = _q(cust.opening_balance)
    return sync(ref_customer_opening(cust.pk), [('ar', amount), ('opening_equity', -amount)],
                journal_type='opening', date=cust.opening_date,
                description=f'رصيد افتتاحي — العميل {cust.name}')


def sync_supplier_opening(supplier):
    """المستحق للمورد قبل السيستم: مدين رصيد افتتاحي / دائن الموردين (والعكس لو لينا)."""
    from printing.models import PrintSupplier
    sup = PrintSupplier.objects.filter(pk=getattr(supplier, 'pk', supplier)).first()
    if sup is None:
        return None
    amount = _q(sup.opening_balance)
    return sync(ref_supplier_opening(sup.pk), [('opening_equity', amount), ('ap', -amount)],
                journal_type='opening', date=sup.opening_date,
                description=f'رصيد افتتاحي — المورد {sup.name}')


def treasury_opening(treasury):
    """(المبلغ، التاريخ) للرصيد الافتتاحي: من قيده لو موجود، وإلا من رصيد الخزنة − حركاتها."""
    from inventory.models import AccountingEntry, JournalEntry
    from printing.models import PrintTransaction
    ref = ref_treasury_opening(treasury.pk)
    je = JournalEntry.objects.filter(reference=ref).order_by('date', 'pk').first()
    if je is not None:
        agg = AccountingEntry.objects.filter(journal_entry__reference=ref, account=account('opening_equity')) \
            .aggregate(d=Sum('debit'), c=Sum('credit'))
        return (agg['c'] or ZERO) - (agg['d'] or ZERO), je.date
    agg = PrintTransaction.objects.filter(treasury=treasury).aggregate(
        ins=Sum('amount', filter=Q(transaction_type='in')),
        outs=Sum('amount', filter=Q(transaction_type='out')))
    first = PrintTransaction.objects.filter(treasury=treasury).order_by('date').values_list('date', flat=True).first()
    return treasury.balance - (agg['ins'] or ZERO) + (agg['outs'] or ZERO), first


def resync_treasury(treasury):
    """الخزنة اتغيّر نوعها (خزنة ↔ بنك): رصيدها الافتتاحي وكل حركاتها ينقلوا لحسابها الجديد."""
    from printing.models import PrintTransaction
    amount, date = treasury_opening(treasury)
    post_treasury_opening(treasury, amount, date)
    for pk in PrintTransaction.objects.filter(treasury=treasury).values_list('pk', flat=True):
        sync_txn(pk)


def post_material_opening(material, value, date=None):
    value = _q(value)
    return sync(ref_material_opening(material.pk), [('materials', value), ('opening_equity', -value)],
                journal_type='opening', date=date, branch=material.branch_id,
                description=f'مخزون افتتاحي — {material.name}')


def sync_credit_note(cn):
    """مرتجع/مسموح بيع بعد التسليم: بيعكس الإيراد والضريبة وبيقلّل مديونية العميل."""
    from printing.models import PrintCreditNote
    cn = (PrintCreditNote.objects.select_related('order__customer', 'customer')
          .filter(pk=getattr(cn, 'pk', cn)).first())
    if cn is None:
        return None
    debit = CREDIT_NOTE_ACCOUNT.get(cn.kind, 'sales_returns')
    lines = [(debit, _q(cn.amount)), ('vat_output', _q(cn.tax_amount)), ('ar', -_q(cn.total))]
    where = f'طلب #{cn.order.order_number} ' if cn.order_id else ''
    customer = cn.customer.name if cn.customer_id else cn.order.customer.name
    return sync(ref_credit_note(cn.pk), lines, journal_type='sales' if cn.kind == 'return' else 'adjustment',
                date=cn.date, created_by=cn.created_by, branch=cn.order.branch_id if cn.order_id else None,
                description=f'{cn.get_kind_display()} #{cn.number} — {where}({customer}): {cn.reason}'[:255])


def asset_target(asset):
    """قيد إثبات الأصل. النقدي بيتقيّد من حركة الخزنة نفسها (بند «شراء أصل ثابت»)."""
    cost = _q(asset.cost)
    if asset.funding == 'credit':
        return [('fixed_assets', cost), ('ap', -cost)]
    if asset.funding == 'opening':
        acc = _q(asset.opening_accumulated)
        return [('fixed_assets', cost), ('accum_depreciation', -acc), ('opening_equity', -(cost - acc))]
    return []


def disposal_target(asset):
    """بيع/استبعاد: يقفل التكلفة ومجمع إهلاكها، والفرق مع المتحصلات ربح أو خسارة."""
    if not asset.disposed_on:
        return []
    cost = _q(asset.cost)
    acc = _q(asset.accumulated_until(asset.disposed_on))
    proceeds = _q(asset.disposal_amount)
    gain = proceeds - (cost - acc)
    return [('accum_depreciation', acc), ('asset_disposal', proceeds), ('fixed_assets', -cost),
            ('asset_gain' if gain > 0 else 'asset_loss', -gain)]


def sync_fixed_asset(asset):
    from printing.models import PrintFixedAsset
    a = PrintFixedAsset.objects.select_related('supplier').filter(pk=getattr(asset, 'pk', asset)).first()
    if a is None:
        return None
    sync(ref_asset(a.pk), asset_target(a), journal_type='opening' if a.funding == 'opening' else 'purchase',
         date=a.purchase_date, created_by=a.created_by, branch=a.branch_id,
         description=f'إثبات أصل ثابت — {a.name}' + (f' ({a.supplier.name})' if a.supplier_id else ''))
    return sync(ref_asset_disposal(a.pk), disposal_target(a), journal_type='adjustment', branch=a.branch_id,
                date=a.disposed_on, created_by=a.created_by, description=f'بيع / استبعاد أصل ثابت — {a.name}')


def _month_end(year, month):
    import calendar
    from datetime import date as _date
    return _date(year, month, calendar.monthrange(year, month)[1])


def _months(start, end):
    """(سنة، شهر) من شهر ``start`` لحد شهر ``end``."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def last_completed_month_end(today=None):
    """آخر يوم في آخر شهر خلص (النهارده لو هو آخر الشهر)."""
    from datetime import timedelta
    today = today or timezone.localdate()
    end = _month_end(today.year, today.month)
    return end if today == end else today.replace(day=1) - timedelta(days=1)


def sync_depreciation_month(year, month):
    """
    قسط إهلاك الشهر — قيد لكل فرع بتاريخ آخر الشهر (``PRN-DEP-YYYYMM`` للأصول
    اللي مالهاش فرع، و ``PRN-DEP-YYYYMM-B<id>`` لكل فرع).
    """
    from inventory.models import JournalEntry
    from printing.models import PrintFixedAsset
    base = ref_depreciation(year, month)
    totals = defaultdict(lambda: ZERO)
    for a in PrintFixedAsset.objects.all():
        totals[a.branch_id] += a.depreciation_for_month(year, month)
    # فروع كان ليها قسط واتشال (أصل اتمسح/اتنقل) — قيدها يتصفّر.
    for ref in JournalEntry.objects.filter(reference__startswith=f'{base}-B').values_list('reference', flat=True):
        try:
            totals.setdefault(int(ref.rsplit('-B', 1)[1]), ZERO)
        except ValueError:
            continue
    totals.setdefault(None, ZERO)
    posted = None
    for branch_id, total in totals.items():
        total = _q(total)
        je = sync(f'{base}-B{branch_id}' if branch_id else base,
                  [('depreciation', total), ('accum_depreciation', -total)],
                  journal_type='adjustment', date=_month_end(year, month), branch=branch_id,
                  description=f'قسط إهلاك الأصول الثابتة — {month}/{year}')
        posted = posted or je
    return posted


def ensure_depreciation(rebuild_from=None, today=None):
    """
    قيّد إهلاك كل شهر خلص ولسه ماتقيّدش. ``rebuild_from``: أصل اتضاف/اتعدّل/اتمسح
    بتاريخ قديم ← نعيد حساب الشهور من أول شهره (المزامنة بتصلّح الفرق بس).
    """
    from datetime import timedelta
    from printing.models import PrintFixedAsset, PrintLedgerState
    state = PrintLedgerState.get()
    through = last_completed_month_end(today)
    if rebuild_from is not None:
        start = rebuild_from
    elif state.depreciation_through:
        if state.depreciation_through >= through:
            return 0
        start = state.depreciation_through + timedelta(days=1)
    else:
        start = min((a.start for a in PrintFixedAsset.objects.all()), default=None)
    posted = 0
    if start is not None and start <= through:
        for y, m in _months(start, through):
            if _safe(sync_depreciation_month, y, m) is not None:
                posted += 1
    if state.depreciation_through is None or state.depreciation_through < through:
        PrintLedgerState.objects.filter(pk=state.pk).update(depreciation_through=through)
    return posted


def sync_bill(bill):
    from printing.models import PrintExpenseBill
    b = PrintExpenseBill.objects.select_related('supplier').filter(pk=getattr(bill, 'pk', bill)).first()
    if b is None:
        return None
    lines = [] if b.is_cancelled else [
        (BILL_EXPENSE_ACCOUNT.get(b.category, 'other_expense'), _q(b.amount)),
        ('vat_input', _q(b.tax_amount)), ('ap', -_q(b.total))]
    return sync(ref_bill(b.pk), lines, journal_type='purchase', date=b.date, created_by=b.created_by,
                branch=b.branch_id,
                description=f'فاتورة مصروف #{b.number} — {b.supplier.name}: {b.description}'[:255])


def sync_purchase_return(ret):
    from printing.models import MaterialPurchaseReturn
    r = MaterialPurchaseReturn.objects.select_related('purchase__supplier').filter(pk=getattr(ret, 'pk', ret)).first()
    if r is None:
        return None
    lines = [('ap', _q(r.total)), ('materials', -_q(r.value)), ('vat_input', -_q(r.tax_amount))] \
        if r.status == 'posted' else []
    return sync(ref_purchase_return(r.pk), lines, journal_type='purchase', date=r.date, created_by=r.created_by,
                branch=r.purchase.branch_id,
                description=f'مرتجع مشتريات #{r.number} — فاتورة #{r.purchase.number} ({r.purchase.supplier.name})')


def payroll_accrual(run):
    """
    صافي المرتبات المستحقة للدورة المعتمدة. الدورة المصروفة بتتستحق بمرتبات
    اتصرفت ببند «صرف مرتبات مستحقة» بس — الدورات القديمة اللي اتصرفت ببند
    «رواتب» مصروفها اتقيّد وقت الصرف، فمابتتستحقش تاني.
    """
    from printing.models import PrintTransaction
    if run.status not in ('approved', 'paid'):
        return ZERO
    total = ZERO
    for entry in run.entries.all():
        if entry.net_salary <= 0:
            continue
        if run.status == 'paid' and not (entry.treasury_transaction_id and PrintTransaction.objects.filter(
                pk=entry.treasury_transaction_id, category='payroll').exists()):
            continue
        total += entry.net_salary
    return _q(total)


def sync_payroll_run(run):
    try:
        from hr.models import PayrollRun
    except ImportError:
        return None
    run = PayrollRun.objects.filter(pk=getattr(run, 'pk', run)).first()
    if run is None:
        return None
    amount = payroll_accrual(run)
    return sync(ref_payroll(run.pk), [('salaries', amount), ('salaries_payable', -amount)],
                journal_type='general', date=_month_end(run.period_year, run.period_month),
                description=f'استحقاق رواتب {run.period_month}/{run.period_year}')


def vat_balances(as_of, exclude_reference=None):
    """رصيد الضريبة المستحقة (مخرجات) والمدفوعة (مدخلات) لحد تاريخ — اللي لسه ماتقفلش في إقرار."""
    from inventory.models import AccountingEntry
    def bal(key):
        qs = AccountingEntry.objects.filter(account=account(key), journal_entry__date__lte=as_of)
        if exclude_reference:
            qs = qs.exclude(journal_entry__reference=exclude_reference)
        agg = qs.aggregate(d=Sum('debit'), c=Sum('credit'))
        return (agg['d'] or ZERO) - (agg['c'] or ZERO)
    return _q(-bal('vat_output')), _q(bal('vat_input'))


def vat_activity(date_from, date_to):
    """ضريبة المبيعات والمشتريات اللي اتقيّدت في الفترة (من غير قيود الإقرارات)."""
    from inventory.models import AccountingEntry
    def net(key):
        agg = (AccountingEntry.objects.filter(account=account(key), journal_entry__date__gte=date_from,
                                              journal_entry__date__lte=date_to)
               .exclude(journal_entry__reference__startswith='PRN-VAT-')
               .aggregate(d=Sum('debit'), c=Sum('credit')))
        return (agg['d'] or ZERO) - (agg['c'] or ZERO)
    return _q(-net('vat_output')), _q(net('vat_input'))


def sync_vat_return(vat):
    from printing.models import PrintVatReturn
    v = PrintVatReturn.objects.filter(pk=getattr(vat, 'pk', vat)).first()
    if v is None:
        return None
    lines = [('vat_output', _q(v.output_vat)), ('vat_input', -_q(v.input_vat)),
             ('vat_settlement', -_q(v.net_payable))]
    return sync(ref_vat_return(v.pk), lines, journal_type='adjustment', date=v.date_to, created_by=v.created_by,
                description=f'إقرار ضريبة القيمة المضافة {v.date_from} → {v.date_to}')


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


def on_credit_note_changed(cn_id, order_id, customer_id=None):
    from printing.models import PrintCreditNote, PrintOrder
    order = PrintOrder.objects.filter(pk=order_id).first() if order_id else None
    if order is not None:
        PrintOrder.objects.filter(pk=order_id).update(credited_amount=order.compute_credited())
        customer_id = order.customer_id
    if PrintCreditNote.objects.filter(pk=cn_id).exists():
        _safe(sync_credit_note, cn_id)
    else:
        _safe(unpost, ref_credit_note(cn_id), 'حذف إشعار دائن')
    if customer_id:
        _safe(sync_customer, customer_id)


def on_asset_changed(asset_id, supplier_id=None, rebuild_from=None):
    from printing.models import PrintFixedAsset
    a = PrintFixedAsset.objects.filter(pk=asset_id).first()
    if a is None:
        _safe(unpost, ref_asset(asset_id), 'حذف أصل ثابت')
        _safe(unpost, ref_asset_disposal(asset_id), 'حذف أصل ثابت')
    else:
        # الإهلاك الأول عشان المتراكم وقت البيع يبقى متقيّد.
        starts = [d for d in (rebuild_from, a.start) if d]
        ensure_depreciation(rebuild_from=min(starts))
        _safe(sync_fixed_asset, a)
        supplier_id = supplier_id or a.supplier_id
    if a is None and rebuild_from:
        ensure_depreciation(rebuild_from=rebuild_from)
    for sid in {supplier_id, getattr(a, 'supplier_id', None)} - {None}:
        _safe(sync_supplier, sid)


def on_bill_changed(bill_id, supplier_id=None):
    from printing.models import PrintExpenseBill
    b = PrintExpenseBill.objects.filter(pk=bill_id).first()
    if b is None:
        _safe(unpost, ref_bill(bill_id), 'حذف فاتورة مصروف')
    else:
        PrintExpenseBill.objects.filter(pk=bill_id).update(paid_amount=b.compute_paid())
        _safe(sync_bill, b)
        supplier_id = b.supplier_id
    if supplier_id:
        _safe(sync_supplier, supplier_id)


def on_purchase_return_changed(ret_id, supplier_id=None):
    from printing.models import MaterialPurchaseReturn
    r = MaterialPurchaseReturn.objects.select_related('purchase').filter(pk=ret_id).first()
    if r is None:
        _safe(unpost, ref_purchase_return(ret_id), 'حذف مرتجع مشتريات')
    else:
        _safe(sync_purchase_return, r)
        supplier_id = r.purchase.supplier_id
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
    from printing.models import MaterialPurchaseReturnLine
    for line in MaterialPurchaseReturnLine.objects.filter(ret__status='posted', purchase_line__material=material):
        value += line.value
    return _q(value)


def bootstrap(force=False):
    """
    Post opening balances and sync every existing printing document.

    Runs once per tenant (``PrintLedgerState``) unless ``force``; each step is
    idempotent, so a forced re-run only repairs what drifted.
    """
    from inventory.models import JournalEntry
    from printing.models import (
        MaterialPurchase, MaterialPurchaseReturn, PrintCreditNote, PrintCustomer, PrintExpenseBill,
        PrintFixedAsset, PrintLedgerState, PrintMaterial, PrintOrder, PrintStockAdjustment, PrintSupplier,
        PrintTransaction, PrintTreasury, PrintVatReturn,
    )
    state = PrintLedgerState.get()
    if state.bootstrapped_at and state.version >= LEDGER_VERSION and not force:
        return False

    # الرصيد الافتتاحي لكل خزنة على حسابها (والقديم اللي كان على «النقدية» بيتنقل).
    for t in PrintTreasury.objects.all():
        amount, date = treasury_opening(t)
        _safe(post_treasury_opening, t, amount, date)

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
    for pk in PrintCreditNote.objects.values_list('pk', flat=True):
        _safe(sync_credit_note, pk)
    for pk in PrintExpenseBill.objects.values_list('pk', flat=True):
        _safe(sync_bill, pk)
    for pk in MaterialPurchaseReturn.objects.values_list('pk', flat=True):
        _safe(sync_purchase_return, pk)
    first_asset = min((a.start for a in PrintFixedAsset.objects.all()), default=None)
    if first_asset:
        ensure_depreciation(rebuild_from=first_asset)
    for pk in PrintFixedAsset.objects.values_list('pk', flat=True):
        _safe(sync_fixed_asset, pk)
    for pk in PrintVatReturn.objects.values_list('pk', flat=True):
        _safe(sync_vat_return, pk)
    for pk in PrintCustomer.objects.values_list('pk', flat=True):
        _safe(sync_customer_opening, pk)
        _safe(sync_customer, pk)
    for pk in PrintSupplier.objects.values_list('pk', flat=True):
        _safe(sync_supplier_opening, pk)
        _safe(sync_supplier, pk)

    _bootstrap_payroll_advance_recoveries()
    try:
        from hr.models import PayrollRun
        for pk in PayrollRun.objects.filter(status__in=('approved', 'paid')).values_list('pk', flat=True):
            _safe(sync_payroll_run, pk)
    except ImportError:
        pass

    state.bootstrapped_at = timezone.now()
    state.version = LEDGER_VERSION
    state.save(update_fields=['bootstrapped_at', 'version'])
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
    """قبل أي تقرير: الدفتر متقيّد بآخر شكل للقيود، وإهلاك الشهور اللي خلصت متقيّد."""
    from printing.models import PrintLedgerState
    state = PrintLedgerState.get()
    if not state.bootstrapped_at or state.version < LEDGER_VERSION:
        bootstrap()
    ensure_depreciation()


# ---------------------------------------------------------------------------
# Per-branch income (قائمة دخل لكل فرع)
# ---------------------------------------------------------------------------
def _pnl_entries(date_from, date_to):
    from inventory.models import AccountingEntry
    return (AccountingEntry.objects
            .filter(entry_date__date__gte=date_from, entry_date__date__lte=date_to,
                    account__account_type__in=('revenue', 'expense'))
            .exclude(journal_entry__journal_type='closing'))


def branch_income_statement(date_from, date_to, branch_id=None):
    """
    قائمة دخل بنفس شكل ``AccountingReportService.income_statement`` لفرع واحد —
    ``branch_id=None`` = القيود العامة اللي مالهاش فرع (مرتبات، تبويب أرصدة…).
    """
    qs = _pnl_entries(date_from, date_to)
    qs = (qs.filter(journal_entry__print_branch__isnull=True) if branch_id is None
          else qs.filter(journal_entry__print_branch__branch_id=branch_id))
    rows = (qs.values('account__code', 'account__name', 'account__account_type')
            .annotate(d=Sum('debit'), c=Sum('credit')).order_by('account__code'))
    sections = {'revenue': [], 'expense': []}
    for r in rows:
        d, c = r['d'] or ZERO, r['c'] or ZERO
        amount = c - d if r['account__account_type'] == 'revenue' else d - c
        if amount:
            sections[r['account__account_type']].append(
                {'code': r['account__code'], 'name': r['account__name'], 'amount': amount})
    revenue = sum((i['amount'] for i in sections['revenue']), ZERO)
    expenses = sum((i['amount'] for i in sections['expense']), ZERO)
    return {
        'date_from': str(date_from), 'date_to': str(date_to),
        'revenue': {'items': sections['revenue'], 'total': revenue},
        'expenses': {'items': sections['expense'], 'total': expenses},
        'net_profit': revenue - expenses, 'is_profit': revenue - expenses >= 0,
    }


def income_by_branch(date_from, date_to):
    """صافي الربح لكل فرع جنب بعض + «عام» للقيود اللي مالهاش فرع."""
    from printing.models import PrintBranch
    cogs_code = PRINT_ACCOUNTS['cogs'][0]
    rows = (_pnl_entries(date_from, date_to)
            .values('journal_entry__print_branch__branch_id', 'account__account_type', 'account__code')
            .annotate(d=Sum('debit'), c=Sum('credit')))
    data = defaultdict(lambda: {'revenue': ZERO, 'cogs': ZERO, 'expenses': ZERO})
    for r in rows:
        d, c = r['d'] or ZERO, r['c'] or ZERO
        bucket = data[r['journal_entry__print_branch__branch_id']]
        if r['account__account_type'] == 'revenue':
            bucket['revenue'] += c - d
        elif r['account__code'] == cogs_code:
            bucket['cogs'] += d - c
        else:
            bucket['expenses'] += d - c
    branches = {b.pk: b for b in PrintBranch.objects.all()}
    result = []
    for branch_id in [pk for pk in branches] + [None]:
        v = data.get(branch_id)
        if v is None and branch_id is not None:
            v = {'revenue': ZERO, 'cogs': ZERO, 'expenses': ZERO}
        if v is None:
            continue
        result.append({'branch': branches.get(branch_id), 'revenue': v['revenue'], 'cogs': v['cogs'],
                       'gross': v['revenue'] - v['cogs'], 'expenses': v['expenses'],
                       'net': v['revenue'] - v['cogs'] - v['expenses']})
    return result
