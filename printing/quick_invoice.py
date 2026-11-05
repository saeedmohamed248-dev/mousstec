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


def resolve_customer(*, customer_id=None, name='', phone=''):
    """عميل مسجّل بالـ id، أو بالموبايل، أو عميل جديد بالاسم والموبايل (زي تحويل عرض السعر)."""
    from printing.models import PrintCustomer
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


def create_quick_invoice(*, user, lines, customer_id=None, customer_name='', customer_phone='',
                         discount='0', delivered=True, paid='0', treasury=None, notes=''):
    """بيعمل الفاتورة كلها في transaction واحدة — يا كله يتحفظ يا ولا حاجة.

    بيرجّع (order, warnings).
    """
    from printing.models import PrintJob, PrintOrder, PrintTransaction

    lines = clean_lines(lines)
    discount = _dec(discount, _("الخصم"))
    paid = _dec(paid, _("المدفوع"))
    subtotal = sum((q * p for _d, q, p in lines), Decimal('0'))
    if subtotal <= 0:
        raise ValidationError(_("إجمالي الفاتورة صفر — اكتب سعر البنود."))
    if discount > subtotal:
        raise ValidationError(_("الخصم أكبر من إجمالي البنود."))
    if paid > subtotal - discount:
        raise ValidationError(_("المدفوع أكبر من قيمة الفاتورة. لو العميل دافع زيادة سجّلها من «تحصيل من عميل»."))
    if paid > 0 and treasury is None:
        raise ValidationError(_("اختار الخزنة اللي الفلوس دخلت فيها."))

    warnings = []
    with transaction.atomic():
        customer, created = resolve_customer(customer_id=customer_id, name=customer_name, phone=customer_phone)
        order = PrintOrder.objects.create(customer=customer, discount=discount, status='confirmed',
                                          notes=(notes or '').strip())
        for desc, qty, price in lines:
            # proof_waived: الفاتورة السريعة لشغل متفق عليه — مفيش بروفة تستنى موافقة العميل
            PrintJob.objects.create(order=order, description=desc, quantity=qty, copies=1,
                                    unit_price=price, proof_waived=True)
        order.recalc_total_from_jobs()
        order.refresh_from_db()

        held = order.apply_credit_hold(previous_status=None)
        if held:
            warnings.append(held)
        elif delivered:
            deliver_order(order)

        if paid > 0:
            txn = PrintTransaction(
                treasury=treasury, transaction_type='in', category='customer', amount=paid,
                order=order, customer=customer, created_by=user,
                description=_("دفعة على فاتورة #%(n)s") % {'n': order.order_number},
            )
            txn.full_clean()
            txn.save()
        order.refresh_from_db()
    if created:
        warnings.insert(0, _("اتسجّل عميل جديد: %(n)s") % {'n': customer.name})
    return order, warnings
