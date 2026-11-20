"""🚚 المطابع — الموردين والمشتريات ومرتجعاتها وسداد المورد من الرصيد المقدّم."""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator
from ._common import _save_with_number
from .customers import PrintBranch
from .materials import PrintMaterial
from .treasury import PrintTransaction, PrintTreasury


# =====================================================================
# 🚚 9. الموردين ومشتريات الخامات (Suppliers & Material Purchases)
# =====================================================================

class PrintSupplier(models.Model):
    """مورد خامات (ورق، أحبار، فينيل…) — رصيده = فواتير الشراء المستلمة − اللي اتدفعله."""
    name = models.CharField(max_length=150, verbose_name=_("اسم المورد"))
    company = models.CharField(max_length=150, blank=True, verbose_name=_("اسم الشركة"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("الهاتف"))
    whatsapp = models.CharField(max_length=20, blank=True, verbose_name=_("واتساب"))
    address = models.TextField(blank=True, verbose_name=_("العنوان"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    opening_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name=_("رصيد افتتاحي"),
        help_text=_("المستحق قبل ما تبدأ على السيستم: موجب = علينا للمورد، سالب = لينا رصيد عنده."))
    opening_date = models.DateField(default=timezone.localdate, verbose_name=_("تاريخ الرصيد الافتتاحي"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("مورد")
        verbose_name_plural = _("الموردين")
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.company})" if self.company else self.name

    def payments_qs(self):
        """كل الفلوس مع المورد: على فواتيره + فواتير مصروفاته + على حسابه (دفعة مقدّمة/تسوية)."""
        return PrintTransaction.objects.filter(
            models.Q(purchase__supplier=self) | models.Q(bill__supplier=self)
            | models.Q(purchase__isnull=True, bill__isnull=True, supplier=self))

    def account_summary(self):
        """
        المستحق للمورد − اللي اتدفعله = balance (موجب = علينا، سالب = لينا رصيد عنده).

        المستحق = الرصيد الافتتاحي + فواتير الخامات المستلمة − مرتجعاتها + فواتير
        المصروفات والخدمات + الأصول اللي اتشرت منه على الحساب.
        """
        zero = Decimal('0')
        purchased = self.opening_balance or zero
        purchased += sum((p.net_total for p in self.purchases.filter(status='received')), zero)
        purchased -= sum((r.total for r in MaterialPurchaseReturn.objects.filter(
            purchase__supplier=self, status='posted')), zero)
        purchased += sum((b.total for b in self.bills.filter(is_cancelled=False)), zero)
        purchased += sum((a.cost for a in self.fixed_assets.filter(funding='credit')), zero)
        agg = self.payments_qs().aggregate(
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')),
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')))
        paid = (agg['outs'] or zero) - (agg['ins'] or zero)
        return {'purchased': purchased, 'paid': paid, 'balance': purchased - paid}

    @property
    def balance(self):
        return self.account_summary()['balance']

    def available_credit(self):
        """
        رصيدنا المقدّم عند المورد اللي لسه ماتسدّدش بيه فاتورة: الدفعات − اللي اتسدّد
        على الفواتير (شامل التسديد من الرصيد) − المستحقات اللي مالهاش سداد مربوط
        (الرصيد الافتتاحي والأصول الآجلة — بتتسدّد من الدفعات على الحساب الأول).
        """
        zero = Decimal('0')
        untracked = max(zero, self.opening_balance or zero) + sum(
            (a.cost for a in self.fixed_assets.filter(funding='credit')), zero)
        return max(zero, self.unapplied_payments() - untracked)

    def unapplied_payments(self):
        """الدفعات (والرصيد الافتتاحي الدائن) اللي مش متسدّد بيها فاتورة شراء أو مصروف."""
        zero = Decimal('0')
        agg = self.payments_qs().aggregate(
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')),
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')))
        payments = (agg['outs'] or zero) - (agg['ins'] or zero) + max(zero, -(self.opening_balance or zero))
        applied = sum((max(zero, min(p.paid_amount, p.net_total - p.returned_amount))
                       for p in self.purchases.filter(status='received')), zero)
        applied += sum((max(zero, min(b.paid_amount, b.total)) for b in self.bills.filter(is_cancelled=False)), zero)
        return max(zero, payments - applied)


class MaterialPurchase(models.Model):
    """
    🧾 فاتورة شراء خامات من مورد.

    الاستلام (receive) بيزوّد كمية الخامات ويحدّث متوسط التكلفة، ولو الدفع
    «نقدي» الفلوس بتطلع من الخزنة لوحدها. الآجل بيفضل على حساب المورد لحد
    ما يتدفع (من صفحة الفاتورة أو المورد).
    """
    STATUS_CHOICES = (
        ('draft', _('مسودة')),
        ('received', _('اتستلمت — دخلت المخزن')),
        ('cancelled', _('ملغية')),
    )
    PAYMENT_CHOICES = (
        ('cash', _('نقدي — يتصرف من الخزنة وقت الاستلام')),
        ('credit', _('آجل — على حساب المورد')),
    )

    number = models.CharField(max_length=30, unique=True, blank=True, editable=False, verbose_name=_("رقم الفاتورة"))
    supplier = models.ForeignKey(PrintSupplier, on_delete=models.PROTECT, related_name='purchases', verbose_name=_("المورد"))
    supplier_invoice_no = models.CharField(max_length=50, blank=True, verbose_name=_("رقم فاتورة المورد"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))
    date = models.DateField(default=timezone.localdate, verbose_name=_("تاريخ الفاتورة"))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft', editable=False,
                              db_index=True, verbose_name=_("الحالة"))
    payment_terms = models.CharField(max_length=10, choices=PAYMENT_CHOICES, default='cash', verbose_name=_("طريقة الدفع"))
    treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, null=True, blank=True,
                                 verbose_name=_("الخزنة اللي هيتصرف منها"),
                                 help_text=_("مطلوبة في الدفع النقدي — المبلغ بيتخصم منها أول ما الفاتورة تتستلم."))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False,
                                       verbose_name=_("إجمالي البنود"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                   validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الخصم"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                     validators=[MinValueValidator(Decimal('0.00'))],
                                     verbose_name=_("ضريبة القيمة المضافة"),
                                     help_text=_("الضريبة المدفوعة للمورد — بتتقيّد ضريبة مدخلات (مش تكلفة خامات)."))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False,
                                      verbose_name=_("المدفوع"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))
    received_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("تاريخ الاستلام"))
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("تاريخ الإلغاء"))

    class Meta:
        verbose_name = _("فاتورة شراء خامات")
        verbose_name_plural = _("فواتير شراء الخامات")
        ordering = ['-date', '-id']

    def __str__(self):
        return f"#{self.number} — {self.supplier}"

    @property
    def inventory_value(self):
        """قيمة الخامات اللي دخلت المخزن = البنود − الخصم (الضريبة مش تكلفة)."""
        return self.total_amount - self.discount

    @property
    def net_total(self):
        """المستحق للمورد = البنود − الخصم + الضريبة."""
        return self.total_amount - self.discount + self.tax_amount

    @property
    def returned_amount(self):
        """قيمة المرتجعات المترحّلة على الفاتورة (بالضريبة)."""
        if not self.pk:
            return Decimal('0')
        agg = self.returns.filter(status='posted').aggregate(v=models.Sum('value'), t=models.Sum('tax_amount'))
        return (agg['v'] or Decimal('0')) + (agg['t'] or Decimal('0'))

    @property
    def remaining(self):
        # 🐛 [FIX]: المرتجع بينزل من المستحق — من غيره الفاتورة اللي اترجع منها جزء
        #    واتسدّدت بالباقي كانت بتفضل «عليها» قيمة المرتجع.
        return self.net_total - self.returned_amount - self.paid_amount

    def compute_paid(self):
        agg = PrintTransaction.objects.filter(purchase_id=self.pk).aggregate(
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')),
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')))
        allocated = (PrintSupplierAllocation.objects.filter(purchase_id=self.pk)
                     .aggregate(t=models.Sum('amount'))['t'] or Decimal('0'))
        return (agg['outs'] or Decimal('0')) - (agg['ins'] or Decimal('0')) + allocated

    def recalc_total(self):
        if not self.pk:
            return
        total = self.lines.aggregate(t=models.Sum('line_total'))['t'] or Decimal('0')
        if total != self.total_amount:
            self.total_amount = total
            MaterialPurchase.objects.filter(pk=self.pk).update(total_amount=total)

    def effective_line_totals(self, lines):
        """
        Each line's share of the inventory value after the invoice discount
        (by line value; the last line takes the rounding remainder).
        """
        total = sum((l.line_total for l in lines), Decimal('0'))
        remaining = self.inventory_value
        out = {}
        for i, line in enumerate(lines):
            if i == len(lines) - 1:
                share = remaining
            else:
                share = ((self.inventory_value * line.line_total / total).quantize(Decimal('0.01'))
                         if total else Decimal('0'))
            out[line.pk] = share
            remaining -= share
        return out

    def save(self, *args, **kwargs):
        if self.number:
            return super().save(*args, **kwargs)
        return _save_with_number(self, 'number', 'PUR-', super().save, *args, **kwargs)

    def receive(self, user=None):
        """
        📥 استلام الفاتورة: الخامات تزيد في المخزن بمتوسط تكلفة مرجّح، ولو الدفع نقدي
        المبلغ يتخصم من الخزنة. كله في عملية واحدة — لو الخزنة مافيهاش رصيد مفيش حاجة بتتغيّر.
        """
        from django.db import transaction
        with transaction.atomic():
            p = MaterialPurchase.objects.select_for_update().get(pk=self.pk)
            if p.status != 'draft':
                raise ValidationError(_("الفاتورة دي اتستلمت أو اتلغت قبل كده."))
            p.recalc_total()
            lines = list(p.lines.order_by('pk'))
            if not lines:
                raise ValidationError(_("أضف بند خامة واحد على الأقل قبل الاستلام."))
            if p.discount > p.total_amount:
                raise ValidationError(_("الخصم أكبر من إجمالي البنود."))
            if p.payment_terms == 'cash' and not p.treasury_id:
                raise ValidationError(_("اختار الخزنة اللي هيتصرف منها (أو خلّي الدفع آجل)."))

            effective = p.effective_line_totals(lines)
            for line in lines:
                mat = PrintMaterial.objects.select_for_update().get(pk=line.material_id)
                held = max(mat.quantity, Decimal('0'))
                new_qty = mat.quantity + line.quantity
                base_qty = held + line.quantity
                new_cost = ((held * mat.cost_per_unit + effective[line.pk]) / base_qty).quantize(Decimal('0.0001'))
                PrintMaterial.objects.filter(pk=mat.pk).update(quantity=new_qty, cost_per_unit=new_cost)

            p.status = 'received'
            p.received_at = timezone.now()
            p.save(update_fields=['status', 'received_at'])

            to_pay = p.net_total - p.compute_paid()
            if p.payment_terms == 'cash' and to_pay > 0:
                try:
                    PrintTransaction.objects.create(
                        treasury=p.treasury, transaction_type='out', amount=to_pay,
                        category='supplier', purchase=p, supplier=p.supplier,
                        description=f"سداد فاتورة شراء #{p.number} — {p.supplier.name}",
                        created_by=user,
                    )
                except ValueError:
                    raise ValidationError(
                        _("رصيد خزنة «%(t)s» مش كفاية لسداد %(a)s — خلّي الدفع آجل أو اختار خزنة تانية.")
                        % {'t': p.treasury.name, 'a': f'{to_pay:,.2f}'})
        self.refresh_from_db()
        return self

    def cancel(self, user=None):
        """
        ↩️ إلغاء الفاتورة. لو كانت مستلمة الخامات بترجع للمورد (بشرط تكون لسه في
        المخزن)، واللي اتدفع بيفضل رصيد لينا عند المورد لحد ما يرجّعه أو نخصمه.
        """
        from django.db import transaction
        with transaction.atomic():
            p = MaterialPurchase.objects.select_for_update().get(pk=self.pk)
            if p.status == 'cancelled':
                raise ValidationError(_("الفاتورة ملغية بالفعل."))
            if p.returns.filter(status='posted').exists():
                raise ValidationError(_("الفاتورة عليها مرتجعات — اعمل مرتجع بالكمية الباقية بدل الإلغاء."))
            if p.status == 'received':
                lines = list(p.lines.order_by('pk'))
                effective = p.effective_line_totals(lines)
                for line in lines:
                    mat = PrintMaterial.objects.select_for_update().get(pk=line.material_id)
                    if mat.quantity < line.quantity:
                        raise ValidationError(
                            _("«%(m)s»: المتاح %(q)s أقل من كمية الفاتورة %(n)s — اتصرف منها على شغل. "
                              "اعمل «مرتجع مشتريات» بالكمية الموجودة بدل الإلغاء.")
                            % {'m': mat.name, 'q': mat.quantity, 'n': line.quantity})
                    left_qty = mat.quantity - line.quantity
                    left_value = mat.quantity * mat.cost_per_unit - effective[line.pk]
                    new_cost = (max(left_value, Decimal('0')) / left_qty).quantize(Decimal('0.0001')) \
                        if left_qty > 0 else mat.cost_per_unit
                    PrintMaterial.objects.filter(pk=mat.pk).update(quantity=left_qty, cost_per_unit=new_cost)
            p.status = 'cancelled'
            p.cancelled_at = timezone.now()
            p.save(update_fields=['status', 'cancelled_at'])
        self.refresh_from_db()
        return self


class MaterialPurchaseLine(models.Model):
    """بند في فاتورة الشراء: خامة × كمية × سعر الوحدة."""
    purchase = models.ForeignKey(MaterialPurchase, on_delete=models.CASCADE, related_name='lines',
                                 verbose_name=_("الفاتورة"))
    material = models.ForeignKey(PrintMaterial, on_delete=models.PROTECT, related_name='purchase_lines',
                                 verbose_name=_("الخامة"))
    quantity = models.DecimalField(max_digits=12, decimal_places=2,
                                   validators=[MinValueValidator(Decimal('0.01'))], verbose_name=_("الكمية"))
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4,
                                    validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("سعر الوحدة"))
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=0, editable=False,
                                     verbose_name=_("الإجمالي"))

    class Meta:
        verbose_name = _("بند شراء")
        verbose_name_plural = _("بنود فاتورة الشراء")

    def __str__(self):
        return f"{self.material} × {self.quantity}"

    def save(self, *args, **kwargs):
        status = MaterialPurchase.objects.filter(pk=self.purchase_id).values_list('status', flat=True).first()
        if status and status != 'draft':
            raise ValidationError(_("بنود الفاتورة مابتتعدّلش بعد الاستلام أو الإلغاء."))
        self.line_total = ((self.quantity or Decimal('0')) * (self.unit_cost or Decimal('0'))).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)
        self.purchase.recalc_total()


