"""🧾 المطابع — المبيعات: عروض الأسعار، البروفات، الرصيد المقدّم، الإشعار الدائن."""
import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
from ._common import _save_with_number
from .customers import PrintCustomer
from .production import PrintJob, PrintOrder
from .treasury import PrintTransaction, PrintTreasury


# =====================================================================
# 💰 8. عروض الأسعار (Price Quotations)
# =====================================================================

class PriceQuotation(models.Model):
    """عرض سعر يرسله المصمم/الموظف للعميل قبل تأكيد الطلب.

    Workflow:
    1. الموظف ينشئ عرض → يضيف بنود → يحدد المدة
    2. ينسخ رابط المشاركة العمومي ويرسله للعميل (واتساب/إيميل)
    3. العميل يفتح الرابط → يشوف العرض بشكل احترافي → يقبل أو يرفض
    4. لو قبل → ممكن يتحوّل تلقائياً لـ PrintOrder
    """
    STATUS_CHOICES = (
        ('draft',     _('مسودة')),
        ('sent',      _('تم الإرسال — في انتظار الرد')),
        ('accepted',  _('مقبول')),
        ('rejected',  _('مرفوض')),
        ('expired',   _('منتهي الصلاحية')),
        ('converted', _('تحوّل لطلب')),
    )

    # ─ رقم وعميل ─
    quote_number = models.CharField(max_length=30, unique=True, blank=True,
        verbose_name=_("رقم العرض"), help_text=_("يُولّد تلقائياً"))
    customer = models.ForeignKey(PrintCustomer, on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name=_("العميل المسجل"),
        help_text=_("اختياري — لو العميل مش مسجل، اكتب اسمه وتليفونه يدوياً"))
    customer_name = models.CharField(max_length=150, blank=True,
        verbose_name=_("اسم العميل (لو غير مسجل)"))
    customer_phone = models.CharField(max_length=20, blank=True,
        verbose_name=_("تليفون العميل"))
    customer_whatsapp = models.CharField(max_length=20, blank=True,
        verbose_name=_("واتساب العميل"))

    # ─ المحتوى ─
    title = models.CharField(max_length=200, verbose_name=_("عنوان العرض"),
        help_text=_("مثال: تصميم لوجو + 500 كرت شخصي"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات / شروط"))

    # ─ الأرقام ─
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        verbose_name=_("المجموع الفرعي"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        validators=[MinValueValidator(Decimal('0'))], verbose_name=_("الخصم"))
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        verbose_name=_("نسبة الضريبة %"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        verbose_name=_("الإجمالي النهائي"))

    # ─ الحالة والصلاحية ─
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='draft',
        verbose_name=_("الحالة"))
    valid_until = models.DateField(null=True, blank=True,
        verbose_name=_("صالح حتى"), help_text=_("افتراضي: 7 أيام من تاريخ الإنشاء"))

    # ─ رابط المشاركة العمومي ─
    share_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
        verbose_name=_("توكن المشاركة"))

    # ─ التتبع ─
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name='quotations_created', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الإرسال"))
    responded_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ رد العميل"))
    converted_order = models.ForeignKey(PrintOrder, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='source_quotations',
        verbose_name=_("الطلب الناتج"))

    class Meta:
        verbose_name = _("عرض سعر")
        verbose_name_plural = _("عروض الأسعار")
        ordering = ['-created_at']

    def __str__(self):
        return f"عرض #{self.quote_number} — {self.customer_display}"

    @property
    def customer_display(self):
        return self.customer.name if self.customer else (self.customer_name or 'بدون اسم')

    @property
    def is_expired(self):
        if not self.valid_until:
            return False
        return timezone.localdate() > self.valid_until

    @property
    def tax_amount(self):
        return ((self.subtotal - self.discount) * self.tax_percent) / Decimal('100')

    def recalc_totals(self, save=True):
        """يحسب subtotal من البنود + يحدّث total."""
        lines_sum = sum((l.line_total for l in self.lines.all()), Decimal('0'))
        self.subtotal = lines_sum
        after_discount = lines_sum - self.discount
        if after_discount < 0:
            after_discount = Decimal('0')
        self.total = after_discount + ((after_discount * self.tax_percent) / Decimal('100'))
        if save:
            self.save(update_fields=['subtotal', 'total'])

    def save(self, *args, **kwargs):
        if not self.valid_until:
            from datetime import timedelta as _td
            self.valid_until = timezone.localdate() + _td(days=7)
        if self.quote_number:
            return super().save(*args, **kwargs)
        # 🐛 [FIX]: عرضين في نفس اللحظة كانوا بياخدوا نفس الرقم فالتاني يقع
        #    بـ IntegrityError (unique). نفس أسلوب ترقيم PrintOrder: نعيد المحاولة.
        from django.db import IntegrityError as _IE, transaction as _txn
        today = timezone.now().strftime('%y%m%d')
        prefix = f'QT-{today}-'
        for _attempt in range(5):
            last = (PriceQuotation.objects
                    .filter(quote_number__startswith=prefix)
                    .order_by('-quote_number').first())
            seq = 1
            if last and last.quote_number:
                try:
                    seq = int(last.quote_number.split('-')[-1]) + 1
                except (ValueError, IndexError):
                    seq = 1
            self.quote_number = f'{prefix}{seq:03d}'
            try:
                with _txn.atomic():
                    return super().save(*args, **kwargs)
            except _IE:
                self.quote_number = ''
        raise _IE('تعذّر توليد رقم عرض سعر فريد — حاول مرة أخرى.')


