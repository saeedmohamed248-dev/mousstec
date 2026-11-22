"""⚡ الفاتورة السريعة — فاتورة بيع لعميل في شاشة واحدة (زي Square/Zoho على الموبايل).

🆕 الفاتورة كانت فورم الأدمن الكامل: 4 تابات + جدول مهام 12 عمود (ماكينة، مقاس، مرحلة، ملف…)
+ جدول دفعات 6 أعمدة، والطلب بيتعمل «مسودة» والتحصيل من شاشة تانية. على الموبايل صاحب المطبعة
مابيعرفش يعمل فاتورة كارت بزنس في دقيقة.

هنا: العميل (مسجّل أو اسم + موبايل)، البنود (وصف × كمية × سعر)، خصم، «اتسلّمت؟»، والمدفوع دلوقتي.
بنستخدم نفس الطرق اللي السيستم بيقيّد بيها (زي ``convert_quotation`` و«تحصيل من عميل»):
طلب مؤكد ⇒ مهام ⇒ حد الائتمان ⇒ تسليم ⇒ دفعة ``category='customer'`` بـ full_clean —
فالدفاتر والخزنة والمخزون بيتحركوا بالظبط زي الفاتورة المفصّلة. مفيش قيد جديد.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

MAX_LINES = 50


def _dec(value, field):
    try:
        d = Decimal(str(value if value not in (None, '') else '0').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        raise ValidationError(_("%(f)s: رقم مش مظبوط.") % {'f': field})
    if not d.is_finite() or d < 0:
        raise ValidationError(_("%(f)s لازم يكون صفر أو أكتر.") % {'f': field})
    return d.quantize(Decimal('0.01'))


def clean_lines(raw_lines):
    """[(وصف، كمية، سعر)] من الفورم ⇒ بنود نضيفة. السطور الفاضية بتتشال."""
    lines = []
    for i, (desc, qty, price) in enumerate(raw_lines, start=1):
        desc = (desc or '').strip()
        if not desc and not (price or '').strip():
            continue
        if not desc:
            raise ValidationError(_("البند %(n)s: اكتب وصفه (مثال: 500 كارت بزنس).") % {'n': i})
        try:
            q = int(str(qty or '1').strip() or '1')
        except ValueError:
            raise ValidationError(_("البند %(n)s: الكمية لازم تكون رقم صحيح.") % {'n': i})
        if q < 1:
            raise ValidationError(_("البند %(n)s: الكمية لازم تكون 1 أو أكتر.") % {'n': i})
        lines.append((desc[:300], q, _dec(price, _("سعر البند %(n)s") % {'n': i})))
    if not lines:
        raise ValidationError(_("ضيف بند واحد على الأقل."))
    if len(lines) > MAX_LINES:
        raise ValidationError(_("أقصى عدد بنود %(n)s.") % {'n': MAX_LINES})
    return lines


MAX_MATERIALS = 30


def clean_materials(raw):
    """🧾 [(id الخامة، الكمية)] من الفورم ⇒ [(PrintMaterial, كمية)] — نفس الخامة مرتين بتتجمع.

    الفاتورة السريعة مكانش فيها خامات: الورق والحبر اللي اتصرفوا على الشغل ماكانوش بيتخصموا من
    المخزن ولا بيدخلوا التكلفة، فربح كل فاتورة كان طالع ١٠٠٪. السطور الفاضية بتتشال، والكمية لازم
    تكون موجودة في المخزن (نفس رسالة PrintJobMaterial — والتأكيد النهائي جوه الـ transaction).
    """
    from printing.models import PrintMaterial
    wanted = {}
    for i, (mid, qty) in enumerate(raw, start=1):
        mid, qty = str(mid or '').strip(), str(qty or '').strip()
        if not mid and not qty:
            continue
        if not mid:
            raise ValidationError(_("الخامة %(n)s: اختار الخامة.") % {'n': i})
        q = _dec(qty, _("كمية الخامة %(n)s") % {'n': i})
        if q <= 0:
            raise ValidationError(_("الخامة %(n)s: اكتب الكمية اللي اتصرفت.") % {'n': i})
        wanted[mid] = wanted.get(mid, Decimal('0')) + q
    if len(wanted) > MAX_MATERIALS:
        raise ValidationError(_("أقصى عدد خامات %(n)s.") % {'n': MAX_MATERIALS})
    found = {str(m.pk): m for m in PrintMaterial.objects.filter(pk__in=[k for k in wanted if k.isdigit()])}
    out = []
    for mid, q in wanted.items():
        mat = found.get(mid)
        if mat is None:
            raise ValidationError(_("الخامة المختارة مش موجودة."))
        if mat.quantity < q:
            raise ValidationError(
                _("المخزون من «%(name)s» مش كفاية: المتاح %(have)s %(unit)s، المطلوب %(need)s.")
                % {'name': mat.name, 'have': mat.quantity, 'unit': mat.unit, 'need': q})
        out.append((mat, q))
    return out


#: 💵 الأسامي اللي الموظف بيكتبها لما يقصد «بيع كاش لعميل عابر» (مش عميل اسمه كده)
CASH_ALIASES = frozenset({
    'نقدي', 'نقدى', 'نقداً', 'نقدا', 'كاش', 'عميل نقدي', 'عميل نقدى', 'عميل كاش', 'عميل عابر',
    'cash', 'cash customer', 'walk-in', 'walk in', 'walkin',
})
CASH_CUSTOMER_NAME = 'عميل نقدي'


def is_cash_name(name):
    return ' '.join((name or '').split()).lower() in CASH_ALIASES


def cash_customer():
    """العميل النقدي المشترك للمطبعة — واحد بس (لو اتعمل قبل كده باسم «نقدي» أو «كاش» بنستخدمه)."""
    from printing.models import PrintCustomer
    for c in PrintCustomer.objects.filter(phone='').order_by('pk'):
        if is_cash_name(c.name):
            return c
    return PrintCustomer.objects.create(name=CASH_CUSTOMER_NAME, phone='', whatsapp='')


def resolve_customer(*, customer_id=None, name='', phone=''):
    """عميل مسجّل بالـ id، أو بالموبايل، أو عميل جديد بالاسم والموبايل (زي تحويل عرض السعر).

    🐛 [FIX]: مطبعة كتبت «نقدي» في خانة العميل (يعني بيع كاش) ⇒ السيستم عمل عميل جديد اسمه «نقدي»
    وطلّع تحذير «اتسجّل عميل جديد: نقدي». دلوقتي «نقدي/كاش/cash» من غير موبايل = العميل النقدي المشترك.
    """
    from printing.models import PrintCustomer
    if not customer_id and not (phone or '').strip() and is_cash_name(name):
        return cash_customer(), False
    if customer_id:
        try:
            return PrintCustomer.objects.get(pk=int(customer_id)), False
        except (PrintCustomer.DoesNotExist, ValueError, TypeError):
            raise ValidationError(_("العميل المختار مش موجود."))
    name, phone = (name or '').strip(), (phone or '').strip()
    if phone:
        found = PrintCustomer.objects.filter(phone=phone).first()
        if found:
            return found, False
    if not name:
        raise ValidationError(_("اكتب اسم العميل (أو اختار عميل مسجّل)."))
    if not phone:
        # كتب اسم عميل موجود من غير ما يختاره من القايمة — مانعملش نسخة تانية منه
        same = list(PrintCustomer.objects.filter(name__iexact=name)[:2])
        if len(same) == 1:
            return same[0], False
    return PrintCustomer.objects.create(name=name[:150], phone=phone[:20], whatsapp=phone[:20]), True


def deliver_order(order):
    """✅ «اتسلّمت»: البنود تخلص (تكلفتها تتثبت وتطلع من لوحة الإنتاج) والطلب يبقى «تم التسليم».

    🐛 [FIX]: الفاتورة السريعة كانت بتتسلّم والبنود فاضلة «تصميم / مش مكتملة» — فبتفضل في لوحة
    الإنتاج كشغل جاري، والأدمن بيحذّر «فيه مهام لسه مش مكتملة». نفس اللي بيحصل لما تعلّم المهام
    «مكتملة» وتغيّر الحالة من الأدمن. البند اللي مستني موافقة بروفة مابيتقفلش من هنا.
    """
    from printing.models import PrintOrder
    if order.status not in ('confirmed', 'in_progress', 'ready'):
        raise ValidationError(_("الفاتورة دي مش في حالة تتسلّم (مسودة أو ملغية أو اتسلّمت قبل كده)."))
    jobs = list(order.jobs.filter(is_complete=False))
    waiting = [j for j in jobs if not j.can_go_to_production]
    if waiting:
        raise ValidationError(_("«%(job)s» مستني موافقة العميل على البروفة — مينفعش تتسلّم قبلها.")
                              % {'job': waiting[0].description[:60]})
    with transaction.atomic():
        for job in jobs:
            job.is_complete = True
            job.save()
        order = PrintOrder.objects.get(pk=order.pk)
        order.status = 'delivered'
        order.save()
    return order


def parse_invoice_date(value):
    """📅 تاريخ الفاتورة من الشاشة (YYYY-MM-DD) — فاضي/النهارده ⇒ None (يعني دلوقتي).
    مينفعش تاريخ في المستقبل ولا جوه فترة محاسبية مقفولة."""
    from datetime import date as _date, datetime as _dt, time as _time
    from django.utils import timezone
    value = (value or '').strip()
    if not value:
        return None
    try:
        day = _date.fromisoformat(value)
    except ValueError:
        raise ValidationError(_("تاريخ الفاتورة غير صحيح."))
    today = timezone.localdate()
    if day > today:
        raise ValidationError(_("تاريخ الفاتورة مينفعش يبقى في المستقبل."))
    if day == today:
        return None
    from inventory.models import AccountingPeriod
    period = AccountingPeriod.for_date(day)
    if period is not None and period.is_closed:
        raise ValidationError(_("مينفعش تحط الفاتورة في فترة مقفولة: %(p)s.") % {'p': period})
    # نص النهار — عشان فرق التوقيت مايرجّعهاش يوم
    return timezone.make_aware(_dt.combine(day, _time(12, 0)), timezone.get_current_timezone())


FILE_FIELDS = ('project_file', 'project_file_2', 'project_file_3')
MAX_FILE_MB = 50


def parse_due_date(value):
    """⏰ موعد التسليم (YYYY-MM-DD) للشغل اللي لسه ماتسلّمش — آخر اليوم (٦ مساءً)."""
    from datetime import date as _date, datetime as _dt, time as _time
    from django.utils import timezone
    value = (value or '').strip()
    if not value:
        return None
    try:
        day = _date.fromisoformat(value)
    except ValueError:
        raise ValidationError(_("موعد التسليم غير صحيح."))
    if day < timezone.localdate():
        raise ValidationError(_("موعد التسليم مينفعش يبقى فات."))
    return timezone.make_aware(_dt.combine(day, _time(18, 0)), timezone.get_current_timezone())


def create_quick_invoice(*, user, lines, customer_id=None, customer_name='', customer_phone='',
                         discount='0', delivered=True, paid='0', treasury=None, notes='',
                         invoice_date=None, tax_rate='0', due_date=None, files=(), materials=()):
    """بيعمل الفاتورة كلها في transaction واحدة — يا كله يتحفظ يا ولا حاجة.

    materials: [(id الخامة، الكمية)] اللي اتصرفت على الشغل — بتتخصم من المخزن وتدخل تكلفة الفاتورة
    (بتتسجّل على أول بند؛ الربح والتكلفة بيتحسبوا على الفاتورة كلها).

    بيرجّع (order, warnings).
    """
    from printing.models import PrintJob, PrintJobMaterial, PrintOrder, PrintTransaction

    lines = clean_lines(lines)
    used = clean_materials(materials)
    discount = _dec(discount, _("الخصم"))
    paid = _dec(paid, _("المدفوع"))
    # 🧾 الضريبة نسبة (١٤٪ عادةً) بدل ما الموظف يحسب المبلغ بإيده — بتتحسب على الصافي بعد الخصم
    rate = _dec(tax_rate, _("نسبة الضريبة"))
    if rate > 100:
        raise ValidationError(_("نسبة الضريبة لازم تبقى بين ٠ و ١٠٠."))
    subtotal = sum((q * p for _d, q, p in lines), Decimal('0'))
    if subtotal <= 0:
        raise ValidationError(_("إجمالي الفاتورة صفر — اكتب سعر البنود."))
    if discount > subtotal:
        raise ValidationError(_("الخصم أكبر من إجمالي البنود."))
    tax = ((subtotal - discount) * rate / 100).quantize(Decimal('0.01'))
    net = subtotal - discount + tax
    due = None if delivered else parse_due_date(due_date)
    files = [f for f in (files or ()) if f]
    if len(files) > len(FILE_FIELDS):
        raise ValidationError(_("أقصى عدد ملفات %(n)s.") % {'n': len(FILE_FIELDS)})
    for f in files:
        if f.size > MAX_FILE_MB * 1024 * 1024:
            raise ValidationError(_("الملف «%(f)s» أكبر من %(m)s ميجا.") % {'f': f.name, 'm': MAX_FILE_MB})
    if paid > net:
        raise ValidationError(_("المدفوع أكبر من قيمة الفاتورة. لو العميل دافع زيادة سجّلها من «تحصيل من عميل»."))
    if paid > 0 and treasury is None:
        raise ValidationError(_("اختار الخزنة اللي الفلوس دخلت فيها."))
    when = parse_invoice_date(invoice_date)   # None = دلوقتي
    # 💎 حد طلبات الطباعة الشهري في الباقة (Lite = 100)
    from tenancy.services.plan_limits import print_orders_block
    blocked = print_orders_block()
    if blocked:
        raise ValidationError(blocked)

    warnings = []
    with transaction.atomic():
        customer, created = resolve_customer(customer_id=customer_id, name=customer_name, phone=customer_phone)
        # 🐛 [FIX]: العميل النقدي مشترك بين ناس كتير — باقي عليه = دين مالوش صاحب في كشف الحساب.
        if not customer.phone and is_cash_name(customer.name) and paid < net:
            raise ValidationError(_("العميل النقدي لازم يدفع الفاتورة كاملة. لو هيدفع الباقي بعدين اكتب اسمه وموبايله."))
        extra = {'date_created': when} if when else {}
        if due:
            extra['date_due'] = due
        order = PrintOrder(customer=customer, discount=discount, tax_amount=tax, status='confirmed',
                           notes=(notes or '').strip(), **extra)
        for name, f in zip(FILE_FIELDS, files):
            setattr(order, name, f)
        order.save()
        jobs = [
            # proof_waived: الفاتورة السريعة لشغل متفق عليه — مفيش بروفة تستنى موافقة العميل
            PrintJob.objects.create(order=order, description=desc, quantity=qty, copies=1,
                                    unit_price=price, proof_waived=True)
            for desc, qty, price in lines
        ]
        # 🧾 الخامات قبل التسليم: بتتخصم من المخزن (تحت شغل جاري) وتتحوّل تكلفة لما الفاتورة تتسلّم
        for mat, qty in used:
            PrintJobMaterial.objects.create(job=jobs[0], material=mat, quantity=qty)
        order.recalc_total_from_jobs()
        order.refresh_from_db()

        held = order.apply_credit_hold(previous_status=None)
        if held:
            warnings.append(held)
        elif delivered:
            order = deliver_order(order)
            if when:
                # فاتورة قديمة: اتسلّمت يومها ⇒ الإيراد بتاريخها (القيد بيتنقل في ledger.sync)
                order.date_delivered = when
                order.save(update_fields=['date_delivered'])

        if paid > 0:
            txn = PrintTransaction(
                treasury=treasury, transaction_type='in', category='customer', amount=paid,
                order=order, customer=customer, created_by=user,
                description=_("دفعة على فاتورة #%(n)s") % {'n': order.order_number},
            )
            if when:
                txn.date = when     # الفلوس اتدفعت يوم الفاتورة نفسها
            txn.full_clean()
            txn.save()
        order.refresh_from_db()
    if created:
        warnings.insert(0, _("اتسجّل عميل جديد: %(n)s") % {'n': customer.name})
    return order, warnings