# =====================================================================
# 📤 17. مرتجع مشتريات (جزئي) للمورد
# =====================================================================

class MaterialPurchaseReturn(models.Model):
    """
    مرتجع جزء من فاتورة شراء مستلمة — الخامات بتخرج من المخزن بتكلفتها في الفاتورة،
    والمبلغ بينزل من المستحق للمورد (أو بيرجع نقدي لو اخترت خزنة).
    """
    STATUS_CHOICES = (
        ('draft', _('مسودة')),
        ('posted', _('اترحّل — الخامات خرجت')),
    )
    number = models.CharField(max_length=30, unique=True, blank=True, editable=False, verbose_name=_("رقم المرتجع"))
    purchase = models.ForeignKey(MaterialPurchase, on_delete=models.PROTECT, related_name='returns',
                                 verbose_name=_("فاتورة الشراء"))
    date = models.DateField(default=timezone.localdate, verbose_name=_("التاريخ"))
    reason = models.CharField(max_length=300, blank=True, verbose_name=_("السبب"))
    refund_treasury = models.ForeignKey(
        PrintTreasury, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
        verbose_name=_("المورد رجّع الفلوس في خزنة"),
        help_text=_("سيبها فاضية لو المرتجع هيتخصم من المستحق للمورد."))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft', editable=False,
                              verbose_name=_("الحالة"))
    value = models.DecimalField(max_digits=14, decimal_places=2, default=0, editable=False,
                                verbose_name=_("قيمة الخامات المرتجعة"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False,
                                     verbose_name=_("الضريبة المرتجعة"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))
    posted_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("تاريخ الترحيل"))

    class Meta:
        verbose_name = _("مرتجع مشتريات")
        verbose_name_plural = _("مرتجعات المشتريات")
        ordering = ['-date', '-id']

    def __str__(self):
        return f"#{self.number} — {self.purchase}"

    @property
    def total(self):
        return self.value + self.tax_amount

    def save(self, *args, **kwargs):
        if self.number:
            return super().save(*args, **kwargs)
        return _save_with_number(self, 'number', 'PRT-', super().save, *args, **kwargs)

    def post(self, user=None):
        """ترحيل المرتجع: الكميات تخرج من المخزن بتكلفة الفاتورة، وقيمتها تنزل من حساب المورد."""
        from django.db import transaction
        with transaction.atomic():
            ret = MaterialPurchaseReturn.objects.select_for_update().get(pk=self.pk)
            if ret.status != 'draft':
                raise ValidationError(_("المرتجع اترحّل قبل كده."))
            purchase = ret.purchase
            if purchase.status != 'received':
                raise ValidationError(_("المرتجع بيتعمل على فاتورة مستلمة بس."))
            lines = list(ret.lines.select_related('purchase_line'))
            if not lines:
                raise ValidationError(_("أضف بند واحد على الأقل للمرتجع."))
            p_lines = list(purchase.lines.order_by('pk'))
            effective = purchase.effective_line_totals(p_lines)
            value = Decimal('0')
            for line in lines:
                pl = line.purchase_line
                if pl.purchase_id != purchase.pk:
                    raise ValidationError(_("بند من فاتورة تانية."))
                returned = (MaterialPurchaseReturnLine.objects
                            .filter(purchase_line=pl, ret__status='posted')
                            .aggregate(q=models.Sum('quantity'))['q'] or Decimal('0'))
                if line.quantity > pl.quantity - returned:
                    raise ValidationError(
                        _("«%(m)s»: المتاح للمرتجع %(q)s بس.") % {'m': pl.material.name, 'q': pl.quantity - returned})
                unit = effective[pl.pk] / pl.quantity
                line_value = (line.quantity * unit).quantize(Decimal('0.01'))
                mat = PrintMaterial.objects.select_for_update().get(pk=pl.material_id)
                if mat.quantity < line.quantity:
                    raise ValidationError(
                        _("«%(m)s»: المتاح في المخزن %(q)s أقل من كمية المرتجع.") % {'m': mat.name, 'q': mat.quantity})
                left = mat.quantity - line.quantity
                new_cost = ((max(mat.quantity * mat.cost_per_unit - line_value, Decimal('0')) / left)
                            .quantize(Decimal('0.0001')) if left > 0 else mat.cost_per_unit)
                PrintMaterial.objects.filter(pk=mat.pk).update(quantity=left, cost_per_unit=new_cost)
                MaterialPurchaseReturnLine.objects.filter(pk=line.pk).update(value=line_value)
                value += line_value
            tax = ((purchase.tax_amount * value / purchase.inventory_value).quantize(Decimal('0.01'))
                   if purchase.inventory_value else Decimal('0'))
            ret.value, ret.tax_amount = value, tax
            ret.status, ret.posted_at = 'posted', timezone.now()
            ret.save(update_fields=['value', 'tax_amount', 'status', 'posted_at'])
            if ret.refund_treasury_id:
                PrintTransaction.objects.create(
                    treasury_id=ret.refund_treasury_id, transaction_type='in', amount=ret.total,
                    purchase=purchase, created_by=user,
                    description=f"استرداد من المورد — مرتجع #{ret.number}")
        self.refresh_from_db()
        return self