class QuotationLine(models.Model):
    """بند داخل عرض السعر — وصف + كمية + سعر وحدة."""
    quotation = models.ForeignKey(PriceQuotation, on_delete=models.CASCADE,
        related_name='lines', verbose_name=_("العرض"))
    description = models.CharField(max_length=300, verbose_name=_("الوصف"),
        help_text=_("مثال: تصميم لوجو احترافي بـ 3 خيارات"))
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1'),
        validators=[MinValueValidator(Decimal('0.01'))], verbose_name=_("الكمية"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        validators=[MinValueValidator(Decimal('0'))], verbose_name=_("سعر الوحدة"))
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'),
        verbose_name=_("الإجمالي"))
    sort_order = models.PositiveSmallIntegerField(default=0, verbose_name=_("الترتيب"))

    class Meta:
        verbose_name = _("بند عرض")
        verbose_name_plural = _("بنود العرض")
        ordering = ['sort_order', 'id']

    def save(self, *args, **kwargs):
        self.line_total = self.quantity * self.unit_price
        super().save(*args, **kwargs)
        # Trigger parent recalc
        if self.quotation_id:
            self.quotation.recalc_totals()


# =====================================================================
# 🖼️ 11. البروفات وموافقة العميل (Proofs)
# =====================================================================

class PrintProof(models.Model):
    """
    بروفة تصميم لمهمة — العميل بيفتحها من رابط ويوافق أو يطلب تعديل.

    الموافقة بتنقل المهمة لمرحلة الطباعة، وطلب التعديل بيرجّعها للتصميم.
    """
    STATUS_CHOICES = (
        ('pending', _('في انتظار رد العميل')),
        ('approved', _('العميل وافق')),
        ('changes_requested', _('العميل طلب تعديل')),
    )
    job = models.ForeignKey(PrintJob, on_delete=models.CASCADE, related_name='proofs', verbose_name=_("المهمة"))
    version = models.PositiveSmallIntegerField(default=1, editable=False, verbose_name=_("رقم النسخة"))
    file = models.FileField(upload_to='print_proofs/%Y/%m/', verbose_name=_("ملف البروفة"),
                            help_text=_("صورة أو PDF للتصميم زي ما هيتطبع."))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات للعميل"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', editable=False,
                              db_index=True, verbose_name=_("الحالة"))
    customer_comment = models.TextField(blank=True, editable=False, verbose_name=_("تعليق العميل"))
    share_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name=_("توكن المشاركة"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))
    responded_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("تاريخ رد العميل"))

    class Meta:
        verbose_name = _("بروفة")
        verbose_name_plural = _("البروفات")
        ordering = ['-created_at']

    def __str__(self):
        return f"بروفة {self.version} — {self.job.description[:40]}"

    @property
    def is_image(self):
        return (self.file.name or '').lower().rsplit('.', 1)[-1] in ('png', 'jpg', 'jpeg', 'gif', 'webp')

    @property
    def is_latest(self):
        return not PrintProof.objects.filter(job_id=self.job_id, version__gt=self.version).exists()

    def save(self, *args, **kwargs):
        from django.db import transaction
        is_new = self.pk is None
        with transaction.atomic():
            if is_new:
                PrintJob.objects.select_for_update().filter(pk=self.job_id).first()
                last = PrintProof.objects.filter(job_id=self.job_id).aggregate(v=models.Max('version'))['v']
                self.version = (last or 0) + 1
            super().save(*args, **kwargs)
            if is_new:
                job = PrintJob.objects.get(pk=self.job_id)
                if job.stage in ('design', 'proof'):
                    job.stage = 'proof'
                    job.save()

    def respond(self, action, comment=''):
        """رد العميل: approve ← المهمة تروح للطباعة، reject ← ترجع للتصميم بتعليقه."""
        from django.db import transaction
        if action not in ('approve', 'reject'):
            raise ValidationError(_("إجراء غير صالح."))
        with transaction.atomic():
            proof = PrintProof.objects.select_for_update().get(pk=self.pk)
            if proof.status != 'pending':
                raise ValidationError(_("البروفة دي اترد عليها قبل كده."))
            if not proof.is_latest:
                raise ValidationError(_("فيه نسخة أحدث من البروفة دي — افتح الرابط الجديد."))
            # 🐛 [FIX]: رابط بروفة قديم كان بيشتغل حتى بعد ما الطلب اتلغى أو اتسلّم — «موافقة»
            #    بتنقل المهمة لـ «طباعة» على طلب مقفول وبتسجّل رد على شغل مابقاش موجود.
            order_status = PrintOrder.objects.filter(jobs__pk=proof.job_id).values_list('status', flat=True).first()
            if order_status in ('cancelled', 'delivered'):
                raise ValidationError(_("الطلب ده اتقفل — البروفة مابقتش مستنية رد."))
            proof.status = 'approved' if action == 'approve' else 'changes_requested'
            proof.customer_comment = (comment or '').strip()[:2000]
            proof.responded_at = timezone.now()
            proof.save(update_fields=['status', 'customer_comment', 'responded_at'])
            job = PrintJob.objects.select_for_update().get(pk=proof.job_id)
            if action == 'approve' and job.stage in ('design', 'proof'):
                job.stage = 'printing'
                job.save()
            elif action == 'reject' and job.stage == 'proof':
                job.stage = 'design'
                job.save()
        self.refresh_from_db()
        return self


