"""🛍️ كتالوج السوق — متاجر ومنتجات بتنفّذها Mouss Tec نفسها (مطابع/تصميم + قطع غيار).

* كل متجر ليه اسم وتخصص، والكارت بيقول «بيتنفّذ عن طريق Mouss Tec» — شفافية للعميل
  (مفيش تقييمات ولا مبيعات مزيّفة).
* المنتجات من غير أسعار: العميل بيضغط «اطلب سعر» ⇒ CatalogQuoteRequest بيوصل لصاحب المنصة
  في السوبر أدمن.
"""
import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

__all__ = ['CatalogStore', 'CatalogItem', 'CatalogQuoteRequest']

SECTOR_CHOICES = (
    ('printing', _('طباعة وتصميم')),
    ('automotive', _('قطع غيار سيارات')),
)


class CatalogStore(models.Model):
    sector = models.CharField(max_length=20, choices=SECTOR_CHOICES, db_index=True)
    name = models.CharField(max_length=120, verbose_name=_("اسم المتجر"))
    slug = models.SlugField(max_length=80, unique=True, allow_unicode=True)
    tagline = models.CharField(max_length=200, blank=True, verbose_name=_("جملة تعريفية"))
    description = models.TextField(blank=True, verbose_name=_("عن المتجر"))
    icon = models.CharField(max_length=50, default='fa-store', help_text=_("Font Awesome، مثلاً fa-mug-hot"))
    color = models.CharField(max_length=20, default='#1E2A57')
    city = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    sort = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("متجر كتالوج")
        verbose_name_plural = _("🛍️ متاجر الكتالوج (Mouss Tec)")
        ordering = ['sector', 'sort', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_sector_display()})"


class CatalogItem(models.Model):
    store = models.ForeignKey(CatalogStore, on_delete=models.CASCADE, related_name='items')
    item_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    title = models.CharField(max_length=200, verbose_name=_("اسم المنتج"))
    description = models.TextField(blank=True, verbose_name=_("الوصف"))
    category = models.CharField(max_length=40, db_index=True, verbose_name=_("القسم"))
    icon = models.CharField(max_length=50, default='fa-box')
    image = models.ImageField(upload_to='catalog/', blank=True, null=True, verbose_name=_("صورة (اختياري)"))
    # قطع الغيار
    car_make = models.CharField(max_length=60, blank=True, db_index=True, verbose_name=_("الماركة"))
    car_models = models.CharField(max_length=200, blank=True, verbose_name=_("الموديلات"))
    part_number = models.CharField(max_length=80, blank=True)
    condition = models.CharField(max_length=40, blank=True, verbose_name=_("الحالة/النوع"))
    min_qty = models.CharField(max_length=60, blank=True, verbose_name=_("أقل كمية"))
    is_active = models.BooleanField(default=True, db_index=True)
    sort = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("منتج كتالوج")
        verbose_name_plural = _("منتجات الكتالوج")
        ordering = ['store', 'sort', 'id']
        indexes = [models.Index(fields=['category', 'is_active'])]

    def __str__(self):
        return self.title


class CatalogQuoteRequest(models.Model):
    STATUS_CHOICES = (
        ('new', _('جديد')),
        ('contacted', _('اتواصلت معاه')),
        ('quoted', _('اتبعتله سعر')),
        ('done', _('اتنفّذ')),
        ('cancelled', _('ملغي')),
    )
    request_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    item = models.ForeignKey(CatalogItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='quote_requests')
    item_title = models.CharField(max_length=200)      # نسخة ثابتة لو المنتج اتمسح
    store_name = models.CharField(max_length=120, blank=True)
    sector = models.CharField(max_length=20, choices=SECTOR_CHOICES, db_index=True)
    customer = models.ForeignKey('MarketplaceCustomer', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='catalog_quote_requests')
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30)
    quantity = models.CharField(max_length=60, blank=True)
    details = models.TextField(blank=True)
    car_info = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new', db_index=True)
    admin_notes = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("طلب سعر من الكتالوج")
        verbose_name_plural = _("طلبات أسعار الكتالوج")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.item_title} — {self.name}"
