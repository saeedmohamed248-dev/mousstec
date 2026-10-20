"""
🎨 Mousstec Printing & Design Module
=====================================
نظام إدارة المطابع واستوديوهات التصميم — معزول تماماً عن قطاع السيارات.
كل tenant في قطاع الطباعة يحصل على هذه الجداول في الـ schema الخاص به.
"""
import uuid
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator


# =====================================================================
# 🏢 1. الفروع والعملاء (Branch & Customer — مستقل عن inventory)
# =====================================================================

class PrintBranch(models.Model):
    """فرع المطبعة / الاستوديو"""
    name = models.CharField(max_length=100, verbose_name=_("اسم الفرع"))
    address = models.TextField(blank=True, verbose_name=_("العنوان"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("الهاتف"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("فرع")
        verbose_name_plural = _("الفروع")
        ordering = ['name']

    def __str__(self):
        return self.name


class PrintCustomer(models.Model):
    """عملاء المطبعة / الاستوديو"""
    name = models.CharField(max_length=150, verbose_name=_("اسم العميل"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("الهاتف"))
    whatsapp = models.CharField(max_length=20, blank=True, verbose_name=_("واتساب"))
    email = models.EmailField(blank=True, verbose_name=_("البريد الإلكتروني"))
    company = models.CharField(max_length=150, blank=True, verbose_name=_("اسم الشركة"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name=_("حد الائتمان"),
        help_text=_("أقصى مديونية مسموحة على العميل — الطلب اللي يعدّيها بيفضل مسودة لحد ما يدفع. 0 = من غير حد."))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("عميل")
        verbose_name_plural = _("العملاء")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.company})" if self.company else self.name

    def payments_qs(self):
        """كل فلوس العميل: دفعات على طلباته + دفعات على حسابه (من غير طلب)."""
        return PrintTransaction.objects.filter(
            models.Q(order__customer=self) | models.Q(order__isnull=True, customer=self))

    def account_summary(self):
        """
        رصيد العميل على مستوى الحساب كله (مش طلب طلب).

        * due         — المستحق على الطلبات الشغالة (مش مسودة ولا ملغية)
        * payments    — صافي فلوسه اللي دخلت الخزنة (دفعات − مردودات)
        * balance     — due − payments: موجب = عليه، سالب = ليه رصيد
        * outstanding — المتبقّي على طلباته بعد اللي اتسدّد عليها
        * credit      — رصيد مقدّم متاح (فلوس دافعها ومش متسدّدة على طلب)
        """
        zero = Decimal('0')
        due = applied = outstanding = zero
        for o in self.printorder_set.only('status', 'total_amount', 'discount', 'tax_amount', 'paid_amount'):
            o_due = o.net_total if o.status not in PrintOrder.NON_DEBT_STATUSES else zero
            due += o_due
            outstanding += max(zero, o_due - o.paid_amount)
            applied += max(zero, min(o.paid_amount, o_due))
        agg = self.payments_qs().aggregate(
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')),
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')))
        payments = (agg['ins'] or zero) - (agg['outs'] or zero)
        return {
            'due': due, 'payments': payments, 'balance': due - payments,
            'outstanding': outstanding, 'credit': max(zero, payments - applied),
        }

    @property
    def balance(self):
        return self.account_summary()['balance']

    @property
    def available_credit(self):
        return self.account_summary()['credit']

    def over_credit_limit(self):
        """رسالة لو مديونية العميل عدّت حد الائتمان، وإلا None (0 = من غير حد)."""
        if not self.credit_limit or self.credit_limit <= 0:
            return None
        balance = self.account_summary()['balance']
        if balance <= self.credit_limit:
            return None
        return (f"العميل «{self.name}» عدّى حد الائتمان: المديونية {balance:,.2f} "
                f"والحد {self.credit_limit:,.2f}. حصّل دفعة أو ارفع الحد الأول.")


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
    created_at = models.DateTimeField(auto_now_add=True)

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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("مصمم")
        verbose_name_plural = _("المصممين")

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username}"

    def get_month_stats(self, month=None, year=None):
        """إحصائيات الشهر: عدد الأعمال، متوسط التقييم، إجمالي الساعات"""
        today = timezone.now().date()
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

    created_at = models.DateTimeField(auto_now_add=True)

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
    created_at = models.DateTimeField(auto_now_add=True)

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
    date_created = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإنشاء"))
    date_due = models.DateTimeField(null=True, blank=True, verbose_name=_("موعد التسليم"))
    date_delivered = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ التسليم الفعلي"))
    date_cancelled = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("تاريخ الإلغاء"))

    # مالي
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الإجمالي"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الخصم"))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("المدفوع"))
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
        verbose_name = _("طلب طباعة")
        verbose_name_plural = _("طلبات الطباعة")
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
    def remaining(self):
        return self.net_total - self.paid_amount

    def compute_paid(self):
        """المدفوع على الطلب من مصدره: دفعات الخزنة (إيداع − استرداد) + اللي اتسدّد من رصيد العميل المقدّم."""
        if not self.pk:
            return Decimal('0')
        agg = PrintTransaction.objects.filter(order_id=self.pk).aggregate(
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')),
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')))
        allocated = self.credit_allocations.aggregate(t=models.Sum('amount'))['t'] or Decimal('0')
        paid = (agg['ins'] or Decimal('0')) - (agg['outs'] or Decimal('0')) + allocated
        return max(paid, Decimal('0'))

    @property
    def material_cost(self):
        """تكلفة الخامات المصروفة على كل مهام الطلب (بسعرها وقت الصرف)."""
        if not self.pk:
            return Decimal('0')
        total = PrintJobMaterial.objects.filter(job__order_id=self.pk).aggregate(
            t=models.Sum(models.F('quantity') * models.F('unit_cost'),
                         output_field=models.DecimalField(max_digits=14, decimal_places=2)),
        )['t']
        return (total or Decimal('0')).quantize(Decimal('0.01'))

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


# =====================================================================
# 📦 5. المخزون (خامات الطباعة)
# =====================================================================

class PrintMaterial(models.Model):
    """خامات الطباعة: ورق، حبر، فينيل، بنر، إلخ"""
    CATEGORY_CHOICES = (
        ('paper', _('ورق')),
        ('ink', _('حبر')),
        ('vinyl', _('فينيل')),
        ('banner', _('بنر / فليكس')),
        ('lamination', _('تغليف / لامينيشن')),
        ('packaging', _('تغليف وتعبئة')),
        ('other', _('خامات أخرى')),
    )

    name = models.CharField(max_length=200, verbose_name=_("اسم الخامة"))
    category = models.CharField(max_length=15, choices=CATEGORY_CHOICES, default='paper', verbose_name=_("التصنيف"))
    sku = models.CharField(max_length=50, blank=True, verbose_name=_("كود الخامة"))
    unit = models.CharField(max_length=30, default='قطعة', verbose_name=_("وحدة القياس"), help_text=_("مثال: رول، ورقة، لتر، مل"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))

    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الكمية الحالية"))
    min_stock = models.DecimalField(max_digits=12, decimal_places=2, default=5, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("الحد الأدنى"))
    # 4 خانات عشرية: متوسط تكلفة الفرخ/المتر بيطلع كسور (1.234) — خانتين بيضيّعوا قروش في كل شراء.
    cost_per_unit = models.DecimalField(max_digits=14, decimal_places=4, default=0, validators=[MinValueValidator(Decimal('0.00'))], verbose_name=_("تكلفة الوحدة (ج.م)"))

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("خامة طباعة")
        verbose_name_plural = _("خامات الطباعة")
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"

    @property
    def is_low_stock(self):
        return self.quantity <= self.min_stock

    @property
    def stock_value(self):
        return self.quantity * self.cost_per_unit


class PrintJobMaterial(models.Model):
    """
    🧾 خامة اتصرفت على مهمة طباعة — بتخصم من المخزون وبتدخل في تكلفة المهمة.

    🐛 [FIX]: المخزون (ورق/فينيل/بنر) ماكانش بيتخصم من أي شغل، وتكلفته
    ماكانتش داخلة في تكلفة المهمة — فربح كل طلب كان متضخّم وعدّ المخزون
    مابيتحركش غير بالتعديل اليدوي.
    """
    job = models.ForeignKey(PrintJob, on_delete=models.CASCADE, related_name='material_usages',
                            verbose_name=_("المهمة"))
    material = models.ForeignKey(PrintMaterial, on_delete=models.PROTECT, related_name='usages',
                                 verbose_name=_("الخامة"))
    quantity = models.DecimalField(max_digits=12, decimal_places=2,
                                   validators=[MinValueValidator(Decimal('0.01'))],
                                   verbose_name=_("الكمية المصروفة"))
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0, editable=False,
                                    verbose_name=_("تكلفة الوحدة وقت الصرف"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("خامة مصروفة")
        verbose_name_plural = _("الخامات المصروفة على المهام")

    def __str__(self):
        return f"{self.material} × {self.quantity} → {self.job_id}"

    @property
    def total_cost(self):
        return ((self.quantity or Decimal('0')) * (self.unit_cost or Decimal('0'))).quantize(Decimal('0.01'))

    def save(self, *args, **kwargs):
        """Deduct (or adjust) stock atomically; refuse to go below zero."""
        from django.core.exceptions import ValidationError
        from django.db import transaction as _txn
        with _txn.atomic():
            old = None
            if self.pk:
                old = PrintJobMaterial.objects.select_for_update().filter(pk=self.pk).first()
            if old is not None:
                # Give back what the previous version of this line took.
                PrintMaterial.objects.filter(pk=old.material_id).update(
                    quantity=models.F('quantity') + old.quantity)
            mat = PrintMaterial.objects.select_for_update().get(pk=self.material_id)
            if mat.quantity < self.quantity:
                raise ValidationError(
                    f"المخزون من «{mat.name}» مش كفاية: المتاح {mat.quantity} {mat.unit}، "
                    f"المطلوب {self.quantity}."
                )
            if old is None or old.material_id != self.material_id or not self.unit_cost:
                self.unit_cost = mat.cost_per_unit
            PrintMaterial.objects.filter(pk=mat.pk).update(quantity=models.F('quantity') - self.quantity)
            super().save(*args, **kwargs)
            # A job already completed has a frozen cost snapshot — keep it true.
            delta = self.total_cost - (old.total_cost if old is not None else Decimal('0'))
            _shift_job_snapshot(self.job_id, delta)


def _shift_job_snapshot(job_id, cost_delta):
    """Add ``cost_delta`` to a completed job's frozen cost (and take it off profit)."""
    if not cost_delta:
        return
    PrintJob.objects.filter(pk=job_id, is_complete=True, completed_at__isnull=False).update(
        actual_cost=models.F('actual_cost') + cost_delta,
        actual_profit=models.F('actual_profit') - cost_delta,
    )


# =====================================================================
# 💰 6. الخزينة والمصروفات (خاصة بالمطابع)
# =====================================================================

class PrintTreasury(models.Model):
    """خزينة المطبعة"""
    name = models.CharField(max_length=100, verbose_name=_("اسم الخزينة"))
    branch = models.ForeignKey(PrintBranch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع"))
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name=_("الرصيد"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشطة"))

    class Meta:
        verbose_name = _("خزينة")
        verbose_name_plural = _("الخزائن")

    def __str__(self):
        return f"{self.name} ({self.balance:,.2f} ج.م)"


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
        if (self.order_id or self.customer_id) and (self.purchase_id or self.supplier_id):
            raise ValidationError(_("الحركة مينفعش تتربط بعميل ومورد في نفس الوقت."))
        if self.order_id and self.customer_id and self.order.customer_id != self.customer_id:
            raise ValidationError(_("العميل المختار مش هو عميل الطلب."))
        if self.purchase_id and self.supplier_id and self.purchase.supplier_id != self.supplier_id:
            raise ValidationError(_("المورد المختار مش هو مورد فاتورة الشراء."))
        if self.category == 'customer' and not (self.order_id or self.customer_id):
            raise ValidationError(_("دفعة العميل لازم تتربط بطلب أو بعميل."))
        if self.category == 'supplier' and not (self.purchase_id or self.supplier_id):
            raise ValidationError(_("دفعة المورد لازم تتربط بفاتورة شراء أو بمورد."))

    def _fill_links(self):
        """العميل/المورد من الطلب/الفاتورة، والبند من الربط أو الوصف."""
        if self.order_id:
            self.customer_id = PrintOrder.objects.filter(pk=self.order_id).values_list('customer_id', flat=True).first()
        if self.purchase_id:
            self.supplier_id = MaterialPurchase.objects.filter(pk=self.purchase_id).values_list('supplier_id', flat=True).first()
        if self.customer_id:
            self.category = 'customer'
        elif self.supplier_id:
            self.category = 'supplier'
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
            if old is not None:
                if (old['treasury_id'], old['transaction_type'], old['amount']) == \
                        (self.treasury_id, self.transaction_type, self.amount):
                    return
                PrintTreasury.objects.filter(pk=old['treasury_id']).update(
                    balance=_F('balance') - self._signed(old['transaction_type'], old['amount']))
            treasury = PrintTreasury.objects.select_for_update().get(pk=self.treasury_id)
            delta = self._signed(self.transaction_type, self.amount)
            # 🛡️ Negative balance check
            if delta < 0 and treasury.balance + delta < 0:
                raise ValueError("رصيد الخزنة لا يكفي لإتمام العملية.")
            PrintTreasury.objects.filter(pk=treasury.pk).update(balance=_F('balance') + delta)

    # Deleting a transaction reverses its balance effect in the post_delete
    # signal (printing/signals.py) — a model ``delete()`` override is skipped
    # by queryset/bulk deletes (e.g. the admin "delete selected" action), which
    # used to leave the balance wrong.


# =====================================================================
# 🔐 7. صلاحيات الموظفين (Staff Permissions)
# =====================================================================

class StaffPermission(models.Model):
    """
    صلاحيات مخصصة لكل موظف — الأدمن يتحكم مين يشوف إيه.
    بدل ما نعتمد على Django Groups/Permissions المعقدة.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='print_permissions', verbose_name=_("الموظف")
    )

    # 📊 مالي
    can_view_treasury = models.BooleanField(default=False, verbose_name=_("مشاهدة الخزينة"))
    can_manage_treasury = models.BooleanField(default=False, verbose_name=_("إدارة الخزينة (إيداع/سحب)"))
    can_view_profits = models.BooleanField(default=False, verbose_name=_("مشاهدة الأرباح والتكاليف"))

    # 📋 الطلبات
    can_create_orders = models.BooleanField(default=True, verbose_name=_("إنشاء طلبات"))
    can_edit_orders = models.BooleanField(default=True, verbose_name=_("تعديل طلبات"))
    can_delete_orders = models.BooleanField(default=False, verbose_name=_("حذف طلبات"))
    can_view_all_orders = models.BooleanField(default=True, verbose_name=_("مشاهدة كل الطلبات"))

    # 👥 العملاء
    can_manage_customers = models.BooleanField(default=True, verbose_name=_("إدارة العملاء"))

    # 📁 ملفات المشاريع
    can_view_project_files = models.BooleanField(default=False, verbose_name=_("مشاهدة ملفات المشاريع"))
    can_upload_project_files = models.BooleanField(default=True, verbose_name=_("رفع ملفات المشاريع"))

    # 📦 المخزون
    can_manage_stock = models.BooleanField(default=False, verbose_name=_("إدارة المخزون"))

    # 🎨 المصممين
    can_view_designers = models.BooleanField(default=False, verbose_name=_("مشاهدة أداء المصممين"))

    # 🤖 AI Studio
    can_use_ai_studio = models.BooleanField(default=False, verbose_name=_("استخدام AI Studio"))

    # 📊 التقارير
    can_view_reports = models.BooleanField(default=False, verbose_name=_("مشاهدة التقارير"))

    class Meta:
        verbose_name = _("صلاحية موظف")
        verbose_name_plural = _("صلاحيات الموظفين")

    def __str__(self):
        return f"صلاحيات: {self.user.get_full_name() or self.user.username}"


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
    created_at = models.DateTimeField(auto_now_add=True)
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
        return timezone.now().date() > self.valid_until

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
            self.valid_until = timezone.now().date() + _td(days=7)
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
# 🚚 9. الموردين ومشتريات الخامات (Suppliers & Material Purchases)
# =====================================================================

def _save_with_number(instance, field, prefix, save, *args, **kwargs):
    """
    Save ``instance`` with the next ``<prefix>YYMMDD-NNN`` number in ``field``.

    Two saves in the same instant read the same last number — the second one
    hits the unique constraint, so we retry inside a savepoint.
    """
    from django.db import IntegrityError, transaction
    model = type(instance)
    day_prefix = f"{prefix}{timezone.localdate():%y%m%d}-"
    for _attempt in range(5):
        last = (model.objects.filter(**{f'{field}__startswith': day_prefix})
                .order_by(f'-{field}').values_list(field, flat=True).first())
        try:
            seq = int(last.split('-')[-1]) + 1 if last else 1
        except (ValueError, IndexError):
            seq = 1
        setattr(instance, field, f'{day_prefix}{seq:03d}')
        try:
            with transaction.atomic():
                return save(*args, **kwargs)
        except IntegrityError:
            setattr(instance, field, '')
    raise ValidationError(_("تعذّر توليد رقم فريد — حاول مرة أخرى."))


class PrintSupplier(models.Model):
    """مورد خامات (ورق، أحبار، فينيل…) — رصيده = فواتير الشراء المستلمة − اللي اتدفعله."""
    name = models.CharField(max_length=150, verbose_name=_("اسم المورد"))
    company = models.CharField(max_length=150, blank=True, verbose_name=_("اسم الشركة"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("الهاتف"))
    whatsapp = models.CharField(max_length=20, blank=True, verbose_name=_("واتساب"))
    address = models.TextField(blank=True, verbose_name=_("العنوان"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("مورد")
        verbose_name_plural = _("الموردين")
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.company})" if self.company else self.name

    def payments_qs(self):
        """كل الفلوس مع المورد: على فواتيره + على حسابه (دفعة مقدّمة/تسوية)."""
        return PrintTransaction.objects.filter(
            models.Q(purchase__supplier=self) | models.Q(purchase__isnull=True, supplier=self))

    def account_summary(self):
        """purchased − paid = balance (موجب = علينا للمورد، سالب = لينا رصيد عنده)."""
        zero = Decimal('0')
        purchased = sum((p.net_total for p in self.purchases.filter(status='received')), zero)
        agg = self.payments_qs().aggregate(
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')),
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')))
        paid = (agg['outs'] or zero) - (agg['ins'] or zero)
        return {'purchased': purchased, 'paid': paid, 'balance': purchased - paid}

    @property
    def balance(self):
        return self.account_summary()['balance']


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
    created_at = models.DateTimeField(auto_now_add=True)
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
    def remaining(self):
        return self.net_total - self.paid_amount

    def compute_paid(self):
        agg = PrintTransaction.objects.filter(purchase_id=self.pk).aggregate(
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')),
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')))
        return (agg['outs'] or Decimal('0')) - (agg['ins'] or Decimal('0'))

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
            if p.status == 'received':
                lines = list(p.lines.order_by('pk'))
                effective = p.effective_line_totals(lines)
                for line in lines:
                    mat = PrintMaterial.objects.select_for_update().get(pk=line.material_id)
                    if mat.quantity < line.quantity:
                        raise ValidationError(
                            _("«%(m)s»: المتاح %(q)s أقل من كمية الفاتورة %(n)s — اتصرف منها على شغل. "
                              "اعمل تسوية مخزن بالكمية المرتجعة بدل الإلغاء.")
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
# 📦 10. تسويات المخزون (جرد / تالف) — الطريق الوحيد لتعديل الكمية بالإيد
# =====================================================================

class PrintStockAdjustment(models.Model):
    """
    تسوية كمية خامة (عجز/زيادة جرد، تالف…) — بتتقيّد فروقات جرد بتكلفة الخامة.

    🐛 [FIX]: الكمية كانت بتتكتب بالإيد في صفحة الخامة، فالمخزون بيتغيّر من غير
    أثر ولا قيمة ولا سبب. دلوقتي الكمية بتتحرك بس من: الشراء، الصرف على الشغل،
    أو تسوية مسجّلة هنا.
    """
    REASON_CHOICES = (
        ('count', _('جرد — فرق عدّ')),
        ('damage', _('تالف / هالك')),
        ('other', _('أخرى')),
    )
    material = models.ForeignKey(PrintMaterial, on_delete=models.PROTECT, related_name='adjustments',
                                 verbose_name=_("الخامة"))
    quantity_change = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("التغيير في الكمية"),
                                          help_text=_("موجب = زيادة، سالب = عجز/تالف."))
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0, editable=False,
                                    verbose_name=_("تكلفة الوحدة وقت التسوية"))
    reason = models.CharField(max_length=10, choices=REASON_CHOICES, default='count', verbose_name=_("السبب"))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))

    class Meta:
        verbose_name = _("تسوية مخزن")
        verbose_name_plural = _("تسويات المخزن (جرد / تالف)")
        ordering = ['-created_at']

    def __str__(self):
        sign = '+' if self.quantity_change > 0 else ''
        return f"{self.material} {sign}{self.quantity_change}"

    @property
    def value(self):
        """القيمة بإشارتها: موجب = زيادة في المخزون، سالب = عجز."""
        return ((self.quantity_change or Decimal('0')) * (self.unit_cost or Decimal('0'))).quantize(Decimal('0.01'))

    def save(self, *args, **kwargs):
        from django.db import transaction
        if self.pk:
            raise ValidationError(_("التسوية مابتتعدّلش — امسحها واعمل واحدة جديدة."))
        if not self.quantity_change:
            raise ValidationError(_("التغيير في الكمية مينفعش يبقى صفر."))
        with transaction.atomic():
            mat = PrintMaterial.objects.select_for_update().get(pk=self.material_id)
            if mat.quantity + self.quantity_change < 0:
                raise ValidationError(
                    _("المخزون من «%(m)s» مش كفاية: المتاح %(q)s %(u)s.")
                    % {'m': mat.name, 'q': mat.quantity, 'u': mat.unit})
            self.unit_cost = mat.cost_per_unit
            PrintMaterial.objects.filter(pk=mat.pk).update(quantity=models.F('quantity') + self.quantity_change)
            super().save(*args, **kwargs)


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
    created_at = models.DateTimeField(auto_now_add=True)
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
            order_left = order.net_total - order.compute_paid() + old
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
        amount = min(order.customer.account_summary()['credit'], order.net_total - order.compute_paid())
        if amount <= 0:
            return Decimal('0')
        cls.objects.create(order=order, amount=amount, created_by=user,
                           notes=_("تسديد تلقائي من الرصيد المقدّم"))
        return amount


# =====================================================================
# 📒 13. حالة الدفتر المحاسبي للمطبعة
# =====================================================================

class PrintLedgerState(models.Model):
    """صف واحد بيسجّل إن الأرصدة الافتتاحية والحركات القديمة اتقيّدت في الدفتر (bootstrap)."""
    bootstrapped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("حالة الدفتر المحاسبي")
        verbose_name_plural = _("حالة الدفتر المحاسبي")

    @classmethod
    def get(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj
