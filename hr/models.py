"""
👥 Mousstec HR Module — Automated Pipeline & Operations Workflow
================================================================
نظام إدارة الموارد البشرية المؤتمت — يخدم قطاعي السيارات والطباعة.

Components:
1. HRSettings          — إعدادات الشركة (geofence, خصومات, سياسات)
2. Employee            — ملف الموظف الموحد (بصمة وجه, مدير مباشر, راتب)
3. WorkShift           — ورديات العمل
4. AttendanceRecord    — سجل الحضور والانصراف الذكي
5. LeaveRequest        — طلبات الإجازات
6. Advance             — السلف والعهد
7. AdvanceInstallment  — أقساط السلف المجدولة
8. PayrollRun          — الدورة الشهرية للمرتبات
9. PayrollEntry        — كشف راتب الموظف الفردي
10. DesignSubmission   — حلقة عمل التصميم (Design Workflow)
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

import logging

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 1. إعدادات الموارد البشرية للشركة (HR Settings — per Tenant)
# =====================================================================

class HRSettings(models.Model):
    """
    إعدادات HR على مستوى الشركة (Tenant).
    واحد لكل Tenant — يتحكم في الـ Geofencing وسياسات الخصم.
    """
    # --- Geofencing ---
    geofence_latitude = models.DecimalField(
        max_digits=10, decimal_places=7, default=Decimal('30.0444196'),
        verbose_name=_("خط العرض (Latitude)"),
        help_text=_("إحداثية موقع الشركة/الورشة — خط العرض"),
    )
    geofence_longitude = models.DecimalField(
        max_digits=10, decimal_places=7, default=Decimal('31.2357116'),
        verbose_name=_("خط الطول (Longitude)"),
        help_text=_("إحداثية موقع الشركة/الورشة — خط الطول"),
    )
    geofence_radius_meters = models.PositiveIntegerField(
        default=200,
        verbose_name=_("نطاق السماح (متر)"),
        help_text=_("المسافة القصوى المسموحة لتسجيل الحضور من الموقع (بالمتر)"),
    )

    # --- سياسات الحضور والخصم ---
    grace_minutes = models.PositiveIntegerField(
        default=15,
        verbose_name=_("فترة السماح (دقيقة)"),
        help_text=_("عدد الدقائق المسموحة بعد بداية الوردية قبل احتساب التأخير"),
    )
    late_deduction_per_minute = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("خصم التأخير لكل دقيقة (ج.م)"),
        help_text=_("المبلغ المخصوم عن كل دقيقة تأخير بعد فترة السماح — 0 يعني تعطيل الخصم الدقيقي"),
    )
    late_deduction_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("نسبة خصم التأخير من اليومي (%)"),
        help_text=_("النسبة المئوية المخصومة من أجر اليوم عند التأخير — بديل عن الخصم الدقيقي"),
    )
    absence_deduction_days = models.DecimalField(
        max_digits=4, decimal_places=1, default=Decimal('1.0'),
        verbose_name=_("خصم الغياب بدون إذن (عدد أيام)"),
        help_text=_("عدد الأيام المخصومة من الراتب عند كل يوم غياب بدون إذن مسبق"),
    )
    working_days_per_month = models.PositiveIntegerField(
        default=26,
        verbose_name=_("أيام العمل الشهرية"),
        help_text=_("عدد أيام العمل في الشهر — يُستخدم لحساب أجر اليوم"),
    )

    # --- متطلبات التحقق عند البصمة ---
    require_face_verification = models.BooleanField(
        default=False,
        verbose_name=_("إلزام بصمة الوجه"),
        help_text=_("إذا مُفعّل: لن يتمكن الموظف من تسجيل الحضور بدون التحقق من وجهه بالكاميرا"),
    )
    require_location = models.BooleanField(
        default=False,
        verbose_name=_("إلزام تحديد الموقع (GPS)"),
        help_text=_("إذا مُفعّل: لن يتمكن الموظف من تسجيل الحضور بدون تفعيل الموقع الجغرافي"),
    )
    face_match_threshold = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('0.45'),
        verbose_name=_("حد مطابقة الوجه"),
        help_text=_("المسافة الأقصى للمطابقة (أقل = أدق). الافتراضي 0.45 — قيم بين 0.3 و 0.6"),
    )

    # --- سياسات السلف ---
    max_advance_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('50.00'),
        verbose_name=_("الحد الأقصى للسلفة (% من الراتب)"),
        help_text=_("أقصى نسبة مسموحة من الراتب الأساسي كسلفة"),
    )
    max_installments = models.PositiveIntegerField(
        default=6,
        verbose_name=_("أقصى عدد أقساط"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("إعدادات الموارد البشرية")
        verbose_name_plural = _("إعدادات الموارد البشرية")

    def __str__(self):
        return "إعدادات HR"

    @staticmethod
    def get_settings():
        """جلب أو إنشاء إعدادات HR — Singleton per tenant schema."""
        obj, _ = HRSettings.objects.get_or_create(pk=1)
        return obj


# =====================================================================
# 2. ملف الموظف الموحد (Unified Employee Profile)
# =====================================================================

class Employee(models.Model):
    """
    ملف الموظف — يخدم القطاعين (سيارات وطباعة).
    مربوط بـ User (1:1) ويحتوي على:
    - البيانات الشخصية والوظيفية
    - بصمة الوجه (face_encoding) للتحقق من الهوية
    - المدير المباشر (supervisor) لنظام الموافقات
    - صلاحية التفويض التلقائي للمصممين
    """
    DEPARTMENT_CHOICES = (
        # عام
        ('management', _('الإدارة العامة')),
        ('hr', _('الموارد البشرية')),
        ('accounting', _('المحاسبة')),
        # قطاع السيارات
        ('workshop', _('الورشة / الصيانة')),
        ('spare_parts', _('قطع الغيار')),
        ('warehouse', _('المخزن')),
        ('reception', _('الاستقبال')),
        # قطاع الطباعة
        ('design', _('التصميم')),
        ('printing', _('الطباعة والتشطيب')),
        ('sales', _('المبيعات')),
    )

    CONTRACT_CHOICES = (
        ('full_time', _('دوام كامل')),
        ('part_time', _('دوام جزئي')),
        ('contract', _('عقد مؤقت')),
        ('freelance', _('مستقل (Freelance)')),
    )

    # --- الربط بـ User ---
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='hr_employee', verbose_name=_("حساب المستخدم"),
    )

    # --- البيانات الوظيفية ---
    employee_id = models.CharField(
        max_length=20, unique=True, blank=True,
        verbose_name=_("الرقم الوظيفي"),
        help_text=_("يُولّد تلقائياً إذا تُرك فارغاً"),
    )
    department = models.CharField(
        max_length=20, choices=DEPARTMENT_CHOICES, default='workshop',
        verbose_name=_("القسم"),
    )
    job_title = models.CharField(
        max_length=100, blank=True, verbose_name=_("المسمى الوظيفي"),
        help_text=_("مثال: فني ميكانيكا، مصمم جرافيك، مدير مبيعات"),
    )
    contract_type = models.CharField(
        max_length=15, choices=CONTRACT_CHOICES, default='full_time',
        verbose_name=_("نوع التعاقد"),
    )
    hire_date = models.DateField(
        default=timezone.now, verbose_name=_("تاريخ التعيين"),
    )

    # --- الهيكل الإداري ---
    supervisor = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='subordinates', verbose_name=_("المدير المباشر"),
        help_text=_("المسؤول عن الموافقات والمراجعات لهذا الموظف"),
    )
    is_hr_manager = models.BooleanField(
        default=False, verbose_name=_("مدير موارد بشرية"),
        help_text=_("صلاحية الموافقة على السلف وتعديل إعدادات HR"),
    )

    # --- المالي ---
    base_salary = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("الراتب الأساسي (شهري)"),
    )
    daily_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("أجر اليوم"),
        help_text=_("يُحسب تلقائياً = الراتب / أيام العمل الشهرية. يمكن تعديله يدوياً."),
    )

    # --- بصمة الوجه (Face Recognition) ---
    face_encoding = models.JSONField(
        null=True, blank=True,
        verbose_name=_("بصمة الوجه (Face Encoding)"),
        help_text=_("بيانات الـ Face Embedding المُشفرة — تُنشأ من صورة الموظف عبر واجهة التسجيل"),
    )
    face_photo = models.ImageField(
        upload_to='hr/faces/%Y/%m/', blank=True,
        verbose_name=_("صورة بصمة الوجه"),
        help_text=_("الصورة المرجعية المستخدمة لتوليد الـ Face Encoding"),
    )

    # --- تفويض المصممين ---
    auto_approve_designs = models.BooleanField(
        default=False,
        verbose_name=_("تفويض تلقائي (تخطي مراجعة المدير)"),
        help_text=_("إذا مُفعّل: تصميمات هذا الموظف تُعتمد فوراً بدون مراجعة المدير المباشر"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("موظف")
        verbose_name_plural = _("الموظفون")
        ordering = ['department', 'user__first_name']

    def __str__(self):
        name = self.user.get_full_name() or self.user.username
        return f"{self.employee_id} — {name} ({self.get_department_display()})"

    def save(self, *args, **kwargs):
        # Auto-generate employee_id
        if not self.employee_id:
            prefix = self.department[:3].upper() if self.department else 'EMP'
            last = Employee.objects.filter(
                employee_id__startswith=prefix
            ).order_by('-employee_id').first()
            if last and last.employee_id[len(prefix):].isdigit():
                num = int(last.employee_id[len(prefix):]) + 1
            else:
                num = 1
            self.employee_id = f"{prefix}{num:04d}"

        # Auto-calculate daily_rate if not manually set
        if self.base_salary > 0 and self.daily_rate == 0:
            hr_settings = HRSettings.get_settings()
            if hr_settings.working_days_per_month > 0:
                self.daily_rate = (
                    self.base_salary / Decimal(str(hr_settings.working_days_per_month))
                ).quantize(Decimal('0.01'))

        super().save(*args, **kwargs)

    def get_daily_rate(self):
        """حساب أجر اليوم الفعلي."""
        if self.daily_rate > 0:
            return self.daily_rate
        hr_settings = HRSettings.get_settings()
        if self.base_salary > 0 and hr_settings.working_days_per_month > 0:
            return (
                self.base_salary / Decimal(str(hr_settings.working_days_per_month))
            ).quantize(Decimal('0.01'))
        return Decimal('0.00')


# =====================================================================
# 3. ورديات العمل (Work Shifts)
# =====================================================================

class WorkShift(models.Model):
    """
    وردية عمل — يمكن ربط أكثر من موظف بنفس الوردية.
    أيام العمل تُحفظ كـ JSON list مثل: ["sat","sun","mon","tue","wed","thu"]
    """
    name = models.CharField(
        max_length=100, verbose_name=_("اسم الوردية"),
        help_text=_("مثال: وردية صباحية، وردية مسائية"),
    )
    start_time = models.TimeField(verbose_name=_("وقت البداية"))
    end_time = models.TimeField(verbose_name=_("وقت النهاية"))
    days_of_week = models.JSONField(
        default=list, verbose_name=_("أيام العمل"),
        help_text=_('قائمة أيام الأسبوع: ["sat","sun","mon","tue","wed","thu"]'),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("نشطة"))

    class Meta:
        verbose_name = _("وردية عمل")
        verbose_name_plural = _("ورديات العمل")

    def __str__(self):
        return f"{self.name} ({self.start_time.strftime('%H:%M')} — {self.end_time.strftime('%H:%M')})"


class EmployeeShiftAssignment(models.Model):
    """ربط الموظف بالوردية — مع فترة صلاحية."""
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='shift_assignments',
        verbose_name=_("الموظف"),
    )
    shift = models.ForeignKey(
        WorkShift, on_delete=models.CASCADE, related_name='assignments',
        verbose_name=_("الوردية"),
    )
    effective_from = models.DateField(default=timezone.now, verbose_name=_("سارية من"))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_("سارية حتى"))

    class Meta:
        verbose_name = _("تعيين وردية")
        verbose_name_plural = _("تعيينات الورديات")
        ordering = ['-effective_from']

    def __str__(self):
        return f"{self.employee} -> {self.shift}"


# =====================================================================
# 4. سجل الحضور والانصراف الذكي (Smart Attendance)
# =====================================================================

class AttendanceRecord(models.Model):
    """
    سجل حضور/انصراف يومي لكل موظف.
    يتضمن:
    - التحقق من بصمة الوجه (face_verified)
    - التحقق من الموقع الجغرافي (location_verified)
    - حساب التأخير وساعات العمل تلقائياً
    """
    STATUS_CHOICES = (
        ('present', _('حاضر')),
        ('late', _('متأخر')),
        ('absent', _('غائب')),
        ('excused', _('إجازة / إذن')),
        ('holiday', _('عطلة رسمية')),
    )

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='attendance_records',
        verbose_name=_("الموظف"),
    )
    date = models.DateField(default=timezone.now, verbose_name=_("التاريخ"))
    shift = models.ForeignKey(
        WorkShift, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name=_("الوردية"),
    )

    # --- أوقات الحضور والانصراف ---
    clock_in = models.DateTimeField(null=True, blank=True, verbose_name=_("وقت الحضور"))
    clock_out = models.DateTimeField(null=True, blank=True, verbose_name=_("وقت الانصراف"))

    # --- التحقق من الهوية والموقع ---
    face_verified = models.BooleanField(
        default=False, verbose_name=_("بصمة الوجه مُوثقة"),
    )
    check_in_latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True,
        verbose_name=_("خط العرض عند الحضور"),
    )
    check_in_longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True,
        verbose_name=_("خط الطول عند الحضور"),
    )
    location_verified = models.BooleanField(
        default=False, verbose_name=_("الموقع الجغرافي مُوثق"),
    )
    check_out_latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True,
        verbose_name=_("خط العرض عند الانصراف"),
    )
    check_out_longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True,
        verbose_name=_("خط الطول عند الانصراف"),
    )

    # --- الحسابات التلقائية ---
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='absent',
        verbose_name=_("الحالة"),
    )
    late_minutes = models.PositiveIntegerField(
        default=0, verbose_name=_("دقائق التأخير"),
    )
    worked_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("ساعات العمل الفعلية"),
    )
    overtime_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("ساعات العمل الإضافية"),
    )

    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("سجل حضور")
        verbose_name_plural = _("سجلات الحضور والانصراف")
        unique_together = ('employee', 'date')
        ordering = ['-date', 'employee']

    def __str__(self):
        return f"{self.employee} — {self.date} ({self.get_status_display()})"


# =====================================================================
# 5. طلبات الإجازات (Leave Requests)
# =====================================================================

class LeaveRequest(models.Model):
    """طلب إجازة — يتطلب موافقة HR Manager أو المدير المباشر."""
    TYPE_CHOICES = (
        ('annual', _('سنوية')),
        ('sick', _('مرضية')),
        ('personal', _('شخصية')),
        ('unpaid', _('بدون راتب')),
        ('emergency', _('طارئة')),
    )
    STATUS_CHOICES = (
        ('pending', _('قيد المراجعة')),
        ('approved', _('موافق عليه')),
        ('rejected', _('مرفوض')),
        ('cancelled', _('ملغي')),
    )

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='leave_requests',
        verbose_name=_("الموظف"),
    )
    leave_type = models.CharField(
        max_length=15, choices=TYPE_CHOICES, verbose_name=_("نوع الإجازة"),
    )
    from_date = models.DateField(verbose_name=_("من تاريخ"))
    to_date = models.DateField(verbose_name=_("إلى تاريخ"))
    reason = models.TextField(blank=True, verbose_name=_("السبب"))

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='pending',
        verbose_name=_("الحالة"),
    )
    reviewed_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_leaves', verbose_name=_("تمت المراجعة بواسطة"),
    )
    review_notes = models.TextField(blank=True, verbose_name=_("ملاحظات المراجعة"))
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ المراجعة"))

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("طلب إجازة")
        verbose_name_plural = _("طلبات الإجازات")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.employee} — {self.get_leave_type_display()} ({self.from_date} -> {self.to_date})"

    @property
    def total_days(self):
        """عدد أيام الإجازة."""
        if self.from_date and self.to_date:
            return (self.to_date - self.from_date).days + 1
        return 0

    def clean(self):
        if self.from_date and self.to_date and self.to_date < self.from_date:
            raise ValidationError(_("تاريخ النهاية يجب أن يكون بعد تاريخ البداية."))


# =====================================================================
# 6. السلف والعهد (Advances & Loans)
# =====================================================================

class Advance(models.Model):
    """
    سلفة موظف — تُقسّم على أقساط تُخصم شهرياً من الراتب.
    تتطلب موافقة HR Manager أو Company Admin.
    """
    STATUS_CHOICES = (
        ('pending', _('قيد الموافقة')),
        ('approved', _('موافق عليه')),
        ('rejected', _('مرفوض')),
        ('active', _('جاري السداد')),
        ('completed', _('تم السداد بالكامل')),
        ('cancelled', _('ملغي')),
    )

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='advances',
        verbose_name=_("الموظف"),
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal('1.00'))],
        verbose_name=_("مبلغ السلفة"),
    )
    reason = models.TextField(blank=True, verbose_name=_("سبب السلفة"))
    installments_count = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(24)],
        verbose_name=_("عدد الأقساط"),
        help_text=_("عدد الأشهر لتقسيط السلفة"),
    )

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='pending',
        verbose_name=_("الحالة"),
    )
    approved_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_advances', verbose_name=_("الموافقة بواسطة"),
    )
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الموافقة"))
    rejection_reason = models.TextField(blank=True, verbose_name=_("سبب الرفض"))

    remaining_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("المبلغ المتبقي"),
    )

    requested_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الطلب"))

    class Meta:
        verbose_name = _("سلفة")
        verbose_name_plural = _("السلف والعهد")
        ordering = ['-requested_at']

    def __str__(self):
        return f"{self.employee} — {self.amount} ({self.get_status_display()})"

    def clean(self):
        """التحقق من الحد الأقصى للسلفة وعدد الأقساط."""
        if self.amount and self.employee_id:
            hr_settings = HRSettings.get_settings()
            max_allowed = (
                self.employee.base_salary * hr_settings.max_advance_percentage / Decimal('100')
            )
            if self.amount > max_allowed:
                raise ValidationError(
                    _(
                        "مبلغ السلفة (%(amount)s) يتجاوز الحد الأقصى المسموح "
                        "(%(max)s ج.م — %(pct)s%% من الراتب)."
                    ),
                    params={
                        'amount': self.amount,
                        'max': max_allowed.quantize(Decimal('0.01')),
                        'pct': hr_settings.max_advance_percentage,
                    },
                )

            if self.installments_count > hr_settings.max_installments:
                raise ValidationError(
                    _("عدد الأقساط (%(count)s) يتجاوز الحد الأقصى (%(max)s)."),
                    params={
                        'count': self.installments_count,
                        'max': hr_settings.max_installments,
                    },
                )

    @property
    def installment_amount(self):
        """قيمة القسط الشهري."""
        if self.installments_count > 0:
            return (self.amount / Decimal(str(self.installments_count))).quantize(Decimal('0.01'))
        return self.amount


class AdvanceInstallment(models.Model):
    """قسط سلفة مجدول — يُخصم تلقائياً عند تشغيل كشف الرواتب."""
    STATUS_CHOICES = (
        ('scheduled', _('مجدول')),
        ('deducted', _('تم الخصم')),
        ('skipped', _('مؤجل')),
    )

    advance = models.ForeignKey(
        Advance, on_delete=models.CASCADE, related_name='installments',
        verbose_name=_("السلفة"),
    )
    installment_number = models.PositiveIntegerField(verbose_name=_("رقم القسط"))
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("مبلغ القسط"),
    )
    due_month = models.DateField(
        verbose_name=_("شهر الاستحقاق"),
        help_text=_("أول يوم من الشهر المستحق فيه القسط"),
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='scheduled',
        verbose_name=_("الحالة"),
    )
    deducted_in_payroll = models.ForeignKey(
        'PayrollEntry', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='advance_installments_deducted',
        verbose_name=_("خُصم في كشف راتب"),
    )

    class Meta:
        verbose_name = _("قسط سلفة")
        verbose_name_plural = _("أقساط السلف")
        unique_together = ('advance', 'installment_number')
        ordering = ['due_month', 'installment_number']

    def __str__(self):
        return f"قسط #{self.installment_number} — {self.amount} ({self.get_status_display()})"


# =====================================================================
# 7. الدورة الشهرية للمرتبات (Payroll Engine)
# =====================================================================

class PayrollRun(models.Model):
    """
    دورة رواتب شهرية — يُنشأ سجل واحد لكل شهر.
    يحتوي على كشوف الرواتب الفردية (PayrollEntry) لكل موظف.
    """
    STATUS_CHOICES = (
        ('draft', _('مسودة')),
        ('calculated', _('محسوب')),
        ('approved', _('معتمد')),
        ('paid', _('مصروف')),
    )

    period_month = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        verbose_name=_("الشهر"),
    )
    period_year = models.PositiveIntegerField(verbose_name=_("السنة"))
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default='draft',
        verbose_name=_("الحالة"),
    )

    total_gross = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("إجمالي المرتبات قبل الخصم"),
    )
    total_deductions = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("إجمالي الخصومات"),
    )
    total_net = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("صافي المرتبات"),
    )
    total_employees = models.PositiveIntegerField(
        default=0, verbose_name=_("عدد الموظفين"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        verbose_name=_("أنشأها"),
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_payrolls', verbose_name=_("اعتمدها"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الصرف"))

    class Meta:
        verbose_name = _("دورة رواتب")
        verbose_name_plural = _("دورات الرواتب")
        unique_together = ('period_month', 'period_year')
        ordering = ['-period_year', '-period_month']

    def __str__(self):
        return f"رواتب {self.period_month}/{self.period_year} ({self.get_status_display()})"


class PayrollEntry(models.Model):
    """
    كشف راتب فردي لموظف واحد ضمن دورة رواتب شهرية.
    يحسب تلقائياً: خصومات التأخير + الغياب + أقساط السلف.
    """
    payroll_run = models.ForeignKey(
        PayrollRun, on_delete=models.CASCADE, related_name='entries',
        verbose_name=_("دورة الرواتب"),
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='payroll_entries',
        verbose_name=_("الموظف"),
    )

    # --- الراتب الأساسي ---
    base_salary = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("الراتب الأساسي"),
    )

    # --- الخصومات التفصيلية ---
    days_present = models.PositiveIntegerField(default=0, verbose_name=_("أيام الحضور"))
    days_absent = models.PositiveIntegerField(default=0, verbose_name=_("أيام الغياب بدون إذن"))
    days_late = models.PositiveIntegerField(default=0, verbose_name=_("أيام التأخير"))
    days_excused = models.PositiveIntegerField(default=0, verbose_name=_("أيام الإجازة / الإذن"))
    total_late_minutes = models.PositiveIntegerField(default=0, verbose_name=_("إجمالي دقائق التأخير"))
    total_worked_hours = models.DecimalField(
        max_digits=7, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("إجمالي ساعات العمل"),
    )

    late_deduction = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("خصم التأخير"),
    )
    absence_deduction = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("خصم الغياب"),
    )
    advance_deduction = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("خصم أقساط السلف"),
    )
    other_deductions = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("خصومات أخرى"),
    )
    other_deductions_note = models.CharField(
        max_length=255, blank=True, verbose_name=_("ملاحظة الخصومات الأخرى"),
    )

    # --- الإضافات ---
    bonuses = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("مكافآت / حوافز"),
    )
    overtime_pay = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("بدل إضافي"),
    )
    bonuses_note = models.CharField(
        max_length=255, blank=True, verbose_name=_("ملاحظة المكافآت"),
    )

    # --- الصافي ---
    total_deductions = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("إجمالي الخصومات"),
    )
    total_additions = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("إجمالي الإضافات"),
    )
    net_salary = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("صافي الراتب"),
    )

    # --- الربط المالي (Generic — يعمل مع inventory.FinancialTransaction أو printing.PrintTransaction) ---
    treasury_transaction_id = models.PositiveIntegerField(
        null=True, blank=True,
        verbose_name=_("معرّف الحركة المالية"),
        help_text=_("معرّف FinancialTransaction أو PrintTransaction المرتبطة"),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("كشف راتب")
        verbose_name_plural = _("كشوف الرواتب")
        unique_together = ('payroll_run', 'employee')

    def __str__(self):
        return f"{self.employee} — صافي: {self.net_salary}"

    def calculate_totals(self):
        """إعادة حساب الإجماليات."""
        self.total_deductions = (
            self.late_deduction + self.absence_deduction +
            self.advance_deduction + self.other_deductions
        )
        self.total_additions = self.bonuses + self.overtime_pay
        self.net_salary = (
            self.base_salary - self.total_deductions + self.total_additions
        )
        # ضمان عدم سالبية الراتب
        if self.net_salary < 0:
            self.net_salary = Decimal('0.00')


# =====================================================================
# 8. حلقة عمل التصميم (Design Approval Workflow)
# =====================================================================

class DesignSubmission(models.Model):
    """
    تصميم مرفوع من المصمم — يمر بمسار موافقة ذكي:
    1. المصمم يرفع التصميم
    2. إذا auto_approve_designs مُفعّل --> يُعتمد فوراً
    3. وإلا --> يذهب للمدير المباشر (supervisor) للمراجعة
    """
    EXECUTION_TYPE_CHOICES = (
        ('manual', _('يدوي بالكامل')),
        ('ai_generated', _('مُنشأ بالذكاء الاصطناعي')),
        ('ai_assisted', _('مساعد بالذكاء الاصطناعي (AI + تعديل يدوي)')),
    )
    STATUS_CHOICES = (
        ('pending', _('قيد المراجعة')),
        ('approved', _('معتمد')),
        ('rejected', _('مرفوض')),
        ('revision_requested', _('مطلوب تعديل')),
    )

    # --- المصمم والملف ---
    designer = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='design_submissions',
        verbose_name=_("المصمم"),
    )
    title = models.CharField(max_length=200, verbose_name=_("عنوان التصميم"))
    description = models.TextField(blank=True, verbose_name=_("الوصف / التفاصيل"))
    design_file = models.FileField(
        upload_to='hr/designs/%Y/%m/', verbose_name=_("ملف التصميم"),
    )
    preview_image = models.ImageField(
        upload_to='hr/design_previews/%Y/%m/', blank=True,
        verbose_name=_("صورة معاينة"),
    )
    execution_type = models.CharField(
        max_length=15, choices=EXECUTION_TYPE_CHOICES, default='manual',
        verbose_name=_("نوع التنفيذ"),
    )

    # --- مسار الموافقة ---
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending',
        verbose_name=_("الحالة"),
    )
    auto_approved = models.BooleanField(
        default=False, verbose_name=_("اعتماد تلقائي"),
        help_text=_("يُفعّل عند اعتماد التصميم تلقائياً بسبب تفويض المصمم"),
    )
    reviewer = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_designs', verbose_name=_("المراجع"),
    )
    review_notes = models.TextField(blank=True, verbose_name=_("ملاحظات المراجعة"))
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ المراجعة"))

    # --- الربط بالطلب (اختياري — PrintOrder أو SaleInvoice) ---
    related_order_id = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("رقم الطلب المرتبط"),
        help_text=_("رقم PrintOrder أو SaleInvoice المرتبط بالتصميم"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("تصميم مرفوع")
        verbose_name_plural = _("التصاميم المرفوعة")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} — {self.designer} ({self.get_status_display()})"


# =====================================================================
# 9. اشتراكات الذكاء الاصطناعي للمصممين (AI Design Subscriptions)
# =====================================================================

class AIDesignSubscription(models.Model):
    """
    اشتراك مصمم في خدمة الذكاء الاصطناعي للتصميم.
    - يتحكم في إمكانية استخدام AI في إنشاء/تعديل التصاميم
    - يمكن تفعيله بالدفع الإلكتروني (فيزا) أو يدوياً من الأدمن
    - عند انتهاء الاشتراك يتوقف AI تلقائياً
    """
    PLAN_CHOICES = (
        ('basic', _('أساسي — 50 تصميم AI شهرياً')),
        ('pro', _('احترافي — 200 تصميم AI شهرياً')),
        ('unlimited', _('غير محدود — تصاميم لا نهائية')),
    )
    STATUS_CHOICES = (
        ('active', _('نشط')),
        ('expired', _('منتهي')),
        ('cancelled', _('ملغي')),
        ('pending_payment', _('في انتظار الدفع')),
    )
    PAYMENT_METHOD_CHOICES = (
        ('visa', _('فيزا / بطاقة ائتمان')),
        ('admin_manual', _('تفعيل يدوي من الأدمن')),
        ('wallet', _('خصم من رصيد الشركة')),
    )

    # --- المصمم ---
    designer = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='ai_subscriptions',
        verbose_name=_("المصمم"),
    )

    # --- تفاصيل الاشتراك ---
    plan = models.CharField(
        max_length=15, choices=PLAN_CHOICES, default='basic',
        verbose_name=_("الباقة"),
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending_payment',
        verbose_name=_("الحالة"),
    )
    start_date = models.DateField(
        null=True, blank=True, verbose_name=_("تاريخ البدء"),
    )
    end_date = models.DateField(
        null=True, blank=True, verbose_name=_("تاريخ الانتهاء"),
    )
    auto_renew = models.BooleanField(
        default=False, verbose_name=_("تجديد تلقائي"),
        help_text=_("عند التفعيل: يتجدد الاشتراك تلقائياً عند انتهائه (يتطلب بطاقة محفوظة)"),
    )

    # --- الدفع ---
    payment_method = models.CharField(
        max_length=15, choices=PAYMENT_METHOD_CHOICES, default='visa',
        verbose_name=_("طريقة الدفع"),
    )
    price_paid = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name=_("المبلغ المدفوع"),
    )
    payment_reference = models.CharField(
        max_length=200, blank=True,
        verbose_name=_("مرجع الدفع"),
        help_text=_("رقم العملية من بوابة الدفع أو ملاحظة التفعيل اليدوي"),
    )
    card_last_four = models.CharField(
        max_length=4, blank=True,
        verbose_name=_("آخر 4 أرقام من البطاقة"),
    )

    # --- الاستخدام ---
    ai_generations_used = models.PositiveIntegerField(
        default=0, verbose_name=_("عدد التصاميم المُنشأة بالـ AI"),
    )
    ai_generations_limit = models.PositiveIntegerField(
        default=50, verbose_name=_("الحد الأقصى للتصاميم"),
        help_text=_("0 يعني غير محدود"),
    )

    # --- الأدمن ---
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_subs_activated', verbose_name=_("فعّله"),
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_subs_cancelled', verbose_name=_("ألغاه"),
    )
    admin_notes = models.TextField(blank=True, verbose_name=_("ملاحظات الأدمن"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("اشتراك AI تصميم")
        verbose_name_plural = _("اشتراكات AI التصميم")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.designer} — {self.get_plan_display()} ({self.get_status_display()})"

    @property
    def is_active_and_valid(self):
        """هل الاشتراك نشط ولم ينتهِ؟"""
        if self.status != 'active':
            return False
        today = timezone.now().date()
        if self.end_date and today > self.end_date:
            return False
        # فحص حد الاستخدام
        if self.ai_generations_limit > 0 and self.ai_generations_used >= self.ai_generations_limit:
            return False
        return True

    @property
    def days_remaining(self):
        """الأيام المتبقية."""
        if not self.end_date:
            return 0
        remaining = (self.end_date - timezone.now().date()).days
        return max(0, remaining)

    @property
    def usage_percentage(self):
        """نسبة الاستخدام."""
        if self.ai_generations_limit == 0:
            return 0  # unlimited
        if self.ai_generations_limit > 0:
            return min(100, int(self.ai_generations_used / self.ai_generations_limit * 100))
        return 0

    PLAN_PRICES = {
        'basic': Decimal('99.00'),
        'pro': Decimal('249.00'),
        'unlimited': Decimal('499.00'),
    }
    PLAN_LIMITS = {
        'basic': 50,
        'pro': 200,
        'unlimited': 0,  # 0 = unlimited
    }