class MaterialPurchaseReturnLine(models.Model):
    ret = models.ForeignKey(MaterialPurchaseReturn, on_delete=models.CASCADE, related_name='lines',
                            verbose_name=_("المرتجع"))
    purchase_line = models.ForeignKey(MaterialPurchaseLine, on_delete=models.PROTECT, related_name='return_lines',
                                      verbose_name=_("البند في الفاتورة"))
    quantity = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                                   verbose_name=_("الكمية المرتجعة"))
    value = models.DecimalField(max_digits=14, decimal_places=2, default=0, editable=False,
                                verbose_name=_("القيمة (بتكلفة الفاتورة)"))

    class Meta:
        verbose_name = _("بند مرتجع")
        verbose_name_plural = _("بنود المرتجع")

    def __str__(self):
        return f"{self.purchase_line.material} × {self.quantity}"

    def save(self, *args, **kwargs):
        status = MaterialPurchaseReturn.objects.filter(pk=self.ret_id).values_list('status', flat=True).first()
        if status and status != 'draft':
            raise ValidationError(_("بنود المرتجع مابتتعدّلش بعد الترحيل."))
        super().save(*args, **kwargs)


# =====================================================================
# 💳 20. سداد فواتير المورد من رصيدنا المقدّم عنده
# =====================================================================

