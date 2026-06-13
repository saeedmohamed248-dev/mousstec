from django.conf import settings
from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
from clients.soft_delete import SoftDeleteMixin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db.models import F
from datetime import timedelta
from decimal import Decimal
import uuid
import logging

logger = logging.getLogger('mouss_tec_core')


# Visitor logs, platform events, system error log (super-admin observability).

# =====================================================================
# 📊 Visitor & Activity Tracking (Super Admin Analytics)
# =====================================================================

class VisitorLog(models.Model):
    """
    سجل زوار المنصة — يُستخدم في لوحة السوبر أدمن.
    يُسجل كل طلب HTTP مع البيانات الجغرافية والجهاز.
    Shared app → جدول واحد في الـ public schema.
    """
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    path = models.CharField(max_length=500)
    method = models.CharField(max_length=10, default='GET')
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    tenant_schema = models.CharField(max_length=100, blank=True, db_index=True)
    user_agent = models.TextField(blank=True)
    referer = models.URLField(max_length=1000, blank=True)
    device_type = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = _("سجل زائر")
        verbose_name_plural = _("سجلات الزوار")
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp', 'tenant_schema']),
            models.Index(fields=['ip_address', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.ip_address} → {self.path} ({self.timestamp:%H:%M})"


class PlatformEvent(models.Model):
    """
    أحداث المنصة المهمة — تسجيل دخول، تسجيل شركة، دفع، إلخ.
    يظهر كـ Activity Feed في لوحة السوبر أدمن.
    """
    EVENT_TYPES = (
        ('signup', _('تسجيل شركة جديدة')),
        ('login', _('تسجيل دخول')),
        ('payment', _('عملية دفع')),
        ('subscription', _('تفعيل اشتراك')),
        ('suspension', _('تعليق حساب')),
        ('fraud_flag', _('تعليم احتيال')),
        ('invoice', _('إنشاء فاتورة')),
        ('error', _('خطأ في النظام')),
        ('other', _('أخرى')),
    )

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    tenant_schema = models.CharField(max_length=100, blank=True, db_index=True)
    tenant_name = models.CharField(max_length=150, blank=True)
    user_name = models.CharField(max_length=150, blank=True)
    description = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = _("حدث منصة")
        verbose_name_plural = _("أحداث المنصة")
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.event_type}] {self.description[:80]}"
# =====================================================================
# 🚨 SystemErrorLog — مركز رصد الأخطاء عبر كل المستأجرين (Super Admin)
# =====================================================================
class SystemErrorLog(models.Model):
    LEVEL_CHOICES = (
        ('warning', _('تحذير')),
        ('error', _('خطأ')),
        ('critical', _('حرج')),
    )
    tenant_schema = models.CharField(max_length=63, db_index=True, blank=True, default='')
    tenant_name = models.CharField(max_length=100, blank=True, default='')
    user_id = models.IntegerField(null=True, blank=True)
    username = models.CharField(max_length=150, blank=True, default='')
    path = models.CharField(max_length=500)
    method = models.CharField(max_length=10)
    status_code = models.IntegerField(db_index=True)
    exception_class = models.CharField(max_length=200, blank=True, default='')
    message = models.TextField(blank=True, default='')
    traceback = models.TextField(blank=True, default='')
    request_data = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='error')
    is_resolved = models.BooleanField(default=False, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("سجل خطأ نظام")
        verbose_name_plural = _("سجلات أخطاء النظام")
        indexes = [
            models.Index(fields=['-created_at', 'is_resolved']),
            models.Index(fields=['tenant_schema', '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.status_code}] {self.exception_class or self.path} @ {self.tenant_schema or 'public'}"


# =====================================================================
# 📢 Email Broadcast Campaigns (Super Admin → Tenants)
# =====================================================================

class BroadcastCampaign(models.Model):
    """
    حملة بث جماعية من السوبر أدمن — إعلان، تحديث، maintenance notice…
    تحفظ الـ audience filters المستخدمة + نتائج الإرسال.
    """
    AUDIENCE_CHOICES = (
        ('all',         _('كل الشركات')),
        ('active',      _('الشركات النشطة فقط')),
        ('trial',       _('في الفترة التجريبية')),
        ('expiring',    _('اشتراك ينتهي خلال 14 يوم')),
        ('at_risk',     _('شركات في خطر churn')),
        ('plan',        _('باقة محددة')),
        ('custom',      _('فلتر مخصص')),
    )
    STATUS_CHOICES = (
        ('draft',     _('مسودة')),
        ('sending',   _('جاري الإرسال')),
        ('sent',      _('تم الإرسال')),
        ('failed',    _('فشل')),
    )

    subject = models.CharField(max_length=200, verbose_name=_("الموضوع"))
    body = models.TextField(verbose_name=_("نص الرسالة"))
    audience = models.CharField(max_length=20, choices=AUDIENCE_CHOICES, default='all')
    audience_plan = models.CharField(max_length=20, blank=True, default='',
                                     help_text="لو audience=plan: slug الباقة")
    audience_filter = models.JSONField(default=dict, blank=True,
                                       help_text="فلتر إضافي JSON (e.g. industry=automotive)")

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='draft', db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # نتائج الإرسال
    audience_size = models.IntegerField(default=0, help_text="عدد المستلمين المتوقع")
    sent_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    skipped_count = models.IntegerField(default=0,
                                        help_text="مستلمون بدون بريد")
    error_log = models.TextField(blank=True, default='')

    class Meta:
        verbose_name = _("حملة بث")
        verbose_name_plural = _("حملات البث")
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.get_status_display()}] {self.subject[:50]}"


