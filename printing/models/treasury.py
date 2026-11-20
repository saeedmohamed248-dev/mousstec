"""💰 المطابع — الخزائن والحركات والتحويلات ومطابقة البنك وإقرار الضريبة."""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator
from .customers import PrintBranch, PrintCustomer
from .production import PrintOrder


# =====================================================================
# 💰 6. الخزينة والمصروفات (خاصة بالمطابع)
# =====================================================================

class PrintTreasury(models.Model):
    """خزينة المطبعة"""
    name = models.CharField(max_length=100, verbose_name=_("اسم الخزينة"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name=_("الرصيد"))
    KIND_CHOICES = (
        ('cash', _('خزنة نقدية')),
        ('bank', _('حساب بنكي')),
        ('wallet', _('محفظة إلكترونية / فيزا')),
        ('cheques_in', _('حافظة شيكات واردة (أوراق قبض)')),
        ('cheques_out', _('شيكات صادرة لسه ماتصرفتش (أوراق دفع)')),
        ('wht_in', _('خصم من المنبع لينا (شهادات العملاء)')),
        ('wht_out', _('خصم وإضافة علينا (مستحق للضرايب)')),
    )
    # فلوس حقيقية (بتتصرف منها مصروفات ومرتبات وبتدخل التدفق النقدي).
    MONEY_KINDS = ('cash', 'bank', 'wallet')
    # حوافظ افتراضية رصيدها بالسالب = التزام علينا (شيكات لسه ماتصرفتش / ضريبة لسه ماتوردتش).
    LIABILITY_KINDS = ('cheques_out', 'wht_out')
    SYSTEM_WALLET_NAMES = {
        'cheques_in': _('🧾 حافظة الشيكات الواردة'),
        'cheques_out': _('🧾 الشيكات الصادرة'),
        'wht_in': _('🧾 خصم من المنبع (شهادات العملاء)'),
        'wht_out': _('🧾 خصم وإضافة مستحق للضرايب'),
    }
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default='cash', verbose_name=_("النوع"),
                            help_text=_("البنك والمحفظة ليهم مطابقة مع كشف الحساب."))
    is_active = models.BooleanField(default=True, verbose_name=_("نشطة"))

    class Meta:
        verbose_name = _("خزينة")
        verbose_name_plural = _("الخزائن")

    def __str__(self):
        return f"{self.name} ({self.balance:,.2f} ج.م)"

    @property
    def is_money(self):
        return self.kind in self.MONEY_KINDS

    @classmethod
    def system_wallet(cls, kind):
        """الحافظة الافتراضية للنوع (بتتعمل أول مرة تتطلب)."""
        wallet = cls.objects.filter(kind=kind).order_by('pk').first()
        if wallet is None:
            wallet = cls.objects.create(name=str(cls.SYSTEM_WALLET_NAMES[kind]), kind=kind)
        return wallet

    def save(self, *args, **kwargs):
        # 🐛 [FIX]: الرصيد بيتحرك من الحركات بس (F() في الداتابيز) — نسخة قديمة من
        #    الخزنة في الذاكرة (تعديل الاسم/النوع من الأدمن) كانت بتكتب الرصيد القديم
        #    فوق الحركات اللي حصلت بعد ما اتحمّلت، فالرصيد يبعد عن حركاته بصمت.
        if self.pk and kwargs.get('update_fields') is None:
            current = PrintTreasury.objects.filter(pk=self.pk).values_list('balance', flat=True).first()
            if current is not None:
                self.balance = current
        super().save(*args, **kwargs)


