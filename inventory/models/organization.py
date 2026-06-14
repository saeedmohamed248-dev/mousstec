from django.db import models, transaction, connection
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from simple_history.models import HistoricalRecords
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from datetime import timedelta
from django.contrib.auth.models import User
from django.db.models import F, Sum, Q, ExpressionWrapper, DecimalField

import uuid
import logging

logger = logging.getLogger('mouss_tec_core')

# Branches, users, employees, shifts, attendance.

class Branch(models.Model):
    name = models.CharField(max_length=100, verbose_name=_("اسم الفرع"))
    location = models.CharField(max_length=255, blank=True, verbose_name=_("الموقع"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("رقم تليفون الفرع"))
    def __str__(self): return self.name


# =====================================================================
# 🔐 Two-Factor Authentication (TOTP — Google Authenticator/Authy)
# =====================================================================
class UserMFA(models.Model):
    """
    سجل MFA لكل مستخدم — TOTP secret + backup codes.

    لما المستخدم يفعّل 2FA من صفحة Security Settings، بنخزّن الـ secret
    (base32) و 10 backup codes (hashed). أثناء الـ login، بعد ما الـ password
    يـ pass، النظام بيـ challenge بكود من الـ authenticator app.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='mfa', verbose_name=_("المستخدم"))
    secret = models.CharField(max_length=64, verbose_name=_("TOTP Secret (base32)"))
    is_enabled = models.BooleanField(default=False, verbose_name=_("مفعّل؟"))
    # JSON list of hashed backup codes — recovery codes for lost devices
    backup_codes = models.JSONField(default=list, blank=True, verbose_name=_("أكواد الاسترجاع"))
    created_at = models.DateTimeField(auto_now_add=True)
    enabled_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("مصادقة ثنائية")
        verbose_name_plural = _("المصادقة الثنائية")

    def __str__(self):
        status = "✅" if self.is_enabled else "⏸️"
        return f"{status} MFA — {self.user.username}"

class EmployeeProfile(models.Model):
    ROLE_CHOICES = (
        ('admin',    _('مدير عام (أدمن)')),
        ('manager',  _('مدير فرع')),
        ('sales',    _('مبيعات (Sales)')),
        ('engineer', _('مهندس تشخيص (Engineer)')),
        ('tech',     _('فني / ميكانيكي (Technician)')),
        ('cashier',  _('كاشير / استقبال (Cashier)')),
        ('stock',    _('أمين مخزن')),
        ('hr',       _('موارد بشرية (HR)')),
    )

    WORKSPACE_MAP = {
        'admin':    '/system/dashboard/',
        'manager':  '/system/dashboard/',
        'sales':    '/system/dashboard/',
        'cashier':  '/system/dashboard/',
        'stock':    '/system/dashboard/',
        'engineer': '/system/tech-workspace/',
        'tech':     '/system/tech-workspace/',
        'hr':       '/system/hr-workspace/',
    }

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='employee_profile', verbose_name=_("المستخدم"))
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع التابع له"))
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='cashier', verbose_name=_("الدور الوظيفي"))
    can_edit_posted_invoices = models.BooleanField(default=False, verbose_name=_("صلاحية تعديل الفواتير المعتمدة؟"))
    can_see_costs = models.BooleanField(default=False, verbose_name=_("صلاحية رؤية أسعار التكلفة؟"),
        help_text=_("Sales = False بشكل افتراضي — يرى أسعار البيع فقط"))

    commission_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("رصيد العمولات المستحقة"))
    commission_rate_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0.00,
        verbose_name=_("نسبة عمولة المبيعات %"),
        help_text=_("تُطبَّق على هامش الربح للقطع التي يبيعها الموظف"))

    # 📍 GPS — last known check-in (denormalised for fast HR dashboards)
    last_checkin_at = models.DateTimeField(null=True, blank=True, verbose_name=_("آخر تسجيل حضور"))
    last_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_("آخر Lat"))
    last_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_("آخر Lng"))

    class Meta:
        verbose_name = _("ملف الموظف")
        verbose_name_plural = _("ملفات الموظفين والصلاحيات")

    def default_workspace_url(self) -> str:
        if self.user.is_superuser:
            return '/system/dashboard/'
        return self.WORKSPACE_MAP.get(self.role, '/system/dashboard/')

    def __str__(self):
        branch_name = self.branch.name if self.branch else "إدارة عامة"
        return f"{self.user.get_full_name() or self.user.username} - {self.get_role_display()}"

class EmployeeShift(models.Model):
    employee = models.ForeignKey(EmployeeProfile, on_delete=models.CASCADE, limit_choices_to={'role': 'tech'}, verbose_name=_("الفني"))
    clock_in = models.DateTimeField(default=timezone.now, verbose_name=_("وقت تسجيل الدخول"))
    clock_out = models.DateTimeField(blank=True, null=True, verbose_name=_("وقت تسجيل الخروج"))
    total_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_("إجمالي ساعات الوردية"))
    
    class Meta: verbose_name_plural = _("سجل حضور وإنتاجية الفنيين")
    def save(self, *args, **kwargs):
        if self.clock_out and self.clock_in:
            duration = self.clock_out - self.clock_in
            seconds = max(duration.total_seconds(), 0)
            self.total_hours = (Decimal(str(seconds)) / Decimal('3600')).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)

class AttendanceCheckIn(models.Model):
    """GPS-stamped check-in / check-out from the Tech & HR workspaces.
    Browser sends navigator.geolocation.getCurrentPosition() → /api/attendance/checkin/."""
    EVENT_CHOICES = (('in', _('حضور')), ('out', _('انصراف')))

    employee = models.ForeignKey(
        EmployeeProfile, on_delete=models.CASCADE, related_name='checkins',
        verbose_name=_("الموظف"),
    )
    event_type = models.CharField(max_length=5, choices=EVENT_CHOICES, default='in', verbose_name=_("النوع"))
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("اللحظة الزمنية"))

    lat = models.DecimalField(max_digits=9, decimal_places=6, verbose_name=_("Latitude"))
    lng = models.DecimalField(max_digits=9, decimal_places=6, verbose_name=_("Longitude"))
    accuracy_m = models.FloatField(null=True, blank=True, verbose_name=_("دقة GPS (متر)"))

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    is_inside_geofence = models.BooleanField(default=False, verbose_name=_("داخل حدود الفرع؟"))
    flagged_reason = models.CharField(max_length=120, blank=True, verbose_name=_("سبب التنبيه"))

    class Meta:
        verbose_name = _("حركة حضور")
        verbose_name_plural = _("سجل الحضور والانصراف (GPS)")
        indexes = [models.Index(fields=['employee', '-occurred_at'])]

    def __str__(self):
        return f"{self.employee} — {self.get_event_type_display()} @ {self.occurred_at:%Y-%m-%d %H:%M}"


