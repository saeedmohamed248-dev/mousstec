"""
🛡️ Soft Delete infrastructure for Mousstec SaaS.

لإن `Client` بيرث `TenantMixin` من django-tenants، الحذف الفعلي بيكسر:
- فواتير تاريخية (PlatformInvoice) مرتبطة بالـ tenant.
- مدفوعات Escrow ومحافظ B2B.
- سجلات الـ marketplace والمزادات.

الحل: علم `is_deleted=True` على الـ tenant فيختفي من كل القوائم،
بس الـ schema والـ FKs تفضل سليمة. الـ "Force Delete" متاح فقط للـ
ultimate superuser (is_superuser=True) كملاذ أخير.
"""

from django.db import models
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.conf import settings


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def dead(self):
        return self.filter(is_deleted=True)

    def soft_delete(self, user=None, reason=''):
        return self.update(
            is_deleted=True,
            deleted_at=timezone.now(),
            deleted_by=user,
            deletion_reason=reason or '',
        )


class SoftDeleteManager(models.Manager):
    """Manager افتراضي — يخفي المحذوفين تلقائياً."""
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=False)


class AllObjectsManager(models.Manager):
    """Manager يعرض كل الصفوف (للسوبر أدمن: استعادة / Force Delete)."""
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db)


class SoftDeleteMixin(models.Model):
    """
    ملاحظة معمارية: المzixين بيضيف الحقول والـ methods فقط.
    مش بيـ override الـ default `objects` manager — عشان django-tenants
    بيستخدم Client.objects.get(schema_name=...) في الـ middleware،
    ولو فلترنا المحذوفين هيكسر التحميل.

    الحماية الفعلية بتم في:
    1. منع الـ login: soft_delete() بيـ set is_active=False (لو متاح).
    2. عرض القوائم: الـ views تستخدم alive_objects / تفلتر is_deleted=False يدوياً.
    """
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    deletion_reason = models.CharField(max_length=255, blank=True, default='')

    # الـ managers اختياريين — كل model يقرر يضيفهم أو لا.
    # في Client، بنحتفظ بـ objects الافتراضي (لـ django-tenants) ونضيف alive_objects.
    alive_objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def soft_delete(self, user=None, reason=''):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = user if (user and user.is_authenticated) else None
        self.deletion_reason = (reason or '')[:255]
        self.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by', 'deletion_reason'])

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.deletion_reason = ''
        self.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by', 'deletion_reason'])

    def force_delete(self, user):
        """حذف فعلي من الـ DB — متاح للـ ultimate superuser فقط."""
        if not (user and user.is_authenticated and user.is_superuser):
            raise PermissionDenied("Force delete restricted to ultimate superuser.")
        return super().delete()