class PrintSupplierAllocation(models.Model):
    """
    دفعة على حساب المورد (من غير فاتورة) بتتوزّع على فاتورة شراء أو مصروف — من غير
    حركة خزنة، فالفاتورة تبان «مدفوعة» ورصيد المورد مابيتغيّرش.
    """
    supplier = models.ForeignKey(PrintSupplier, on_delete=models.CASCADE, related_name='allocations',
                                 editable=False, verbose_name=_("المورد"))
    purchase = models.ForeignKey('MaterialPurchase', on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='credit_allocations', verbose_name=_("فاتورة الشراء"))
    bill = models.ForeignKey('PrintExpenseBill', on_delete=models.CASCADE, null=True, blank=True,
                             related_name='credit_allocations', verbose_name=_("فاتورة المصروف"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                                 verbose_name=_("المبلغ"))
    date = models.DateField(default=timezone.localdate, verbose_name=_("التاريخ"))
    notes = models.CharField(max_length=200, blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("تسديد من رصيدنا عند المورد")
        verbose_name_plural = _("تسديدات من رصيدنا عند الموردين")
        ordering = ['-date', '-id']

    def __str__(self):
        doc = self.purchase or self.bill
        return f"{doc} ← {self.amount:,.2f}"

    def _doc(self):
        from .accounting import PrintExpenseBill  # استيراد محلي لتفادي الدائرية
        if self.purchase_id:
            return MaterialPurchase.objects.select_for_update().get(pk=self.purchase_id)
        return PrintExpenseBill.objects.select_for_update().get(pk=self.bill_id)

    def save(self, *args, **kwargs):
        from django.db import transaction
        if bool(self.purchase_id) == bool(self.bill_id):
            raise ValidationError(_("اختار فاتورة شراء أو فاتورة مصروف (واحدة بس)."))
        with transaction.atomic():
            doc = self._doc()
            if self.purchase_id:
                if doc.status != 'received':
                    raise ValidationError(_("التسديد على فاتورة مستلمة بس."))
                due = doc.net_total - doc.returned_amount
            else:
                if doc.is_cancelled:
                    raise ValidationError(_("الفاتورة ملغية."))
                due = doc.total
            self.supplier_id = doc.supplier_id
            old = (PrintSupplierAllocation.objects.filter(pk=self.pk).values_list('amount', flat=True).first()
                   if self.pk else None) or Decimal('0')
            left = due - doc.compute_paid() + old
            if self.amount > left:
                raise ValidationError(_("المبلغ أكبر من الباقي على الفاتورة (%(a)s).") % {'a': f'{left:,.2f}'})
            credit = PrintSupplier.objects.get(pk=self.supplier_id).available_credit() + old
            if self.amount > credit:
                raise ValidationError(_("رصيدنا المتاح عند المورد %(a)s بس.") % {'a': f'{credit:,.2f}'})
            super().save(*args, **kwargs)

    @classmethod
    def apply_available(cls, doc, user=None):
        """سدّد الفاتورة من رصيدنا عند المورد بأقصى مبلغ متاح. بيرجّع المبلغ (أو صفر)."""
        is_purchase = isinstance(doc, MaterialPurchase)
        if is_purchase and doc.status != 'received':
            return Decimal('0')
        if not is_purchase and doc.is_cancelled:
            return Decimal('0')
        due = doc.net_total - doc.returned_amount if is_purchase else doc.total
        amount = min(doc.supplier.available_credit(), due - doc.compute_paid())
        if amount <= 0:
            return Decimal('0')
        cls.objects.create(amount=amount, created_by=user, notes='تسديد من رصيدنا المقدّم',
                           **({'purchase': doc} if is_purchase else {'bill': doc}))
        return amount