class PrintTransaction(models.Model):
    """حركة مالية في خزينة المطبعة"""
    TYPE_CHOICES = (
        ('in', _('إيداع / إيراد')),
        ('out', _('سحب / مصروف')),
    )

    # 🏷️ بند الحركة — بيحدد القيد المحاسبي (تحصيل عميل ≠ سداد مورد ≠ مصروف ≠ رأس مال).
    CATEGORY_CHOICES = (
        ('customer', _('تحصيل / رد فلوس عميل')),
        ('supplier', _('سداد / استرداد من مورد')),
        ('salaries', _('رواتب وأجور')),
        ('advance', _('سلف موظفين')),
        ('rent', _('إيجار')),
        ('utilities', _('كهرباء ومياه ومرافق')),
        ('maintenance', _('صيانة')),
        ('shipping', _('شحن ومواصلات')),
        ('marketing', _('دعاية وتسويق')),
        ('materials', _('خامات اتصرفت مباشرة (من غير مخزن)')),
        ('other_expense', _('مصروفات أخرى')),
        ('other_income', _('إيرادات أخرى')),
        ('capital', _('رأس مال / إيداع من المالك')),
        ('drawings', _('مسحوبات المالك')),
        ('transfer', _('تحويل بين الخزائن')),
        ('fixed_asset', _('شراء أصل ثابت (ماكينة/معدات)')),
        ('asset_sale', _('بيع أصل ثابت')),
        ('payroll', _('صرف مرتبات مستحقة (دورة رواتب معتمدة)')),
        ('vat', _('سداد ضريبة القيمة المضافة')),
    )
    # نفس كلمات التصنيف القديم في تقرير الأرباح — للحركات اللي اتسجّلت من غير بند.
    _CATEGORY_KEYWORDS = (
        ('advance', ('صرف سلفة', 'سلفة')),
        ('salaries', ('راتب', 'مرتب', 'رواتب', 'بونص', 'حافز', 'salary', 'payroll')),
        ('rent', ('ايجار', 'إيجار', 'rent')),
        ('utilities', ('كهرب', 'مياه', 'انترنت', 'إنترنت', 'electricity', 'water', 'internet')),
        ('materials', ('ورق', 'حبر', 'خام', 'فينيل', 'paper', 'ink', 'material')),
        ('maintenance', ('صيان', 'تصليح', 'maintain', 'repair')),
        ('shipping', ('شحن', 'توصيل', 'مواصلات', 'delivery', 'transport')),
        ('marketing', ('إعلان', 'اعلان', 'دعاية', 'تسويق', 'marketing')),
    )

    treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, verbose_name=_("الخزينة"))
    transaction_type = models.CharField(max_length=3, choices=TYPE_CHOICES, verbose_name=_("النوع"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))], verbose_name=_("المبلغ"))
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, blank=True, default='', db_index=True,
        verbose_name=_("البند"),
        help_text=_("لو سبته فاضي بيتحدد لوحده: دفعة عميل/مورد من الربط، والباقي من الوصف."))
    description = models.CharField(max_length=300, blank=True, verbose_name=_("الوصف"))
    order = models.ForeignKey(PrintOrder, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الطلب المرتبط"))
    customer = models.ForeignKey(
        PrintCustomer, on_delete=models.PROTECT, null=True, blank=True, related_name='payments',
        verbose_name=_("العميل"),
        help_text=_("دفعة على حساب العميل من غير طلب = رصيد مقدّم يتسدّد منه بعدين."))
    supplier = models.ForeignKey(
        'PrintSupplier', on_delete=models.PROTECT, null=True, blank=True, related_name='payments',
        verbose_name=_("المورد"))
    purchase = models.ForeignKey(
        'MaterialPurchase', on_delete=models.PROTECT, null=True, blank=True, related_name='payments',
        verbose_name=_("فاتورة الشراء"))
    bill = models.ForeignKey(
        'PrintExpenseBill', on_delete=models.PROTECT, null=True, blank=True, related_name='payments',
        verbose_name=_("فاتورة مصروف"))
    fixed_asset = models.ForeignKey(
        'PrintFixedAsset', on_delete=models.PROTECT, null=True, blank=True, related_name='payments',
        verbose_name=_("الأصل الثابت"))
    # 🧾 الخصم من المنبع: العميل خصمه من الدفعة (شهادة خصم) أو إحنا خصمناه من المورد —
    #    بيتحسب سداد على الطلب/الفاتورة من غير ما يدخل الخزنة (حركة مرافقة على حافظته).
    withholding_tax = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name=_("خصم من المنبع"),
        help_text=_("ضريبة الخصم والإضافة اللي اتخصمت من الدفعة — بتتحسب من السداد ومش بتدخل الخزنة."))
    withholding_txn = models.OneToOneField('self', on_delete=models.SET_NULL, null=True, blank=True,
                                           editable=False, related_name='withholding_parent')
    # 🏦 مطابقة البنك: الحركة ظهرت في كشف الحساب البنكي
    cleared = models.BooleanField(default=False, db_index=True, verbose_name=_("ظهرت في كشف البنك"))
    cleared_on = models.DateField(null=True, blank=True, verbose_name=_("تاريخ المطابقة"))
    date = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name=_("بواسطة"))

    class Meta:
        verbose_name = _("حركة مالية")
        verbose_name_plural = _("الحركات المالية")
        ordering = ['-date']

    def __str__(self):
        icon = "🟢" if self.transaction_type == 'in' else "🔴"
        return f"{icon} {self.amount:,.2f} ج.م — {self.description[:50]}"

    @staticmethod
    def _signed(txn_type, amount):
        return amount if txn_type == 'in' else -amount

    @classmethod
    def guess_category(cls, description, txn_type):
        text = (description or '').lower()
        for key, words in cls._CATEGORY_KEYWORDS:
            if any(w in text for w in words):
                if txn_type == 'in' and key not in ('advance',):
                    # فلوس داخلة بكلمة مصروف = استرداد/إيراد عارض، مش مصروف بالسالب
                    break
                return key
        return 'other_income' if txn_type == 'in' else 'other_expense'

    def clean(self):
        super().clean()
        if (self.order_id or self.customer_id) and (self.purchase_id or self.supplier_id or self.bill_id):
            raise ValidationError(_("الحركة مينفعش تتربط بعميل ومورد في نفس الوقت."))
        if self.order_id and self.customer_id and self.order.customer_id != self.customer_id:
            raise ValidationError(_("العميل المختار مش هو عميل الطلب."))
        if self.purchase_id and self.supplier_id and self.purchase.supplier_id != self.supplier_id:
            raise ValidationError(_("المورد المختار مش هو مورد فاتورة الشراء."))
        if self.category == 'customer' and not (self.order_id or self.customer_id):
            raise ValidationError(_("دفعة العميل لازم تتربط بطلب أو بعميل."))
        if self.category == 'supplier' and not (self.purchase_id or self.supplier_id or self.bill_id):
            raise ValidationError(_("دفعة المورد لازم تتربط بفاتورة شراء أو بمورد."))
        if self.category in ('fixed_asset', 'asset_sale') and not self.fixed_asset_id:
            raise ValidationError(_("شراء/بيع الأصول بيتسجّل من شاشة «الأصول الثابتة» عشان يتهلك صح."))
        if self.withholding_tax:
            customer_receipt = self.transaction_type == 'in' and (self.order_id or self.customer_id)
            supplier_payment = self.transaction_type == 'out' and (self.purchase_id or self.supplier_id or self.bill_id)
            if not (customer_receipt or supplier_payment):
                raise ValidationError(_("الخصم من المنبع بيتسجّل على دفعة من عميل أو سداد لمورد بس."))
            if self.treasury_id and not self.treasury.is_money:
                raise ValidationError(_("الخصم من المنبع بيتسجّل على دفعة في خزنة أو بنك."))

    def _fill_links(self):
        """العميل/المورد من الطلب/الفاتورة، والبند من الربط أو الوصف."""
        from .accounting import PrintExpenseBill  # استيراد محلي لتفادي الدائرية
        from .purchasing import MaterialPurchase  # استيراد محلي لتفادي الدائرية
        if self.order_id:
            self.customer_id = PrintOrder.objects.filter(pk=self.order_id).values_list('customer_id', flat=True).first()
        if self.purchase_id:
            self.supplier_id = MaterialPurchase.objects.filter(pk=self.purchase_id).values_list('supplier_id', flat=True).first()
        if self.bill_id:
            self.supplier_id = PrintExpenseBill.objects.filter(pk=self.bill_id).values_list('supplier_id', flat=True).first()
        if self.customer_id:
            self.category = 'customer'
        elif self.supplier_id:
            self.category = 'supplier'
        elif self.fixed_asset_id:
            self.category = 'fixed_asset' if self.transaction_type == 'out' else 'asset_sale'
        elif not self.category or self.category in ('customer', 'supplier'):
            self.category = self.guess_category(self.description, self.transaction_type)

    def save(self, *args, **kwargs):
        """
        Keep the treasury balance equal to the sum of its transactions.

        🐛 [FIX]: تعديل حركة موجودة (المبلغ/النوع/الخزنة) كان بيحفظها من غير
        ما يعدّل رصيد الخزنة — فالرصيد بيبعد عن الحركات بصمت. دلوقتي بنرجّع
        أثر النسخة القديمة ونطبّق أثر الجديدة في نفس العملية.
        """
        self._fill_links()
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'customer', 'supplier', 'category'})
        from django.db.models import F as _F
        from django.db import transaction as _txn
        with _txn.atomic():
            old = None
            if self.pk:
                old = (PrintTransaction.objects.select_for_update()
                       .filter(pk=self.pk).values('treasury_id', 'transaction_type', 'amount').first())
            super().save(*args, **kwargs)
            unchanged = old is not None and (old['treasury_id'], old['transaction_type'], old['amount']) == \
                (self.treasury_id, self.transaction_type, self.amount)
            if not unchanged:
                if old is not None:
                    PrintTreasury.objects.filter(pk=old['treasury_id']).update(
                        balance=_F('balance') - self._signed(old['transaction_type'], old['amount']))
                treasury = PrintTreasury.objects.select_for_update().get(pk=self.treasury_id)
                delta = self._signed(self.transaction_type, self.amount)
                # 🛡️ Negative balance check (حوافظ الالتزامات رصيدها بالسالب طبيعي)
                if delta < 0 and treasury.balance + delta < 0 and treasury.kind not in PrintTreasury.LIABILITY_KINDS:
                    raise ValueError("رصيد الخزنة لا يكفي لإتمام العملية.")
                PrintTreasury.objects.filter(pk=treasury.pk).update(balance=_F('balance') + delta)
            self._sync_withholding()

    def _sync_withholding(self):
        """
        الحركة المرافقة للخصم من المنبع على حافظته — بنفس الطلب/الفاتورة/الطرف، فالمدفوع
        وكشف الحساب والقيد بيشوفوها لوحدهم (مدين ضريبة خصم لينا / دائن العملاء، أو
        مدين الموردين / دائن خصم وإضافة مستحق للضرايب).
        """
        wht = self.withholding_tax or Decimal('0')
        companion = (PrintTransaction.objects.filter(pk=self.withholding_txn_id).first()
                     if self.withholding_txn_id else None)
        if wht <= 0:
            if companion is not None:
                PrintTransaction.objects.filter(pk=self.pk).update(withholding_txn=None)
                self.withholding_txn_id = None
                companion._with_parent = True
                companion.delete()
            return
        kind = 'wht_in' if self.transaction_type == 'in' else 'wht_out'
        note = f" — {self.description}" if self.description else ''
        values = dict(
            treasury=PrintTreasury.system_wallet(kind), transaction_type=self.transaction_type, amount=wht,
            order_id=self.order_id, customer_id=self.customer_id, purchase_id=self.purchase_id,
            bill_id=self.bill_id, supplier_id=self.supplier_id, date=self.date, created_by_id=self.created_by_id,
            description=f"خصم من المنبع على الدفعة #{self.pk}{note}"[:300])
        if companion is None:
            companion = PrintTransaction(**values)
            companion.save()
            PrintTransaction.objects.filter(pk=self.pk).update(withholding_txn=companion)
            self.withholding_txn_id = companion.pk
        else:
            for key, value in values.items():
                setattr(companion, key, value)
            companion.save()

    # Deleting a transaction reverses its balance effect in the post_delete
    # signal (printing/signals.py) — a model ``delete()`` override is skipped
    # by queryset/bulk deletes (e.g. the admin "delete selected" action), which
    # used to leave the balance wrong.


