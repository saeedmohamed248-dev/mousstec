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

# Products, services, vendors, inventory levels, stock transfers, scrap.

from .organization import *  # noqa: F401, F403

class Product(models.Model):
    CONDITION_CHOICES = (('new', _('جديد')), ('used', _('استيراد/تقطيع')), ('core', _('تالف للتجديد')))
    
    name = models.CharField(max_length=200, verbose_name=_("اسم القطعة")) 
    part_number = models.CharField(max_length=100, unique=True, verbose_name="Part Number") 
    brand = models.CharField(max_length=100, default="BMW", verbose_name=_("الماركة")) 
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='new', verbose_name=_("الحالة"))
    engine_code = models.CharField(max_length=100, blank=True, verbose_name=_("كود المحرك")) 
    car_model = models.CharField(max_length=100, verbose_name=_("الموديلات المتوافقة")) 
    car_year = models.CharField(max_length=100, verbose_name=_("سنة الصنع"))
    barcode = models.CharField(max_length=100, blank=True, null=True, unique=True, verbose_name=_("الباركود"))
    
    chassis_compatibility = models.JSONField(blank=True, null=True, help_text="أكواد الشاسيهات المتوافقة (مثال: ['F30', 'E90', 'G20'])", verbose_name=_("توافقية الشاسيه"))
    oem_cross_reference = models.JSONField(blank=True, null=True, help_text="أرقام الـ OEM البديلة المطابقة", verbose_name=_("أكواد الأجزاء البديلة"))

    min_stock_level = models.IntegerField(default=2, verbose_name=_("حد التنبيه الأساسي"))
    ai_calculated_min_stock = models.IntegerField(default=2, verbose_name=_("حد التنبيه الديناميكي (AI)"))
    
    shopify_product_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="Shopify ID")
    warranty_months = models.IntegerField(default=0, verbose_name=_("فترة الضمان (بالأشهر)"))
    
    purchase_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("آخر سعر شراء"))
    retail_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("سعر البيع (قطاعي)"))
    b2b_wholesale_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("سعر البيع (جملة/B2B)")) 
    average_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("متوسط التكلفة"))
    
    core_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("تأمين التالف/الكور"))
    is_b2b_published = models.BooleanField(default=False, verbose_name=_("طرح في سوق Mouss Tec العام"))
    
    image = models.ImageField(upload_to='products/', blank=True, null=True, verbose_name=_("صورة القطعة"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط")) 
    alternatives = models.ManyToManyField('self', blank=True, verbose_name=_("البدائل المتوافقة"))
    
    ai_suggested_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("سعر السوق المقترح (AI)"))
    ai_price_elasticity = models.DecimalField(max_digits=4, decimal_places=2, default=1.00, editable=False)
    
    history = HistoricalRecords() 
    
    @property
    def total_inventory_qty(self):
        return self.inventory_set.aggregate(Sum('quantity'))['quantity__sum'] or 0
        
    def __str__(self): return f"{self.name} ({self.part_number})"

class ProductPriceHistory(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='price_history')
    old_retail = models.DecimalField(max_digits=10, decimal_places=2)
    new_retail = models.DecimalField(max_digits=10, decimal_places=2)
    old_cost = models.DecimalField(max_digits=10, decimal_places=2)
    new_cost = models.DecimalField(max_digits=10, decimal_places=2)
    change_date = models.DateTimeField(auto_now_add=True)
    class Meta: verbose_name_plural = _("سجل تاريخ الأسعار")

class Inventory(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name=_("القطعة"))
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, verbose_name=_("الفرع"))
    quantity = models.IntegerField(default=0, verbose_name=_("الكمية المتاحة"))
    shelf_location = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("مكان الرف"))
    class Meta:
        verbose_name_plural = "Inventories"
        unique_together = ('product', 'branch')
        constraints = [
            models.CheckConstraint(
                check=models.Q(quantity__gte=0),
                name='inventory_quantity_non_negative',
            ),
        ] 
    def __str__(self): return f"{self.product.name} | {self.branch.name} | QTY: {self.quantity}"

# =====================================================================
# 🏎️ 2. محرك التفكيك والإفرج الجمركي (Scrap & Import Engine)
# =====================================================================
class ScrapDismantlingJob(models.Model):
    job_ref = models.CharField(max_length=50, unique=True, default=uuid.uuid4, verbose_name=_("كود عملية التقطيع"))
    car_model = models.CharField(max_length=100, verbose_name=_("موديل سيارة التقطيع"))
    chassis_number = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("رقم الشاسيه الأصلي"))
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, verbose_name=_("فرع التخزين"))
    total_purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("تكلفة الشراء والاستيراد الكلية"))
    customs_doc = models.FileField(upload_to='customs_docs/', blank=True, null=True, verbose_name=_("مستندات الإفراج الجمركي (PDF)"))
    engine_serial = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("رقم المحرك المفرج عنه"))
    date_dismantled = models.DateField(default=timezone.now, verbose_name=_("تاريخ التفكيك"))
    is_completed = models.BooleanField(default=False, verbose_name=_("تم التفكيك (إضافة للمخزن)"))

    class Meta:
        verbose_name = _("عملية تقطيع / استيراد")
        verbose_name_plural = _("🚢 محرك التفكيك والإفراج الجمركي")
    def __str__(self): return f"{self.car_model} - {self.job_ref[:8]}"

