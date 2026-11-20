"""🖨️ المطابع — الإنتاج: الماكينات، المصممين، أنواع البنود، أوامر وشغلانات الطباعة."""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
from .customers import PrintBranch, PrintCustomer


# =====================================================================
# 🖨️ 2. ملف الماكينة وتحليل التكاليف (Machine Cost Analyzer)
# =====================================================================

class MachineProfile(models.Model):
    """
    بطاقة تعريف كل ماكينة طباعة مع تحليل تكاليف التشغيل الحية.
    تتيح حساب التكلفة الفعلية لكل مهمة طباعة بدقة.
    """
    MACHINE_TYPE_CHOICES = (
        ('digital', _('طابعة رقمية (Digital)')),
        ('offset', _('طابعة أوفست (Offset)')),
        ('large_format', _('طابعة لارج فورمات (Wide/Large Format)')),
        ('dtf', _('طابعة DTF')),
        ('uv', _('طابعة UV')),
        ('sublimation', _('طباعة حرارية (Sublimation)')),
        ('cutter', _('ماكينة قص (Cutting Plotter)')),
        ('laminator', _('ماكينة تغليف (Laminator)')),
        ('other', _('أخرى')),
    )

    name = models.CharField(max_length=150, verbose_name=_("اسم الماكينة"))
    machine_type = models.CharField(max_length=20, choices=MACHINE_TYPE_CHOICES, default='digital', verbose_name=_("نوع الماكينة"))
    brand = models.CharField(max_length=100, blank=True, verbose_name=_("الماركة / الشركة المصنعة"))
    model_number = models.CharField(max_length=100, blank=True, verbose_name=_("رقم الموديل"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))
    is_active = models.BooleanField(default=True, verbose_name=_("تعمل حالياً"))

    # ⚡ تكاليف التشغيل (Operating Costs)
    power_consumption_kwh = models.DecimalField(
        max_digits=8, decimal_places=2, default=0,
        verbose_name=_("استهلاك الكهرباء (kWh/ساعة)"),
        help_text=_("استهلاك الماكينة بالكيلو وات في الساعة")
    )
    electricity_rate_per_kwh = models.DecimalField(
        max_digits=8, decimal_places=4, default=Decimal('2.50'),
        verbose_name=_("سعر الكيلو وات (ج.م)"),
        help_text=_("تكلفة الكيلو وات ساعة حسب شريحة الكهرباء")
    )
    hourly_labor_cost = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        verbose_name=_("تكلفة العامل/الساعة (ج.م)")
    )

    # 🎨 تتبع الأحبار (CMYK Ink Tracking)
    ink_cyan_cost_per_ml = models.DecimalField(max_digits=8, decimal_places=4, default=0, verbose_name=_("تكلفة حبر Cyan (ج.م/مل)"))
    ink_magenta_cost_per_ml = models.DecimalField(max_digits=8, decimal_places=4, default=0, verbose_name=_("تكلفة حبر Magenta (ج.م/مل)"))
    ink_yellow_cost_per_ml = models.DecimalField(max_digits=8, decimal_places=4, default=0, verbose_name=_("تكلفة حبر Yellow (ج.م/مل)"))
    ink_black_cost_per_ml = models.DecimalField(max_digits=8, decimal_places=4, default=0, verbose_name=_("تكلفة حبر Black (ج.م/مل)"))

    # 📊 إحصائيات
    total_print_hours = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("إجمالي ساعات التشغيل"))
    maintenance_due_date = models.DateField(null=True, blank=True, verbose_name=_("موعد الصيانة القادمة"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("ماكينة طباعة")
        verbose_name_plural = _("ماكينات الطباعة")
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_machine_type_display()})"

    @property
    def hourly_electricity_cost(self):
        return self.power_consumption_kwh * self.electricity_rate_per_kwh

    @property
    def hourly_operating_cost(self):
        """التكلفة الكلية للساعة = كهرباء + عمالة"""
        return self.hourly_electricity_cost + self.hourly_labor_cost

    def calculate_ink_cost(self, cyan_ml=0, magenta_ml=0, yellow_ml=0, black_ml=0):
        """حساب تكلفة الأحبار لمهمة محددة"""
        return (
            (Decimal(str(cyan_ml)) * self.ink_cyan_cost_per_ml) +
            (Decimal(str(magenta_ml)) * self.ink_magenta_cost_per_ml) +
            (Decimal(str(yellow_ml)) * self.ink_yellow_cost_per_ml) +
            (Decimal(str(black_ml)) * self.ink_black_cost_per_ml)
        )


