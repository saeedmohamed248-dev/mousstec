"""📒 المطابع — الدفتر المحاسبي، الأصول الثابتة، فواتير المصروفات، فرع كل قيد."""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator
from ._common import _save_with_number
from .customers import PrintBranch
from .production import MachineProfile
from .purchasing import PrintSupplierAllocation
from .treasury import PrintTransaction, PrintTreasury


# =====================================================================
# 📒 13. حالة الدفتر المحاسبي للمطبعة
# =====================================================================

class PrintLedgerState(models.Model):
    """صف واحد بيسجّل إن الأرصدة الافتتاحية والحركات القديمة اتقيّدت في الدفتر (bootstrap).

    ``version``: لما شكل القيود يتغيّر (حساب لكل خزنة مثلاً) الرقم بيزيد في
    printing/ledger.py، وأول فتح للدفاتر بيعيد مزامنة كل المستندات مرة واحدة.
    """
    bootstrapped_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveSmallIntegerField(default=1)
    depreciation_through = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = _("حالة الدفتر المحاسبي")
        verbose_name_plural = _("حالة الدفتر المحاسبي")

    @classmethod
    def get(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


# =====================================================================
# 🏭 15. الأصول الثابتة والإهلاك (ماكينات/معدات)
# =====================================================================

class PrintFixedAsset(models.Model):
    """
    أصل ثابت (ماكينة، معدات تشطيب، كمبيوتر…) — بيتسجّل أصل ويتوزّع ثمنه على عمره.

    🐛 [FIX]: شراء ماكينة من الخزنة كان بيتقيّد «مصروف» في نفس الشهر، فالشهر ده
    يطلع خسران والشهور اللي بعده ربحها متضخّم. دلوقتي القسط الشهري (قسط ثابت)
    هو اللي بيدخل المصروفات، والبيع/الاستبعاد بيطلّع الربح أو الخسارة.
    """
    CATEGORY_CHOICES = (
        ('machine', _('ماكينة طباعة')),
        ('equipment', _('معدات تشطيب / قص')),
        ('computer', _('كمبيوتر وأجهزة')),
        ('furniture', _('أثاث وتجهيزات')),
        ('vehicle', _('سيارة')),
        ('other', _('أخرى')),
    )
    FUNDING_CHOICES = (
        ('cash', _('نقدي — اتدفع من الخزنة')),
        ('credit', _('آجل — على حساب مورد')),
        ('opening', _('أصل موجود قبل السيستم (رصيد افتتاحي)')),
    )

    name = models.CharField(max_length=200, verbose_name=_("اسم الأصل"))
    category = models.CharField(max_length=12, choices=CATEGORY_CHOICES, default='machine', verbose_name=_("النوع"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                               verbose_name=_("الفرع"))
    machine = models.OneToOneField(MachineProfile, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='fixed_asset', verbose_name=_("الماكينة المرتبطة"))
    supplier = models.ForeignKey('PrintSupplier', on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='fixed_assets', verbose_name=_("المورد"))
    purchase_date = models.DateField(default=timezone.localdate, verbose_name=_("تاريخ الشراء"))
    cost = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                               verbose_name=_("التكلفة"))
    salvage_value = models.DecimalField(max_digits=14, decimal_places=2, default=0,
                                        validators=[MinValueValidator(Decimal('0.00'))],
                                        verbose_name=_("قيمة الخردة في آخر العمر"))
    useful_life_months = models.PositiveSmallIntegerField(
        default=60, validators=[MinValueValidator(1)], verbose_name=_("العمر الإنتاجي (شهور)"),
        help_text=_("للأصل الافتتاحي: الشهور الباقية من عمره من أول بداية الإهلاك."))
    depreciation_start = models.DateField(null=True, blank=True, verbose_name=_("بداية الإهلاك"),
                                          help_text=_("افتراضي: تاريخ الشراء."))
    opening_accumulated = models.DecimalField(
        max_digits=14, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name=_("إهلاك متراكم قبل السيستم"),
        help_text=_("للأصل الافتتاحي بس: اللي اتهلك منه قبل ما تبدأ على السيستم."))
    funding = models.CharField(max_length=10, choices=FUNDING_CHOICES, default='cash', verbose_name=_("طريقة الشراء"))
    treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                 verbose_name=_("الخزنة اللي اتدفع منها"))
    disposed_on = models.DateField(null=True, blank=True, verbose_name=_("تاريخ البيع / الاستبعاد"))
    disposal_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0,
                                          validators=[MinValueValidator(Decimal('0.00'))],
                                          verbose_name=_("مبلغ البيع"))
    disposal_treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, null=True, blank=True,
                                          related_name='+', verbose_name=_("خزنة استلام مبلغ البيع"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("أصل ثابت")
        verbose_name_plural = _("الأصول الثابتة (ماكينات ومعدات)")
        ordering = ['-purchase_date', '-id']

    def __str__(self):
        return self.name

    @property
    def start(self):
        return self.depreciation_start or self.purchase_date

    @property
    def depreciable_base(self):
        """اللي فاضل يتهلك على السيستم = التكلفة − الخردة − المتراكم قبل السيستم."""
        return max(self.cost - self.salvage_value - self.opening_accumulated, Decimal('0'))

    def depreciation_for_month(self, year, month):
        """قسط الإهلاك (قسط ثابت) لشهر معيّن — آخر شهر بياخد فرق التقريب."""
        base = self.depreciable_base
        life = self.useful_life_months or 1
        k = (year * 12 + month) - (self.start.year * 12 + self.start.month)
        if base <= 0 or k < 0 or k >= life:
            return Decimal('0')
        # شهر البيع/الاستبعاد ومابعده مالهمش إهلاك (الاستبعاد بيقفل المتراكم لحد الشهر اللي قبله).
        if self.disposed_on and (year, month) >= (self.disposed_on.year, self.disposed_on.month):
            return Decimal('0')
        per = (base / life).quantize(Decimal('0.01'))
        return base - per * (life - 1) if k == life - 1 else per

    def accumulated_until(self, on_date=None):
        """الإهلاك المتراكم (شامل اللي قبل السيستم) للشهور اللي خلصت لحد التاريخ —
        نفس الشهور اللي قسطها اتقيّد في الدفتر (القسط بيتقيّد آخر الشهر)."""
        import calendar
        on_date = on_date or timezone.localdate()
        total = self.opening_accumulated
        y, m = self.start.year, self.start.month
        while (y, m) <= (on_date.year, on_date.month):
            if on_date.day < calendar.monthrange(y, m)[1] and (y, m) == (on_date.year, on_date.month):
                break
            total += self.depreciation_for_month(y, m)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return total

    def book_value(self, on_date=None):
        return self.cost - self.accumulated_until(on_date)

    def clean(self):
        super().clean()
        if self.salvage_value and self.cost and self.salvage_value > self.cost:
            raise ValidationError(_("قيمة الخردة أكبر من التكلفة."))
        if self.opening_accumulated and self.funding != 'opening':
            raise ValidationError(_("الإهلاك المتراكم قبل السيستم للأصل الافتتاحي بس."))
        if self.funding == 'credit' and not self.supplier_id:
            raise ValidationError(_("الشراء الآجل لازم يتربط بمورد."))
        if self.funding == 'cash' and not self.pk and not self.treasury_id:
            raise ValidationError(_("اختار الخزنة اللي اتدفع منها ثمن الأصل."))
        if self.disposal_amount and not self.disposal_treasury_id:
            raise ValidationError(_("اختار الخزنة اللي استلمت مبلغ البيع."))
        if self.disposed_on and self.disposed_on < self.purchase_date:
            raise ValidationError(_("تاريخ البيع قبل تاريخ الشراء."))

    def save(self, *args, **kwargs):
        from django.db import transaction
        old = (PrintFixedAsset.objects.filter(pk=self.pk).values(
            'disposed_on', 'disposal_amount', 'disposal_treasury_id', 'cost', 'funding', 'treasury_id').first()
               if self.pk else None)
        if old:
            if old['funding'] == 'cash' and (old['cost'], old['funding'], old['treasury_id']) != (
                    self.cost, self.funding, self.treasury_id):
                raise ValidationError(_("ثمن الأصل اتصرف من الخزنة — مينفعش تغيّر التكلفة أو طريقة الدفع. "
                                        "امسح الأصل (الفلوس بترجع الخزنة) وسجّله تاني."))
            if old['disposed_on'] and (old['disposed_on'], old['disposal_amount'], old['disposal_treasury_id']) != (
                    self.disposed_on, self.disposal_amount, self.disposal_treasury_id):
                raise ValidationError(_("البيع/الاستبعاد اتسجّل خلاص — مينفعش يتعدّل."))
        is_new = self.pk is None
        with transaction.atomic():
            super().save(*args, **kwargs)
            try:
                if is_new and self.funding == 'cash' and self.treasury_id:
                    PrintTransaction.objects.create(
                        treasury_id=self.treasury_id, transaction_type='out', amount=self.cost,
                        fixed_asset=self, created_by=self.created_by,
                        date=timezone.make_aware(timezone.datetime.combine(self.purchase_date, timezone.datetime.min.time()))
                        if self.purchase_date != timezone.localdate() else timezone.now(),
                        description=f"شراء أصل ثابت — {self.name}")
                newly_sold = (self.disposed_on and self.disposal_amount and self.disposal_treasury_id
                              and not (old and old['disposed_on'] and old['disposal_amount']))
                if newly_sold:
                    PrintTransaction.objects.create(
                        treasury_id=self.disposal_treasury_id, transaction_type='in', amount=self.disposal_amount,
                        fixed_asset=self, created_by=self.created_by,
                        description=f"بيع أصل ثابت — {self.name}")
            except ValueError:
                raise ValidationError(_("رصيد الخزنة مش كفاية لثمن الأصل."))

    def delete(self, *args, **kwargs):
        """مسح الأصل بيمسح حركات شرائه/بيعه (الفلوس بترجع مكانها) والقيود بتتشال."""
        from django.db import transaction
        with transaction.atomic():
            for txn in self.payments.all():
                txn._deleting_with_asset = True
                txn.delete()
            return super().delete(*args, **kwargs)


# =====================================================================
# 🧾 16. فواتير المصروفات والخدمات (إيجار/كهرباء/صيانة…) — مستحقة على المورد
# =====================================================================

class PrintExpenseBill(models.Model):
    """
    فاتورة مصروف من مورد خدمة (صاحب المكان، شركة الكهرباء، فني صيانة…).

    🐛 [FIX]: المصروف كان بيتسجّل بس يوم ما الفلوس تطلع من الخزنة — فاتورة إيجار
    الشهر اللي لسه مادفعتهاش مش ظاهرة في مصروفاته ولا في التزاماتك. الفاتورة
    بتقيّد المصروف في شهره كالتزام على حساب المورد، والسداد بعدين بيقفله.
    """
    CATEGORY_CHOICES = (
        ('rent', _('إيجار')),
        ('utilities', _('كهرباء ومياه ومرافق')),
        ('maintenance', _('صيانة')),
        ('shipping', _('شحن ومواصلات')),
        ('marketing', _('دعاية وتسويق')),
        ('materials', _('خامات اتصرفت مباشرة (من غير مخزن)')),
        ('other_expense', _('مصروفات أخرى')),
    )
    PAYMENT_CHOICES = (
        ('credit', _('آجل — على حساب المورد')),
        ('cash', _('نقدي — يتصرف من الخزنة دلوقتي')),
    )
    number = models.CharField(max_length=30, unique=True, blank=True, editable=False, verbose_name=_("رقم الفاتورة"))
    supplier = models.ForeignKey('PrintSupplier', on_delete=models.PROTECT, related_name='bills', verbose_name=_("المورد"))
    supplier_bill_no = models.CharField(max_length=50, blank=True, verbose_name=_("رقم فاتورة المورد"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                               verbose_name=_("الفرع"))
    date = models.DateField(default=timezone.localdate, verbose_name=_("تاريخ الاستحقاق (الشهر اللي يخصّه)"))
    due_date = models.DateField(null=True, blank=True, verbose_name=_("آخر ميعاد للسداد"))
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other_expense', verbose_name=_("البند"))
    description = models.CharField(max_length=300, verbose_name=_("البيان"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                                 verbose_name=_("المبلغ (من غير ضريبة)"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                     validators=[MinValueValidator(Decimal('0.00'))],
                                     verbose_name=_("ضريبة القيمة المضافة"))
    payment_terms = models.CharField(max_length=10, choices=PAYMENT_CHOICES, default='credit', verbose_name=_("طريقة الدفع"))
    treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                                 verbose_name=_("الخزنة اللي هيتصرف منها"))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False,
                                      verbose_name=_("المدفوع"))
    is_cancelled = models.BooleanField(default=False, editable=False, verbose_name=_("ملغية"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("فاتورة مصروف / خدمة")
        verbose_name_plural = _("فواتير المصروفات والخدمات")
        ordering = ['-date', '-id']

    def __str__(self):
        return f"#{self.number} — {self.supplier}"

    @property
    def total(self):
        return (self.amount or Decimal('0')) + (self.tax_amount or Decimal('0'))

    @property
    def remaining(self):
        return self.total - self.paid_amount if not self.is_cancelled else Decimal('0')

    def compute_paid(self):
        agg = PrintTransaction.objects.filter(bill_id=self.pk).aggregate(
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')),
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')))
        allocated = (PrintSupplierAllocation.objects.filter(bill_id=self.pk)
                     .aggregate(t=models.Sum('amount'))['t'] or Decimal('0'))
        return (agg['outs'] or Decimal('0')) - (agg['ins'] or Decimal('0')) + allocated

    def clean(self):
        super().clean()
        if self.payment_terms == 'cash' and not self.pk and not self.treasury_id:
            raise ValidationError(_("اختار الخزنة اللي هيتصرف منها (أو خلّي الدفع آجل)."))

    def save(self, *args, **kwargs):
        from django.db import transaction
        is_new = self.pk is None
        with transaction.atomic():
            if self.number:
                super().save(*args, **kwargs)
            else:
                _save_with_number(self, 'number', 'EXP-', super().save, *args, **kwargs)
            if is_new and self.payment_terms == 'cash' and self.treasury_id:
                try:
                    PrintTransaction.objects.create(
                        treasury_id=self.treasury_id, transaction_type='out', amount=self.total, bill=self,
                        created_by=self.created_by,
                        description=f"سداد فاتورة مصروف #{self.number} — {self.supplier.name}")
                except ValueError:
                    raise ValidationError(
                        _("رصيد الخزنة مش كفاية لسداد %(a)s — خلّي الدفع آجل.") % {'a': f'{self.total:,.2f}'})

    def cancel(self):
        """الفاتورة اتلغت (غلط/اتكررت) — اللي اتدفع عليها بيبقى رصيد لينا عند المورد."""
        PrintExpenseBill.objects.filter(pk=self.pk).update(is_cancelled=True)
        self.is_cancelled = True
        from printing import ledger
        ledger.on_bill_changed(self.pk, supplier_id=self.supplier_id)


# =====================================================================
# 🏢 21. فرع كل قيد (قائمة دخل لكل فرع)
# =====================================================================

class PrintJournalBranch(models.Model):
    """
    فرع المطبعة اللي القيد يخصّه (من مستنده: الطلب/الخزنة/الفاتورة/الأصل).

    جدول جنب القيد بدل فرع النظام الأساسي — فروع المطابع مش فروع المخزون، وعمل
    فرع هناك كان بيتحسب على حد الفروع في الباقة. القيود العامة (مرتبات، إقفال،
    تبويب أرصدة) مالهاش فرع وبتظهر «عام».
    """
    journal_entry = models.OneToOneField('inventory.JournalEntry', on_delete=models.CASCADE,
                                         related_name='print_branch')
    branch = models.ForeignKey(PrintBranch, on_delete=models.CASCADE, related_name='+')

    class Meta:
        verbose_name = _("فرع القيد")
        verbose_name_plural = _("فروع القيود")

    def __str__(self):
        return f"{self.journal_entry_id} → {self.branch}"