# =====================================================================
# 💳 12. الرصيد المقدّم — تسديد طلب من رصيد العميل
# =====================================================================

class PrintCreditAllocation(models.Model):
    """
    تسديد (جزء من) طلب من الرصيد المقدّم على حساب العميل — من غير ما الفلوس
    تدخل أو تطلع من الخزنة تاني (هي دخلت وقت ما العميل دفعها على الحساب).
    """
    order = models.ForeignKey(PrintOrder, on_delete=models.CASCADE, related_name='credit_allocations',
                              verbose_name=_("الطلب"))
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 validators=[MinValueValidator(Decimal('0.01'))], verbose_name=_("المبلغ"))
    date = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))

    class Meta:
        verbose_name = _("تسديد من الرصيد المقدّم")
        verbose_name_plural = _("تسديدات من الرصيد المقدّم")
        ordering = ['-date']

    def __str__(self):
        return f"{self.amount:,.2f} → #{self.order.order_number}"

    def save(self, *args, **kwargs):
        from django.db import transaction
        with transaction.atomic():
            order = PrintOrder.objects.select_for_update().select_related('customer').get(pk=self.order_id)
            # قفل العميل: تسديدين في نفس اللحظة مايصرفوش نفس الرصيد مرتين.
            customer = PrintCustomer.objects.select_for_update().get(pk=order.customer_id)
            old = (PrintCreditAllocation.objects.filter(pk=self.pk).values_list('amount', flat=True).first()
                   if self.pk else None) or Decimal('0')
            if order.status in PrintOrder.NON_DEBT_STATUSES:
                raise ValidationError(_("مينفعش تسدّد طلب مسودة أو ملغي من الرصيد."))
            order_left = order.due_amount - order.compute_paid() + old
            if self.amount > order_left:
                raise ValidationError(_("المبلغ أكبر من المتبقّي على الطلب (%(a)s).") % {'a': f'{order_left:,.2f}'})
            credit = customer.account_summary()['credit'] + old
            if self.amount > credit:
                raise ValidationError(_("الرصيد المقدّم المتاح للعميل %(a)s بس.") % {'a': f'{credit:,.2f}'})
            super().save(*args, **kwargs)

    @classmethod
    def apply_available(cls, order, user=None):
        """سدّد الطلب من رصيد العميل المقدّم بأقصى مبلغ متاح. بيرجّع المبلغ (أو صفر)."""
        if order.status in PrintOrder.NON_DEBT_STATUSES:
            return Decimal('0')
        amount = min(order.customer.account_summary()['credit'], order.due_amount - order.compute_paid())
        if amount <= 0:
            return Decimal('0')
        cls.objects.create(order=order, amount=amount, created_by=user,
                           notes=_("تسديد تلقائي من الرصيد المقدّم"))
        return amount


