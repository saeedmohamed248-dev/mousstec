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

# Audit log, import sessions, B2B listing approval queue.

from .organization import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403

class AuditLog(models.Model):
    ACTION_CHOICES = (
        ('create', _('إنشاء')),
        ('update', _('تعديل')),
        ('delete', _('حذف')),
    )
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("المستخدم"))
    action = models.CharField(max_length=10, choices=ACTION_CHOICES, verbose_name=_("نوع العملية"))
    model_name = models.CharField(max_length=100, db_index=True, verbose_name=_("الجدول"))
    object_id = models.CharField(max_length=100, verbose_name=_("معرف السجل"))
    object_repr = models.CharField(max_length=255, blank=True, verbose_name=_("وصف السجل"))
    changes_json = models.JSONField(default=dict, blank=True, verbose_name=_("التغييرات (قبل/بعد)"))
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name=_("عنوان IP"))

    class Meta:
        verbose_name = _("سجل مراجعة")
        verbose_name_plural = _("سجل المراجعة والتدقيق (Audit Trail)")
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['model_name', 'object_id']),
            models.Index(fields=['-timestamp']),
        ]

    def __str__(self):
        return f"[{self.get_action_display()}] {self.model_name} #{self.object_id} — {self.timestamp:%Y-%m-%d %H:%M}"


# =====================================================================
# 📊 دليل الحسابات والقيود المحاسبية (Double-Entry Accounting Ledger)
# =====================================================================
class ImportSession(models.Model):
    STATUS_CHOICES = (
        ('pending', _('في الانتظار')),
        ('validating', _('جاري الفحص')),
        ('preview', _('جاهز للمراجعة')),
        ('importing', _('جاري الاستيراد')),
        ('completed', _('مكتمل')),
        ('failed', _('فشل')),
        ('rolled_back', _('تم التراجع')),
    )
    ENTITY_CHOICES = (
        ('customer', _('عملاء')),
        ('product', _('منتجات')),
        ('invoice', _('فواتير')),
        ('vendor', _('موردين')),
    )
    session_id = models.UUIDField(default=uuid.uuid4, unique=True)
    entity_type = models.CharField(max_length=20, choices=ENTITY_CHOICES, verbose_name=_("نوع البيانات"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name=_("الحالة"))
    uploaded_file = models.FileField(upload_to='imports/', verbose_name=_("الملف"))
    original_filename = models.CharField(max_length=255, verbose_name=_("اسم الملف"))
    total_rows = models.IntegerField(default=0, verbose_name=_("إجمالي الصفوف"))
    valid_rows = models.IntegerField(default=0, verbose_name=_("صفوف صالحة"))
    error_rows = models.IntegerField(default=0, verbose_name=_("صفوف بها أخطاء"))
    conflict_rows = models.IntegerField(default=0, verbose_name=_("صفوف متعارضة"))
    validation_report = models.JSONField(default=dict, blank=True, verbose_name=_("تقرير الفحص"))
    conflict_report = models.JSONField(default=dict, blank=True, verbose_name=_("تقرير التعارضات"))
    imported_ids = models.JSONField(default=list, blank=True, verbose_name=_("السجلات المستوردة"))
    backup_snapshot = models.JSONField(default=dict, blank=True, verbose_name=_("نسخة احتياطية"))
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("جلسة استيراد")
        verbose_name_plural = _("جلسات الاستيراد الآمن")
        ordering = ['-created_at']

    def __str__(self):
        return f"Import #{self.session_id.hex[:8]} — {self.get_entity_type_display()} ({self.get_status_display()})"


# =====================================================================
# 🛒 طلبات النشر في السوق المركزي (B2B Listing Approval)
# =====================================================================
class B2BListingRequest(models.Model):
    STATUS_CHOICES = (
        ('pending', _('قيد المراجعة')),
        ('approved', _('تمت الموافقة')),
        ('rejected', _('مرفوض')),
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE,
        related_name='b2b_listing_requests', verbose_name=_("المنتج"),
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending',
        verbose_name=_("حالة الطلب"), db_index=True,
    )
    requested_price = models.DecimalField(
        max_digits=10, decimal_places=2, verbose_name=_("السعر المطلوب"),
    )
    approved_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name=_("السعر المعتمد"),
    )
    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='b2b_requests_created', verbose_name=_("طالب النشر"),
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='b2b_requests_reviewed', verbose_name=_("المراجع"),
    )
    review_notes = models.TextField(blank=True, verbose_name=_("ملاحظات المراجعة"))
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    is_synced = models.BooleanField(default=False, editable=False)

    class Meta:
        verbose_name = _("طلب نشر في السوق")
        verbose_name_plural = _("طلبات النشر في السوق المركزي (B2B)")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.product.name} — {self.get_status_display()}"


# =====================================================================
# 🚀 DMS Tier-1 — Pillars 1/2/3/4 (Attendance, OBD, Repair, Feedback)
# =====================================================================

