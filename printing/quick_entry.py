"""⚡ شاشات الإدخال السريع للمطابع: تحصيل من عميل، فاتورة شراء خامات، إضافة عميل.

🆕 الشاشات دي كانت فورم الأدمن الكامل (خانات كتير، جداول عريضة، والتنفيذ على خطوتين — فاتورة
الشراء مثلاً بتتحفظ مسودة وبعدين «استلام» من صفحة تانية). هنا كل واحدة شاشة واحدة على الموبايل.

⚠️ ولا شاشة فيهم بتعمل قيد بنفسها — بتنادي نفس الطرق اللي السيستم بيقيّد بيها:
* التحصيل ⇒ ``PrintTransaction`` بـ ``full_clean`` (نفس «تحصيل من عميل» في الأدمن).
* الشراء ⇒ ``MaterialPurchase`` + بنود ⇒ ``receive()`` (المخزن ومتوسط التكلفة والخزنة).
* العميل ⇒ ``PrintCustomer`` بـ ``full_clean`` (الرصيد الافتتاحي بيتقيّد من الـ signal).
وكل واحدة في transaction واحدة: يا كله يتحفظ يا ولا حاجة.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from printing.quick_invoice import _dec

MAX_LINES = 50


def _qty(value, field):
    """كمية خامة (ممكن كسور: 2.5 رول) — أكبر من صفر."""
    q = _dec(value or '0', field)
    if q <= 0:
        raise ValidationError(_("%(f)s لازم تكون أكبر من صفر.") % {'f': field})
    return q


def open_orders(customer):
    """فواتير العميل اللي لسه عليها فلوس (الأقدم الأول)."""
    from printing.models import PrintOrder
    rows = []
    for o in (PrintOrder.objects.filter(customer=customer).exclude(status__in=PrintOrder.NON_DEBT_STATUSES)
              .order_by('date_created')):
        if o.remaining > 0:
            rows.append(o)
    return rows


# ─────────────────────────────────────────────────────────────────────
# 💵 تحصيل من عميل
# ─────────────────────────────────────────────────────────────────────
def collect_from_customer(*, user, customer_id, amount, treasury, order_id=None, withholding_tax='0',
                          description=''):
    """دفعة من عميل على فاتورة بعينها أو على حسابه. بيرجّع الحركة."""
    from printing.models import PrintCustomer, PrintOrder, PrintTransaction

    amount = _dec(amount, _("المبلغ"))
    withholding = _dec(withholding_tax, _("الخصم من المنبع"))
    if amount <= 0:
        raise ValidationError(_("اكتب المبلغ اللي العميل دفعه."))
    if treasury is None:
        raise ValidationError(_("اختار الخزنة اللي الفلوس دخلت فيها."))
    try:
        customer = PrintCustomer.objects.get(pk=int(customer_id))
    except (PrintCustomer.DoesNotExist, ValueError, TypeError):
        raise ValidationError(_("اختار العميل."))

    order = None
    if order_id:
        try:
            order = PrintOrder.objects.get(pk=int(order_id), customer=customer)
        except (PrintOrder.DoesNotExist, ValueError, TypeError):
            raise ValidationError(_("الفاتورة المختارة مش بتاعة العميل ده."))
        if order.status in PrintOrder.NON_DEBT_STATUSES:
            raise ValidationError(_("الفاتورة دي مسودة أو ملغية — مفيش عليها فلوس."))
        if amount + withholding > order.remaining:
            raise ValidationError(
                _("المبلغ أكبر من الباقي على الفاتورة (%(r)s). لو دافع زيادة اختار «على الحساب».")
                % {'r': f"{order.remaining:,.2f}"})

    with transaction.atomic():
        txn = PrintTransaction(
            treasury=treasury, transaction_type='in', category='customer', amount=amount,
            withholding_tax=withholding, order=order, customer=customer, created_by=user,
            description=(description or '').strip()[:300] or (
                _("تحصيل فاتورة #%(n)s") % {'n': order.order_number} if order
                else _("دفعة على الحساب — %(c)s") % {'c': customer.name}),
        )
        txn.full_clean()
        txn.save()
    return txn


# ─────────────────────────────────────────────────────────────────────
# 🛒 فاتورة شراء خامات
# ─────────────────────────────────────────────────────────────────────
def resolve_supplier(*, supplier_id=None, name='', phone=''):
    from printing.models import PrintSupplier
    if supplier_id:
        try:
            return PrintSupplier.objects.get(pk=int(supplier_id)), False
        except (PrintSupplier.DoesNotExist, ValueError, TypeError):
            raise ValidationError(_("المورد المختار مش موجود."))
    name, phone = (name or '').strip(), (phone or '').strip()
    if phone:
        found = PrintSupplier.objects.filter(phone=phone).first()
        if found:
            return found, False
    if not name:
        raise ValidationError(_("اكتب اسم المورد (أو اختار مورد مسجّل)."))
    same = list(PrintSupplier.objects.filter(name__iexact=name)[:2])
    if len(same) == 1:
        return same[0], False
    return PrintSupplier.objects.create(name=name[:150], phone=phone[:20], whatsapp=phone[:20]), True


def clean_purchase_lines(raw_lines):
    """[(material_id، اسم خامة جديدة، الوحدة، الكمية، تكلفة الوحدة)] ⇒ بنود نضيفة."""
    from printing.models import PrintMaterial
    out = []
    for i, (mid, name, unit, qty, cost) in enumerate(raw_lines, start=1):
        name, unit = (name or '').strip(), (unit or '').strip()
        if not mid and not name and not (qty or '').strip() and not (cost or '').strip():
            continue
        material = None
        if mid:
            try:
                material = PrintMaterial.objects.get(pk=int(mid))
            except (PrintMaterial.DoesNotExist, ValueError, TypeError):
                raise ValidationError(_("البند %(n)s: الخامة المختارة مش موجودة.") % {'n': i})
        elif not name:
            raise ValidationError(_("البند %(n)s: اختار الخامة أو اكتب اسمها.") % {'n': i})
        q = _qty(qty, _("كمية البند %(n)s") % {'n': i})
        c = _dec(cost, _("سعر البند %(n)s") % {'n': i})
        out.append((material, name[:200], unit[:30], q, c))
    if not out:
        raise ValidationError(_("ضيف خامة واحدة على الأقل."))
    if len(out) > MAX_LINES:
        raise ValidationError(_("أقصى عدد بنود %(n)s.") % {'n': MAX_LINES})
    return out


def create_quick_purchase(*, user, lines, supplier_id=None, supplier_name='', supplier_phone='',
                          payment_terms='cash', treasury=None, receive=True, discount='0', tax='0',
                          supplier_invoice_no='', notes=''):
    """فاتورة شراء + بنودها، ولو ``receive`` بتتستلم (المخزن والخزنة) في نفس العملية.

    بيرجّع (purchase, warnings).
    """
    from printing.models import MaterialPurchase, MaterialPurchaseLine, PrintMaterial

    lines = clean_purchase_lines(lines)
    discount, tax = _dec(discount, _("الخصم")), _dec(tax, _("الضريبة"))
    if payment_terms not in ('cash', 'credit'):
        raise ValidationError(_("اختار طريقة الدفع: كاش ولا آجل."))
    subtotal = sum(((q * c).quantize(Decimal('0.01')) for _m, _n, _u, q, c in lines), Decimal('0'))
    if subtotal <= 0:
        raise ValidationError(_("إجمالي الفاتورة صفر — اكتب سعر الخامات."))
    if discount > subtotal:
        raise ValidationError(_("الخصم أكبر من إجمالي البنود."))
    if payment_terms == 'cash' and treasury is None:
        raise ValidationError(_("اختار الخزنة اللي هيتدفع منها (أو خلّي الدفع آجل)."))

    warnings = []
    with transaction.atomic():
        supplier, created = resolve_supplier(supplier_id=supplier_id, name=supplier_name, phone=supplier_phone)
        if created:
            warnings.append(_("اتسجّل مورد جديد: %(n)s") % {'n': supplier.name})
        purchase = MaterialPurchase(
            supplier=supplier, payment_terms=payment_terms, discount=discount, tax_amount=tax,
            treasury=treasury if payment_terms == 'cash' else None,
            supplier_invoice_no=(supplier_invoice_no or '').strip()[:50], notes=(notes or '').strip(),
            created_by=user,
        )
        purchase.full_clean(exclude=['number'])
        purchase.save()
        for material, name, unit, q, c in lines:
            if material is None:
                material = PrintMaterial.objects.filter(name__iexact=name).first()
                if material is None:
                    # خامة جديدة: بتدخل المخزن بكمية صفر — الاستلام هو اللي بيزوّدها بتكلفتها
                    material = PrintMaterial.objects.create(name=name, unit=unit or _('قطعة'), category='other',
                                                            quantity=Decimal('0'), cost_per_unit=Decimal('0'))
                    warnings.append(_("اتضافت خامة جديدة: %(n)s") % {'n': material.name})
            MaterialPurchaseLine.objects.create(purchase=purchase, material=material, quantity=q, unit_cost=c)
        purchase.refresh_from_db()
        if receive:
            purchase.receive(user=user)          # بيرفع ValidationError لو الخزنة مش كفاية ⇒ كله يرجع
        purchase.refresh_from_db()
    return purchase, warnings


# ─────────────────────────────────────────────────────────────────────
# 👤 إضافة عميل
# ─────────────────────────────────────────────────────────────────────
def create_quick_customer(*, name, phone='', whatsapp='', company='', opening_balance='0',
                          opening_direction='owes', opening_date=None, credit_limit='0', notes=''):
    """عميل جديد. ``opening_direction``: owes = عليه لينا (موجب)، credit = ليه رصيد عندنا (سالب)."""
    from printing.models import PrintCustomer

    name, phone = (name or '').strip(), (phone or '').strip()
    if not name:
        raise ValidationError(_("اكتب اسم العميل."))
    if phone:
        dup = PrintCustomer.objects.filter(phone=phone).first()
        if dup:
            raise ValidationError(_("فيه عميل متسجّل بنفس الرقم: %(n)s.") % {'n': dup.name})
    opening = _dec(opening_balance, _("الرصيد القديم"))
    if opening_direction == 'credit':
        opening = -opening
    with transaction.atomic():
        cust = PrintCustomer(
            name=name[:150], phone=phone[:20], whatsapp=((whatsapp or '').strip() or phone)[:20],
            company=(company or '').strip()[:150], notes=(notes or '').strip(),
            opening_balance=opening, opening_date=opening_date or timezone.localdate(),
            credit_limit=_dec(credit_limit, _("حد الائتمان")),
        )
        cust.full_clean()
        cust.save()
    return cust


# ─────────────────────────────────────────────────────────────────────
# 💸 مصروف / 🏭 سداد لمورد — حركات «طالعة» من الخزنة
# ─────────────────────────────────────────────────────────────────────
EXPENSE_CATEGORIES = ('rent', 'utilities', 'maintenance', 'shipping', 'marketing', 'salaries', 'materials',
                      'other_expense')     # نفس بنود شاشة «مصروف» (TXN_MODES['expense'])


def _save_out(txn):
    """حركة طالعة: نفس فحص الأدمن (الرصيد يكفي؟) كرسالة واضحة بدل ValueError من الموديل."""
    txn.full_clean()
    available = txn.treasury.balance
    if txn.amount > available:
        raise ValidationError(_("رصيد الخزنة «%(name)s» لا يكفي: المتاح %(available)s.")
                              % {'name': txn.treasury.name, 'available': f"{available:,.2f}"})
    try:
        txn.save()
    except ValueError:      # سباق: حد صرف من نفس الخزنة في نفس اللحظة
        raise ValidationError(_("رصيد الخزنة «%(name)s» لا يكفي.") % {'name': txn.treasury.name})
    return txn


def record_expense(*, user, amount, category, treasury, description='', date=None):
    from printing.models import PrintTransaction
    amount = _dec(amount, _("المبلغ"))
    if amount <= 0:
        raise ValidationError(_("اكتب مبلغ المصروف."))
    if category not in EXPENSE_CATEGORIES:
        raise ValidationError(_("اختار بند المصروف (إيجار، كهرباء…)."))
    if treasury is None:
        raise ValidationError(_("اختار الخزنة اللي الفلوس طلعت منها."))
    with transaction.atomic():
        txn = PrintTransaction(treasury=treasury, transaction_type='out', category=category, amount=amount,
                               description=(description or '').strip()[:300], created_by=user)
        if date:
            txn.date = date
        return _save_out(txn)


def open_supplier_docs(supplier):
    """فواتير المورد اللي لسه علينا فلوس فيها: شراء خامات مستلمة + فواتير مصروفات (الأقدم الأول)."""
    from printing.models import MaterialPurchase, PrintExpenseBill
    docs = []
    for p in MaterialPurchase.objects.filter(supplier=supplier, status='received').order_by('date', 'pk'):
        if p.remaining > 0:
            docs.append(('purchase', p))
    for b in PrintExpenseBill.objects.filter(supplier=supplier, is_cancelled=False).order_by('date', 'pk'):
        if b.remaining > 0:
            docs.append(('bill', b))
    return docs


def pay_supplier(*, user, supplier_id, amount, treasury, doc='', withholding_tax='0', description=''):
    """سداد لمورد على فاتورة شراء (``purchase:<id>``) أو فاتورة مصروف (``bill:<id>``) أو على حسابه."""
    from printing.models import MaterialPurchase, PrintExpenseBill, PrintSupplier, PrintTransaction

    amount = _dec(amount, _("المبلغ"))
    withholding = _dec(withholding_tax, _("الخصم من المنبع"))
    if amount <= 0:
        raise ValidationError(_("اكتب المبلغ اللي اتدفع للمورد."))
    if treasury is None:
        raise ValidationError(_("اختار الخزنة اللي الفلوس طلعت منها."))
    try:
        supplier = PrintSupplier.objects.get(pk=int(supplier_id))
    except (PrintSupplier.DoesNotExist, ValueError, TypeError):
        raise ValidationError(_("اختار المورد."))

    purchase = bill = None
    kind, _sep, pk = (doc or '').partition(':')
    if kind in ('purchase', 'bill'):
        model = MaterialPurchase if kind == 'purchase' else PrintExpenseBill
        try:
            obj = model.objects.get(pk=int(pk), supplier=supplier)
        except (model.DoesNotExist, ValueError, TypeError):
            raise ValidationError(_("الفاتورة المختارة مش بتاعة المورد ده."))
        if (kind == 'purchase' and obj.status != 'received') or (kind == 'bill' and obj.is_cancelled):
            raise ValidationError(_("الفاتورة دي مش مستلمة أو ملغية."))
        if amount + withholding > obj.remaining:
            raise ValidationError(
                _("المبلغ أكبر من الباقي على الفاتورة (%(r)s). لو دافع مقدّم اختار «على الحساب».")
                % {'r': f"{obj.remaining:,.2f}"})
        purchase, bill = (obj, None) if kind == 'purchase' else (None, obj)

    with transaction.atomic():
        number = purchase.number if purchase else (bill.number if bill else '')
        txn = PrintTransaction(
            treasury=treasury, transaction_type='out', category='supplier', amount=amount,
            withholding_tax=withholding, supplier=supplier, purchase=purchase, bill=bill, created_by=user,
            description=(description or '').strip()[:300] or (
                _("سداد فاتورة #%(n)s — %(s)s") % {'n': number, 's': supplier.name} if number
                else _("دفعة على الحساب — %(s)s") % {'s': supplier.name}),
        )
        return _save_out(txn)