# =====================================================================
# ↩️ 14. الإشعار الدائن — مرتجع/مسموحات بعد التسليم
# =====================================================================

class PrintCreditNote(models.Model):
    """
    إشعار دائن على طلب اتسلّم: مرتجع أو خصم لاحق بيقلّل الإيراد ومديونية العميل.

    🐛 [FIX]: رد فلوس لعميل على طلب اتسلّم كان بيتسجّل حركة خزنة بس — الإيراد
    مابينزلش والعميل يبان كأنه مديون بالمبلغ تاني. الإشعار بيعكس الإيراد
    والضريبة، ولو اخترت خزنة المبلغ بيترد للعميل منها لوحده.
    """
    KIND_CHOICES = (
        ('return', _('مرتجع / مسموحات مبيعات')),
        ('discount', _('خصم تعجيل دفع / فروق كسور')),
        ('bad_debt', _('إعدام دين (مش هيتحصّل)')),
    )
    number = models.CharField(max_length=30, unique=True, blank=True, editable=False, verbose_name=_("رقم الإشعار"))
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='return', verbose_name=_("النوع"),
                            help_text=_("المرتجع بيقلّل الإيراد والضريبة، والخصم والإعدام بيتقيّدوا مصروف."))
    order = models.ForeignKey(PrintOrder, on_delete=models.PROTECT, null=True, blank=True,
                              related_name='credit_notes', verbose_name=_("الطلب"),
                              help_text=_("سيبه فاضي للإعدام/الخصم على الرصيد الافتتاحي أو حساب العميل كله."))
    customer = models.ForeignKey(PrintCustomer, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='credit_notes', verbose_name=_("العميل"))
    date = models.DateField(default=timezone.localdate, verbose_name=_("التاريخ"))
    reason = models.CharField(max_length=300, verbose_name=_("السبب"),
                              help_text=_("مثال: 100 كارت طلعوا بعيب طباعة واترجعوا."))
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                                 verbose_name=_("قيمة المردود (من غير ضريبة)"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                     validators=[MinValueValidator(Decimal('0.00'))],
                                     verbose_name=_("الضريبة المردودة"))
    refund_treasury = models.ForeignKey(
        PrintTreasury, on_delete=models.PROTECT, null=True, blank=True, related_name='+',
        verbose_name=_("رد الفلوس من خزنة"),
        help_text=_("سيبها فاضية لو المبلغ هيفضل رصيد للعميل (هيتخصم من مديونيته أو يبقى رصيد مقدّم)."))
    refund_txn = models.OneToOneField(PrintTransaction, on_delete=models.SET_NULL, null=True, blank=True,
                                      editable=False, related_name='+')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("إشعار دائن (مرتجع مبيعات)")
        verbose_name_plural = _("الإشعارات الدائنة (مرتجعات المبيعات)")
        ordering = ['-date', '-id']

    def __str__(self):
        ref = self.order.order_number if self.order_id else (self.customer.name if self.customer_id else '')
        return f"#{self.number} — {ref}"

    @property
    def total(self):
        return (self.amount or Decimal('0')) + (self.tax_amount or Decimal('0'))

    def clean(self):
        super().clean()
        if not self.order_id and not self.customer_id:
            raise ValidationError(_("اختار الطلب أو العميل."))
        if self.kind != 'return' and self.tax_amount:
            raise ValidationError(_("الضريبة بترجع في المرتجعات بس — الخصم والإعدام من غير ضريبة."))
        if self.kind != 'return' and self.refund_treasury_id:
            raise ValidationError(_("رد الفلوس للمرتجعات بس — الخصم والإعدام بيقفلوا المديونية من غير فلوس."))
        if not self.order_id and self.kind == 'return':
            raise ValidationError(_("المرتجع لازم يبقى على طلب."))

    def save(self, *args, **kwargs):
        from django.db import transaction
        if self.pk:
            raise ValidationError(_("الإشعار الدائن مابيتعدّلش — امسحه واعمل واحد جديد."))
        if self.order_id:
            self.customer_id = PrintOrder.objects.filter(pk=self.order_id).values_list('customer_id', flat=True).first()
        self.clean()
        with transaction.atomic():
            if self.order_id:
                order = PrintOrder.objects.select_for_update().get(pk=self.order_id)
                if order.status != 'delivered':
                    raise ValidationError(_("الإشعار الدائن بيتعمل على طلب اتسلّم بس — قبل التسليم عدّل الطلب أو الغيه."))
                room = order.net_total - order.compute_credited()
                if self.total > room:
                    raise ValidationError(_("المردودات أكبر من قيمة الطلب — المتاح %(a)s.") % {'a': f'{room:,.2f}'})
            else:
                order = None
                customer = PrintCustomer.objects.select_for_update().get(pk=self.customer_id)
                room = customer.account_summary()['balance']
                if self.total > room:
                    raise ValidationError(_("أكبر من مديونية العميل — المتاح %(a)s.") % {'a': f'{max(room, 0):,.2f}'})
            _save_with_number(self, 'number', 'CN-', super().save, *args, **kwargs)
            if self.refund_treasury_id:
                try:
                    self.refund_txn = PrintTransaction.objects.create(
                        treasury_id=self.refund_treasury_id, transaction_type='out', amount=self.total,
                        order=order, created_by=self.created_by,
                        description=f"رد فلوس — إشعار دائن #{self.number} على الطلب #{order.order_number}")
                except ValueError:
                    raise ValidationError(_("رصيد الخزنة مش كفاية لرد %(a)s.") % {'a': f'{self.total:,.2f}'})
                PrintCreditNote.objects.filter(pk=self.pk).update(refund_txn=self.refund_txn)