# =====================================================================
# 🔁 18. التحويل بين الخزائن + 🏦 مطابقة البنك + 🧾 إقرار الضريبة
# =====================================================================

class PrintTreasuryTransfer(models.Model):
    """تحويل فلوس من خزنة لخزنة (درج ← بنك مثلاً) — حركتين في عملية واحدة."""
    from_treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, related_name='+',
                                      verbose_name=_("من خزنة"))
    to_treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, related_name='+',
                                    verbose_name=_("إلى خزنة"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                                 verbose_name=_("المبلغ"))
    date = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات"))
    out_txn = models.OneToOneField(PrintTransaction, on_delete=models.SET_NULL, null=True, blank=True,
                                   editable=False, related_name='+')
    in_txn = models.OneToOneField(PrintTransaction, on_delete=models.SET_NULL, null=True, blank=True,
                                  editable=False, related_name='+')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("تحويل بين الخزائن")
        verbose_name_plural = _("التحويلات بين الخزائن")
        ordering = ['-date', '-id']

    def __str__(self):
        return f"{self.from_treasury.name} ← {self.to_treasury.name}: {self.amount:,.2f}"

    def clean(self):
        super().clean()
        if self.from_treasury_id and self.from_treasury_id == self.to_treasury_id:
            raise ValidationError(_("اختار خزنتين مختلفتين."))

    def save(self, *args, **kwargs):
        from django.db import transaction
        if self.pk:
            raise ValidationError(_("التحويل مابيتعدّلش — امسحه واعمل واحد جديد."))
        self.clean()
        with transaction.atomic():
            try:
                note = f" — {self.notes}" if self.notes else ''
                self.out_txn = PrintTransaction.objects.create(
                    treasury_id=self.from_treasury_id, transaction_type='out', amount=self.amount,
                    category='transfer', date=self.date, created_by=self.created_by,
                    description=f"تحويل إلى {self.to_treasury.name}{note}")
            except ValueError:
                raise ValidationError(_("رصيد خزنة «%(t)s» مش كفاية.") % {'t': self.from_treasury.name})
            self.in_txn = PrintTransaction.objects.create(
                treasury_id=self.to_treasury_id, transaction_type='in', amount=self.amount,
                category='transfer', date=self.date, created_by=self.created_by,
                description=f"تحويل من {self.from_treasury.name}{note}")
            super().save(*args, **kwargs)