# =====================================================================
# 🎨 3. سجل أعمال المصممين (Designer KPI Tracker)
# =====================================================================

class Designer(models.Model):
    """ملف المصمم — مرتبط بمستخدم Django"""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='designer_profile', verbose_name=_("المستخدم"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))
    specialization = models.CharField(max_length=100, blank=True, verbose_name=_("التخصص"), help_text=_("مثال: سوشيال ميديا، طباعة، ثري دي"))
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name=_("أجر الساعة (ج.م)"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("مصمم")
        verbose_name_plural = _("المصممين")

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username}"

    def get_month_stats(self, month=None, year=None):
        """إحصائيات الشهر: عدد الأعمال، متوسط التقييم، إجمالي الساعات"""
        today = timezone.localdate()
        m = month or today.month
        y = year or today.year
        logs = self.work_logs.filter(date__month=m, date__year=y)
        from django.db.models import Avg, Sum, Count
        return logs.aggregate(
            total_works=Count('id'),
            total_hours=Sum('duration_hours'),
            avg_rating=Avg('client_rating'),
        )


class DesignerWorkLog(models.Model):
    """
    سجل يومي لأعمال المصمم — يتتبع نوع التنفيذ وتقييم العميل.
    يُستخدم لحساب KPIs الشهرية/السنوية وتقييم الأداء.
    """
    EXECUTION_TYPE_CHOICES = (
        ('manual', _('⌨️ يدوي بالكامل')),
        ('ai_generated', _('🤖 مُنشأ بالذكاء الاصطناعي')),
        ('ai_assisted', _('🧠 مساعد بالذكاء الاصطناعي (AI + تعديل يدوي)')),
    )

    designer = models.ForeignKey(Designer, on_delete=models.CASCADE, related_name='work_logs', verbose_name=_("المصمم"))
    date = models.DateField(default=timezone.now, verbose_name=_("التاريخ"))
    title = models.CharField(max_length=200, verbose_name=_("عنوان العمل"), help_text=_("مثال: بوستر افتتاح فرع جديد"))
    description = models.TextField(blank=True, verbose_name=_("تفاصيل"))
    customer = models.ForeignKey(PrintCustomer, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("العميل"))

    execution_type = models.CharField(max_length=15, choices=EXECUTION_TYPE_CHOICES, default='manual', verbose_name=_("نوع التنفيذ"))
    duration_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name=_("مدة العمل (ساعات)"))

    # تقييم العميل عبر واتساب (1-5 نجوم)
    client_rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        verbose_name=_("تقييم العميل (1-5)"),
        help_text=_("تقييم العميل عبر واتساب أو مباشرة (1 = ضعيف، 5 = ممتاز)")
    )
    client_feedback = models.TextField(blank=True, verbose_name=_("ملاحظات العميل"))

    # مرفقات
    preview_image = models.ImageField(upload_to='designer_works/%Y/%m/', blank=True, verbose_name=_("صورة العمل"))

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("سجل عمل مصمم")
        verbose_name_plural = _("سجلات أعمال المصممين")
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.designer} — {self.title} ({self.get_execution_type_display()})"


# =====================================================================
# 🏷️ 3.5. أنواع البنود (Product Types) — للتقارير والـ Autocomplete
# =====================================================================