class ScrapDismantlingYield(models.Model):
    job = models.ForeignKey(ScrapDismantlingJob, on_delete=models.CASCADE, related_name='yields')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, limit_choices_to={'condition': 'used'}, verbose_name=_("القطعة المستخرجة"))
    quantity = models.IntegerField(default=1, verbose_name=_("الكمية المستخرجة"))
    estimated_cost_allocation = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("التكلفة التقديرية للقطعة"))

# =====================================================================
# 🛠️ 3. كتالوج الخدمات والمصنعيات
# =====================================================================
class ServiceCatalog(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("اسم الخدمة / الصيانة"))
    labor_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("سعر المصنعية الثابت"))
    estimated_hours = models.DecimalField(max_digits=4, decimal_places=1, default=1.0, verbose_name=_("الوقت التقديري (ساعات)"))
    tech_commission_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_("نسبة عمولة الفني %"))
    
    class Meta:
        verbose_name = _("خدمة صيانة")
        verbose_name_plural = _("كتالوج الخدمات والمصنعيات")
    def __str__(self): return f"{self.name} ({self.labor_price} ج.م)"

# =====================================================================
# 🤝 4. نظام الـ CRM وعقود الـ B2B
# =====================================================================
class Vendor(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("اسم المورد / الشركة"))
    phone = models.CharField(max_length=20, blank=True, null=True, verbose_name=_("رقم الهاتف"))
    tax_id = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("الرقم الضريبي")) 
    company_details = models.TextField(blank=True, null=True, verbose_name=_("تفاصيل الشركة / العنوان"))
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name=_("حساب المورد (مستحقاته)"))
    class Meta:
        verbose_name = _("مورد")
        verbose_name_plural = _("سجل الموردين (SRM)")
    def __str__(self): return self.name

# =====================================================================
# 💰 5. النظام المالي وإدارة الخزائن الإقليمية
# =====================================================================
class StockTransfer(models.Model):
    STATUS_CHOICES = (('pending', _('قيد الانتظار')), ('in_transit', _('في الطريق')), ('completed', _('تم الاستلام')), ('cancelled', _('ملغي')))
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name=_("القطعة"))
    from_branch = models.ForeignKey(Branch, related_name='outgoing_transfers', on_delete=models.CASCADE, verbose_name=_("من فرع"))
    to_branch = models.ForeignKey(Branch, related_name='incoming_transfers', on_delete=models.CASCADE, verbose_name=_("إلى فرع"))
    quantity = models.IntegerField(verbose_name=_("الكمية"))
    date_transferred = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name=_("الحالة"))
    history = HistoricalRecords()


# =====================================================================
# NOTE: All signal handlers have been moved to inventory/signals.py
# which delegates to inventory/services/ (Service Layer pattern).
# Do NOT add @receiver handlers here — they belong in signals.py.
# =====================================================================


# =====================================================================
# 📋 سجل المراجعة والتدقيق (Audit Trail — Immutable Event Log)
# =====================================================================
class InventoryMovement(models.Model):
    REASON_CHOICES = (
        ('sale', _('بيع')),
        ('sale_return', _('مرتجع بيع')),
        ('purchase', _('شراء')),
        ('purchase_return', _('مرتجع شراء')),
        ('transfer_out', _('تحويل صادر')),
        ('transfer_in', _('تحويل وارد')),
        ('adjustment', _('تسوية جرد')),
        ('scrap', _('تقطيع / تالف')),
        ('manual', _('تعديل يدوي')),
    )
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='movements', verbose_name=_("المنتج"))
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, verbose_name=_("الفرع"))
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, verbose_name=_("السبب"))
    quantity_change = models.IntegerField(verbose_name=_("التغيير في الكمية"))
    quantity_before = models.IntegerField(verbose_name=_("الكمية قبل"))
    quantity_after = models.IntegerField(verbose_name=_("الكمية بعد"))
    reference_type = models.CharField(max_length=50, blank=True, verbose_name=_("نوع المرجع"))
    reference_id = models.IntegerField(null=True, blank=True, verbose_name=_("رقم المرجع"))
    note = models.CharField(max_length=255, blank=True, verbose_name=_("ملاحظة"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = _("حركة مخزنية")
        verbose_name_plural = _("سجل حركات المخزون")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['product', '-created_at']),
            models.Index(fields=['branch', '-created_at']),
        ]

    def __str__(self):
        return f"{self.product} | {self.get_reason_display()} | {self.quantity_change:+d}"


class StockAlert(models.Model):
    ALERT_TYPES = (
        ('low_stock', _('مخزون منخفض')),
        ('out_of_stock', _('نفاد تام')),
    )
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='stock_alerts', verbose_name=_("المنتج"))
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, verbose_name=_("الفرع"))
    alert_type = models.CharField(max_length=20, choices=ALERT_TYPES, verbose_name=_("نوع التنبيه"))
    current_quantity = models.IntegerField(verbose_name=_("الكمية الحالية"))
    min_stock_level = models.IntegerField(verbose_name=_("حد الأمان"))
    is_resolved = models.BooleanField(default=False, verbose_name=_("تم الحل"))
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("تنبيه مخزني")
        verbose_name_plural = _("تنبيهات نقص المخزون")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_alert_type_display()} — {self.product} ({self.branch})"


# =====================================================================
# 📥 نظام الاستيراد الآمن (Safe Import with Preview & Rollback)
# =====================================================================