class PrintBankReconciliation(models.Model):
    """مطابقة رصيد البنك/المحفظة مع كشف الحساب في تاريخ معيّن."""
    treasury = models.ForeignKey(PrintTreasury, on_delete=models.PROTECT, related_name='reconciliations',
                                 verbose_name=_("الحساب"))
    statement_date = models.DateField(verbose_name=_("تاريخ الكشف"))
    statement_balance = models.DecimalField(max_digits=15, decimal_places=2, verbose_name=_("الرصيد في كشف البنك"))
    cleared_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0, editable=False,
                                          verbose_name=_("رصيد الحركات المطابقة"))
    book_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0, editable=False,
                                       verbose_name=_("الرصيد في السيستم"))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("مطابقة بنك")
        verbose_name_plural = _("مطابقات البنوك")
        ordering = ['-statement_date', '-id']

    def __str__(self):
        return f"{self.treasury.name} — {self.statement_date}"

    @property
    def difference(self):
        return self.statement_balance - self.cleared_balance


class PrintVatReturn(models.Model):
    """إقرار ضريبة القيمة المضافة لفترة: المحصّلة على المبيعات − المدفوعة على المشتريات."""
    date_from = models.DateField(verbose_name=_("من"))
    date_to = models.DateField(verbose_name=_("إلى"))
    output_vat = models.DecimalField(max_digits=14, decimal_places=2, default=0, editable=False,
                                     verbose_name=_("ضريبة المبيعات"))
    input_vat = models.DecimalField(max_digits=14, decimal_places=2, default=0, editable=False,
                                    verbose_name=_("ضريبة المشتريات"))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("إقرار ضريبة القيمة المضافة")
        verbose_name_plural = _("إقرارات ضريبة القيمة المضافة")
        ordering = ['-date_to']

    def __str__(self):
        return f"{self.date_from} → {self.date_to}"

    @property
    def net_payable(self):
        """موجب = مستحق للضرايب، سالب = رصيد لينا."""
        return self.output_vat - self.input_vat