class ProductType(models.Model):
    """
    نوع البند (تيشرت، كارت بزنس، بنر، ماج، فلاير، إلخ).
    المستخدم يكتب اسم البند وبيتحفظ — ويظهر autocomplete بعد كده.
    يُستخدم في تقارير آخر السنة لمعرفة أكتر بند شغال.
    """
    name = models.CharField(max_length=150, unique=True, verbose_name=_("اسم البند"))
    usage_count = models.PositiveIntegerField(default=0, editable=False, verbose_name=_("عدد مرات الاستخدام"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("نوع بند")
        verbose_name_plural = _("أنواع البنود")
        ordering = ['-usage_count', 'name']

    def __str__(self):
        return self.name


# =====================================================================
# 📋 4. أوامر الطباعة (Print Orders & Jobs)
# =====================================================================

class PrintOrder(models.Model):
    """
    طلب طباعة من عميل — يمكن أن يحتوي على عدة مهام (PrintJob).
    """
    STATUS_CHOICES = (
        ('draft', _('مسودة')),
        ('confirmed', _('مؤكد')),
        ('in_progress', _('قيد التنفيذ')),
        ('ready', _('جاهز للتسليم')),
        ('delivered', _('تم التسليم')),
        ('cancelled', _('ملغي')),
    )
    # الحالات اللي الطلب فيها مش مديونية على العميل.
    NON_DEBT_STATUSES = ('draft', 'cancelled')

    order_number = models.CharField(max_length=30, unique=True, verbose_name=_("رقم الطلب"))
    customer = models.ForeignKey(PrintCustomer, on_delete=models.PROTECT, verbose_name=_("العميل"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='draft', verbose_name=_("الحالة"))

    # تواريخ
    # 📅 تاريخ الفاتورة — بقى يتعدّل (فواتير قديمة من الورق مثلاً). كان auto_now_add فمكانش
    #    فيه أي طريقة تغيّره. التعديل بيترفض لو الفاتورة أو التاريخ الجديد في فترة مقفولة،
    #    وقيد الطلب بيتنقل معاه (printing.ledger._move_dates).
    date_created = models.DateTimeField(default=timezone.now, verbose_name=_("تاريخ الفاتورة"))
    # 🐛 [FIX]: تاريخ الفاتورة بيتعدّل بإيد المستخدم — حدود الباقة الشهرية (فواتير/طلبات) كانت
    #    بتتحسب بيه، فترجيع التاريخ لشهر فات كان بيعدّي الحد. ده وقت الإنشاء الفعلي ومابيتعدّلش.
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True,
                                      verbose_name=_("وقت الإنشاء"))
    date_due = models.DateTimeField(null=True, blank=True, verbose_name=_("موعد التسليم"))
    date_delivered = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ التسليم الفعلي"))
    date_cancelled = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("تاريخ الإلغاء"))

    # مالي
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الإجمالي"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الخصم"))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("المدفوع"))
    credited_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, editable=False,
        verbose_name=_("المردودات (إشعارات دائنة)"),
        help_text=_("مجموع الإشعارات الدائنة على الطلب بعد التسليم — بتنزل من المستحق على العميل."))
    tax_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name=_("الضريبة"),
        help_text=_("مبلغ الضريبة المستحق على العميل (بيتنقل تلقائياً من عرض السعر). مش بيتحسب ضمن الربح."))

    # 📁 ملفات المشروع
    project_file = models.FileField(
        upload_to='print_projects/%Y/%m/', blank=True,
        verbose_name=_("ملف المشروع"),
        help_text=_("ملف التصميم الأصلي (PSD, AI, PDF, إلخ)")
    )
    project_file_2 = models.FileField(
        upload_to='print_projects/%Y/%m/', blank=True,
        verbose_name=_("ملف إضافي 1")
    )
    project_file_3 = models.FileField(
        upload_to='print_projects/%Y/%m/', blank=True,
        verbose_name=_("ملف إضافي 2")
    )

    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))

    class Meta:
        # 🐛 [FIX]: صاحب المطبعة بيدوّر على «فواتير البيع» في القائمة ومش لاقيها — الطلب هو الفاتورة.
        verbose_name = _("فاتورة بيع / طلب")
        verbose_name_plural = _("فواتير البيع والطلبات")
        ordering = ['-date_created']

    def __str__(self):
        return f"#{self.order_number} — {self.customer.name}"

    @property
    def revenue(self):
        """إيراد الطلب الفعلي = الإجمالي − الخصم (الضريبة مش إيراد)."""
        return self.total_amount - self.discount

    @property
    def net_total(self):
        """المستحق على العميل = الإجمالي − الخصم + الضريبة."""
        return self.total_amount - self.discount + self.tax_amount

    @property
    def due_amount(self):
        """المستحق فعلاً بعد المردودات (الإشعارات الدائنة)."""
        return self.net_total - self.credited_amount

    @property
    def remaining(self):
        return self.due_amount - self.paid_amount

    def compute_paid(self):
        """المدفوع على الطلب من مصدره: دفعات الخزنة (إيداع − استرداد) + اللي اتسدّد من رصيد العميل المقدّم."""
        from .treasury import PrintTransaction  # استيراد محلي لتفادي الدائرية
        if not self.pk:
            return Decimal('0')
        agg = PrintTransaction.objects.filter(order_id=self.pk).aggregate(
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')),
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')))
        allocated = self.credit_allocations.aggregate(t=models.Sum('amount'))['t'] or Decimal('0')
        paid = (agg['ins'] or Decimal('0')) - (agg['outs'] or Decimal('0')) + allocated
        return max(paid, Decimal('0'))

    def compute_credited(self):
        """مجموع الإشعارات الدائنة (مردودات/مسموحات) على الطلب بالضريبة."""
        if not self.pk:
            return Decimal('0')
        agg = self.credit_notes.aggregate(a=models.Sum('amount'), t=models.Sum('tax_amount'))
        return (agg['a'] or Decimal('0')) + (agg['t'] or Decimal('0'))

    @property
    def material_cost(self):
        """تكلفة الخامات المصروفة على كل مهام الطلب (بسعرها وقت الصرف)."""
        from .materials import PrintJobMaterial  # استيراد محلي لتفادي الدائرية
        if not self.pk:
            return Decimal('0')
        total = PrintJobMaterial.objects.filter(job__order_id=self.pk).aggregate(
            t=models.Sum(models.F('quantity') * models.F('unit_cost'),
                         output_field=models.DecimalField(max_digits=14, decimal_places=2)),
        )['t']
        return (total or Decimal('0')).quantize(Decimal('0.01'))

    def clean(self):
        """📅 تعديل تاريخ الفاتورة من الأدمن: فترة مقفولة ⇒ رسالة على الخانة (مش 500)."""
        super().clean()
        if self.pk and self.date_created:
            old = PrintOrder.objects.filter(pk=self.pk).values_list('date_created', flat=True).first()
            if old is not None and old != self.date_created:
                from printing import ledger
                try:
                    ledger.check_redate(ledger.ref_order(self.pk), self.date_created)
                except ValidationError as exc:
                    raise ValidationError({'date_created': exc.messages})

    def apply_credit_hold(self, previous_status=None):
        """
        💳 لو الطلب اتأكد (خرج من المسودة) والعميل عدّى حد الائتمان ← يرجع مسودة.

        بيرجّع رسالة التحذير لو اترجّع، وإلا None.
        """
        if self.status in self.NON_DEBT_STATUSES or previous_status not in (None, 'draft'):
            return None
        msg = self.customer.over_credit_limit()
        if not msg:
            return None
        self.status = 'draft'
        PrintOrder.objects.filter(pk=self.pk).update(status='draft')
        from printing import ledger
        ledger.sync_order(self)
        return f"{msg} — الطلب #{self.order_number} اتحفظ مسودة."

    @property
    def total_cost(self):
        """مجموع تكلفة كل المهام (ماكينة + حبر + خامات + مصمم).
        للمهام المكتملة بنستخدم actual_cost (snapshot)، وللباقي full_cost الحي."""
        total = Decimal('0')
        for job in self.jobs.all():
            if job.is_complete and job.actual_cost:
                total += job.actual_cost + job.designer_cost
            else:
                total += job.full_cost
        return total

    @property
    def gross_profit(self):
        return self.revenue - self.total_cost

    @property
    def profit_margin_percent(self):
        if not self.revenue:
            return Decimal('0')
        return (self.gross_profit / self.revenue) * Decimal('100')

    @property
    def is_profitable(self):
        return self.gross_profit > 0

    def recalc_total_from_jobs(self):
        """
        Keep ``total_amount`` equal to the sum of the jobs' prices.

        🐛 [FIX]: الإجمالي كان بيتكتب يدوي ومش مربوط بالمهام — كل مهمة بتحسب
        سعرها، لكن إجمالي الطلب (اللي عليه الربح والمتبقي وكشف الحساب) كان
        بيفضل زي ما اتكتب، غالباً صفر. لو المهام كلها من غير سعر بنسيب
        الإجمالي اليدوي زي ما هو (طلبات قديمة أو محوّلة من عرض سعر).
        """
        if not self.pk:
            return
        jobs_total = self.jobs.aggregate(t=models.Sum('total_price'))['t'] or Decimal('0')
        if jobs_total > 0 and jobs_total != self.total_amount:
            self.total_amount = jobs_total
            PrintOrder.objects.filter(pk=self.pk).update(total_amount=jobs_total)

    def save(self, *args, **kwargs):
        # 🐛 [FIX]: نسخة قديمة من الطلب في الذاكرة (اتحمّلت قبل إضافة مهمة أو دفعة)
        #    كانت بتكتب فوق الإجمالي والمدفوع المحسوبين من المهام والدفعات — فطلب
        #    يتسلّم بعد دفعة بيطلع بإيراد صفر أو مدفوع قديم. الاتنين بيتحسبوا من مصدرهم.
        if self.pk:
            jobs_total = self.jobs.aggregate(t=models.Sum('total_price'))['t'] or Decimal('0')
            if jobs_total > 0:
                self.total_amount = jobs_total
            self.paid_amount = self.compute_paid()
            self.credited_amount = self.compute_credited()
        # 📅 تاريخ الفاتورة اتغيّر ⇒ فحص الفترات المقفولة، ولو الفاتورة اتسلّمت في نفس يوم
        #    إنشائها (فاتورة فورية) تاريخ التسليم (= تاريخ الإيراد) يتنقل معاها.
        if self.pk and self.date_created is not None:
            uf = kwargs.get('update_fields')
            if uf is None or 'date_created' in uf:
                old = (PrintOrder.objects.filter(pk=self.pk)
                       .values('date_created', 'date_delivered').first())
                if old and old['date_created'] != self.date_created:
                    from printing import ledger
                    ledger.check_redate(ledger.ref_order(self.pk), self.date_created)
                    same_day = (old['date_delivered'] is not None and
                                timezone.localtime(old['date_delivered']).date()
                                == timezone.localtime(old['date_created']).date())
                    if same_day and self.date_delivered == old['date_delivered']:
                        self.date_delivered = self.date_created
                        if uf is not None:
                            kwargs['update_fields'] = list(set(uf) | {'date_delivered'})
        # 📦 سجّل تاريخ التسليم الفعلي أول ما الطلب يتقفل كـ "تم التسليم".
        if self.status == 'delivered' and not self.date_delivered:
            self.date_delivered = timezone.now()
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'date_delivered'})
        # 🗓️ تاريخ الإلغاء — الخامات المصروفة على الطلب بتتقيّد هالك بالتاريخ ده.
        if self.status == 'cancelled' and not self.date_cancelled:
            self.date_cancelled = timezone.now()
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'date_cancelled'})
        elif self.status != 'cancelled' and self.date_cancelled:
            self.date_cancelled = None
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'date_cancelled'})
        if not self.order_number:
            from django.db.models import Max
            from django.db import IntegrityError as _IE, transaction as _txn
            today = timezone.localdate().strftime('%y%m%d')
            prefix = f'PO-{today}-'
            for _attempt in range(5):
                last_order = (
                    PrintOrder.objects
                    .filter(order_number__startswith=prefix)
                    .aggregate(max_num=Max('order_number'))
                )['max_num']
                if last_order:
                    try:
                        last_seq = int(last_order.split('-')[-1])
                    except (ValueError, IndexError):
                        last_seq = 0
                else:
                    last_seq = 0
                self.order_number = f'{prefix}{last_seq + 1:03d}'
                try:
                    # 🐛 [FIX]: من غير savepoint، أول IntegrityError جوه transaction
                    # (الأدمن كله atomic) كان بيبوّظ الـ transaction والمحاولة التانية تقع.
                    with _txn.atomic():
                        super().save(*args, **kwargs)
                    return
                except _IE:
                    self.order_number = ''
                    continue
            raise _IE('تعذّر توليد رقم طلب فريد — حاول مرة أخرى.')
        super().save(*args, **kwargs)

    def sync_status_from_jobs(self):
        """Move the order forward as its jobs get done — never backwards.

        🐛 [FIX]: مفيش أي حاجة كانت بتحرّك الطلب لـ «قيد التنفيذ» أو «جاهز
        للتسليم» — فاللوحة والمتابعة مابتعرفش الشغل وصل لفين. أول مهمة تخلص
        (أو خامة تتصرف) ⇒ قيد التنفيذ، وكل المهام تخلص ⇒ جاهز للتسليم.
        🏭 ومع مراحل الإنتاج: أي مهمة تعدّي «تصميم» (بروفة/طباعة/تشطيب) ⇒ قيد التنفيذ.
        """
        from .materials import PrintJobMaterial  # استيراد محلي لتفادي الدائرية
        if not self.pk or self.status not in ('draft', 'confirmed', 'in_progress'):
            return None
        jobs = list(self.jobs.values_list('stage', 'is_complete'))
        if not jobs:
            return None
        if all(done or stage == 'done' for stage, done in jobs):
            new = 'ready'
        elif (any(done or stage != 'design' for stage, done in jobs)
              or PrintJobMaterial.objects.filter(job__order_id=self.pk).exists()):
            new = 'in_progress'
        else:
            return None
        if new == self.status:
            return None
        if self.status == 'draft' and self.customer.credit_limit and \
                self.customer.account_summary()['balance'] + self.net_total > self.customer.credit_limit:
            return None   # 💳 مسودة متعلّقة على حد الائتمان مابتدخلش الإنتاج لوحدها
        self.status = new
        PrintOrder.objects.filter(pk=self.pk).update(status=new)
        return new


class PrintJob(models.Model):
    """
    مهمة طباعة واحدة داخل طلب — تربط الماكينة بالمنتج المطبوع.
    """
    PAPER_SIZE_CHOICES = (
        ('a0', 'A0'), ('a1', 'A1'), ('a2', 'A2'), ('a3', 'A3'), ('a4', 'A4'), ('a5', 'A5'),
        ('b1', 'B1'), ('b2', 'B2'),
        ('roll_60', _('رول 60 سم')), ('roll_90', _('رول 90 سم')),
        ('roll_120', _('رول 120 سم')), ('roll_150', _('رول 150 سم')),
        ('custom', _('مقاس مخصص')),
    )

    order = models.ForeignKey(PrintOrder, on_delete=models.CASCADE, related_name='jobs', verbose_name=_("الطلب"))
    machine = models.ForeignKey(MachineProfile, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الماكينة"))
    designer = models.ForeignKey(Designer, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("المصمم"))

    # 🏷️ نوع البند — بدل مقاس الورق بس (تيشرت، كارت، بنر، إلخ)
    product_type = models.ForeignKey(
        ProductType, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name=_("نوع البند"),
        help_text=_("مثال: تيشرت، كارت بزنس، بنر، ماج، فلاير")
    )
    product_type_text = models.CharField(
        max_length=150, blank=True, verbose_name=_("نوع البند (نص حر)"),
        help_text=_("اكتب نوع البند — لو موجود قبل كده هيظهرلك autocomplete")
    )

    description = models.CharField(max_length=300, verbose_name=_("وصف المهمة"), help_text=_("مثال: طباعة 500 كارت بزنس، سوفت تاتش"))
    paper_size = models.CharField(max_length=10, choices=PAPER_SIZE_CHOICES, default='a4', blank=True, verbose_name=_("مقاس الورق"))
    custom_width_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, verbose_name=_("العرض (سم)"))
    custom_height_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, verbose_name=_("الارتفاع (سم)"))
    quantity = models.PositiveIntegerField(default=1, verbose_name=_("الكمية"))
    copies = models.PositiveIntegerField(default=1, verbose_name=_("عدد النسخ"))

    # تسعير
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name=_("سعر الوحدة"))
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("الإجمالي"))

    # تكلفة فعلية
    actual_time_hours = models.DecimalField(max_digits=6, decimal_places=2, default=0, verbose_name=_("وقت التنفيذ الفعلي (ساعات)"))
    ink_cyan_ml = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name=_("حبر Cyan (مل)"))
    ink_magenta_ml = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name=_("حبر Magenta (مل)"))
    ink_yellow_ml = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name=_("حبر Yellow (مل)"))
    ink_black_ml = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name=_("حبر Black (مل)"))

    # مرفقات
    design_file = models.FileField(upload_to='print_jobs/%Y/%m/', blank=True, verbose_name=_("ملف التصميم"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    is_complete = models.BooleanField(default=False, verbose_name=_("مكتملة"))
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الإكمال"))

    # 🏭 مراحل الإنتاج: تصميم ← بروفة (موافقة العميل) ← طباعة ← تشطيب ← خلصت
    STAGE_CHOICES = (
        ('design', _('تصميم')),
        ('proof', _('بروفة — في انتظار موافقة العميل')),
        ('printing', _('طباعة')),
        ('finishing', _('تشطيب')),
        ('done', _('خلصت')),
    )
    STAGE_FLOW = ('design', 'proof', 'printing', 'finishing', 'done')
    PRODUCTION_STAGES = ('printing', 'finishing', 'done')
    stage = models.CharField(max_length=12, choices=STAGE_CHOICES, default='design', db_index=True,
                             verbose_name=_("مرحلة الإنتاج"))
    proof_waived = models.BooleanField(
        default=False, verbose_name=_("من غير بروفة"),
        help_text=_("العميل جايب ملف جاهز للطباعة أو إعادة طبع — مش محتاج موافقة على بروفة."))
    stage_changed_at = models.DateTimeField(null=True, blank=True, editable=False,
                                            verbose_name=_("آخر تغيير للمرحلة"))

    # 📊 لقطة التكاليف المحفوظة (تُثبَّت عند إكمال المهمة)
    actual_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False, verbose_name=_("التكلفة الفعلية المحفوظة"))
    actual_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False, verbose_name=_("صافي الربح المحفوظ"))

    class Meta:
        verbose_name = _("مهمة طباعة")
        verbose_name_plural = _("مهام الطباعة")

    def __str__(self):
        return f"{self.description[:50]} — {self.order.order_number}"

    @property
    def machine_cost(self):
        if not self.machine:
            return Decimal('0')
        return self.machine.hourly_operating_cost * self.actual_time_hours

    @property
    def ink_cost(self):
        if not self.machine:
            return Decimal('0')
        # 🐛 [FIX]: لو الحبر اتصرف على المهمة كخامة من المخزن، تكلفته داخلة في الخامات —
        #    حسابه تاني من مللي الماكينة كان بيحسب الحبر مرتين وربح الطلب يطلع أقل.
        if self.pk and self.material_usages.filter(material__category='ink').exists():
            return Decimal('0')
        return self.machine.calculate_ink_cost(
            self.ink_cyan_ml, self.ink_magenta_ml,
            self.ink_yellow_ml, self.ink_black_ml
        )

    @property
    def designer_cost(self):
        if not self.designer or not self.actual_time_hours:
            return Decimal('0')
        return self.designer.hourly_rate * self.actual_time_hours

    @property
    def material_cost(self):
        """تكلفة الخامات المستهلكة (ورق/فينيل/بنر…) — بسعرها وقت الصرف."""
        if not self.pk:
            return Decimal('0')
        total = self.material_usages.aggregate(
            t=models.Sum(models.F('quantity') * models.F('unit_cost'),
                         output_field=models.DecimalField(max_digits=14, decimal_places=2)),
        )['t']
        return (total or Decimal('0')).quantize(Decimal('0.01'))

    @property
    def calculated_cost(self):
        """التكلفة الفعلية الحية = تشغيل الماكينة + أحبار + خامات"""
        return self.machine_cost + self.ink_cost + self.material_cost

    @property
    def full_cost(self):
        """التكلفة الكاملة = ماكينة + حبر + خامات + أجر المصمم"""
        return self.calculated_cost + self.designer_cost

    @property
    def profit(self):
        """الربح الحي = السعر - التكلفة الفعلية"""
        return self.total_price - self.calculated_cost

    @property
    def full_profit(self):
        """صافي الربح بعد خصم أجر المصمم"""
        return self.total_price - self.full_cost

    @property
    def has_approved_proof(self):
        return bool(self.pk) and self.proofs.filter(status='approved').exists()

    @property
    def can_go_to_production(self):
        """الطباعة تبدأ بس بعد موافقة العميل على البروفة (أو لو البروفة مش لازمة)."""
        return self.proof_waived or self.has_approved_proof

    @property
    def next_stage(self):
        idx = self.STAGE_FLOW.index(self.stage) if self.stage in self.STAGE_FLOW else 0
        return self.STAGE_FLOW[idx + 1] if idx + 1 < len(self.STAGE_FLOW) else None

    def clean(self):
        super().clean()
        if (self.stage in self.PRODUCTION_STAGES or self.is_complete) and not self.can_go_to_production:
            raise ValidationError(
                _("«%(job)s»: مينفعش تبدأ الطباعة قبل ما العميل يوافق على البروفة — "
                  "ابعتله البروفة أو علّم «من غير بروفة».") % {'job': self.description[:60]})

    def advance_stage(self):
        """نقل المهمة للمرحلة اللي بعدها (بيرفض الطباعة من غير بروفة موافق عليها)."""
        nxt = self.next_stage
        if nxt is None:
            raise ValidationError(_("المهمة خلصت بالفعل."))
        if nxt == 'proof' and self.proof_waived:
            nxt = 'printing'
        if nxt in self.PRODUCTION_STAGES and not self.can_go_to_production:
            raise ValidationError(_("«%(job)s» مستنية موافقة العميل على البروفة.") % {'job': self.description[:60]})
        self.stage = nxt
        self.save()
        return nxt

    def _sync_stage_and_completion(self):
        """«خلصت» = مكتملة والعكس — اللي اتغيّر فيهم هو اللي بيحدد التاني."""
        old = (PrintJob.objects.filter(pk=self.pk).values('stage', 'is_complete').first()
               if self.pk else None)
        if old and old['is_complete'] != self.is_complete:
            if self.is_complete:
                self.stage = 'done'
            elif self.stage == 'done':
                self.stage = 'finishing'
        elif old is None or old['stage'] != self.stage:
            if self.stage == 'done':
                self.is_complete = True
            elif old is not None:
                self.is_complete = False
            elif self.is_complete:
                self.stage = 'done'
        if old is None or old['stage'] != self.stage:
            self.stage_changed_at = timezone.now()

    def save(self, *args, **kwargs):
        self._sync_stage_and_completion()
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = list(set(kwargs['update_fields'])
                                           | {'stage', 'is_complete', 'stage_changed_at'})
        # B4: دائماً أعد حساب total_price عند وجود unit_price
        self.total_price = self.unit_price * self.quantity * self.copies

        # 🏷️ Auto-create/link ProductType من النص الحر
        if self.product_type_text and not self.product_type:
            pt, created = ProductType.objects.get_or_create(
                name__iexact=self.product_type_text.strip(),
                defaults={'name': self.product_type_text.strip()}
            )
            self.product_type = pt
        # تحديث عداد الاستخدام
        if self.product_type and self.pk is None:
            ProductType.objects.filter(pk=self.product_type.pk).update(
                usage_count=models.F('usage_count') + 1
            )

        # B3: ثبّت التكاليف عند إكمال المهمة (snapshot) — مرة واحدة فقط
        # 🚀 [FIX BY QA]: اللقطة تُحفظ فقط عند أول إكمال، لا تُعاد كتابتها لاحقاً
        first_completion = self.is_complete and not self.completed_at
        if first_completion:
            self.actual_cost = self.calculated_cost
            self.actual_profit = self.total_price - self.actual_cost
            self.completed_at = timezone.now()

        super().save(*args, **kwargs)

        # 🐛 [FIX]: «إجمالي ساعات التشغيل» للماكينة عمره ما اتحرّك — فمتابعة
        # الصيانة بالساعات مالهاش معنى. كل مهمة بتخلص بتزوّد ساعاتها مرة واحدة.
        if first_completion and self.machine_id and self.actual_time_hours:
            MachineProfile.objects.filter(pk=self.machine_id).update(
                total_print_hours=models.F('total_print_hours') + self.actual_time_hours)
