from django.db import models, transaction, connection
from django.db.models.signals import post_save, pre_save, post_delete
from django.dispatch import receiver
from django.utils import timezone
from django.core.exceptions import ValidationError
from simple_history.models import HistoricalRecords
from django.utils.translation import gettext_lazy as _ 
from decimal import Decimal
from datetime import timedelta
from django.contrib.auth.models import User
from django.db.models import F # 🚀 الدرع المحاسبي الذري
import uuid 
import logging

from django_tenants.utils import schema_context 

logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🏢 1. الإعدادات الأساسية (فروع ومنتجات وموظفين)
# =====================================================================
class Branch(models.Model):
    name = models.CharField(max_length=100, verbose_name=_("اسم الفرع")) 
    location = models.CharField(max_length=255, blank=True, verbose_name=_("الموقع"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("رقم تليفون الفرع"))
    def __str__(self): return self.name

class EmployeeProfile(models.Model):
    ROLE_CHOICES = (
        ('admin', _('مدير عام (أدمن)')),
        ('manager', _('مدير فرع')),
        ('cashier', _('كاشير / استقبال')),
        ('tech', _('فني / ميكانيكي')),
        ('stock', _('أمين مخزن')),
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='employee_profile', verbose_name=_("المستخدم"))
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("الفرع التابع له"))
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='cashier', verbose_name=_("الدور الوظيفي"))
    can_edit_posted_invoices = models.BooleanField(default=False, verbose_name=_("صلاحية تعديل الفواتير المعتمدة؟"))
    commission_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("رصيد العمولات المستحقة"))

    class Meta:
        verbose_name = _("ملف الموظف")
        verbose_name_plural = _("ملفات الموظفين والصلاحيات")
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
            self.total_hours = Decimal(str(max(duration.total_seconds() / 3600.0, 0)))
        super().save(*args, **kwargs)

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
    
    # 🚀 ابتكار: التوافق الدقيق لمنع أخطاء الصرف في الموديلات المعقدة
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
        from django.db.models import Sum
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
class Customer(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("اسم العميل أو الشركة"))
    phone = models.CharField(max_length=20, unique=True, verbose_name=_("رقم الهاتف"))
    is_b2b_company = models.BooleanField(default=False, verbose_name=_("حساب شركة (B2B Fleet)")) 
    tax_id = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("الرقم الضريبي"))
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name=_("الرصيد / المديونية"))
    loyalty_points = models.IntegerField(default=0, verbose_name=_("نقاط الولاء"))
    date_added = models.DateTimeField(auto_now_add=True)

    @property
    def vip_tier(self):
        if self.is_b2b_company: return "🏢 حساب شركة"
        if self.loyalty_points > 5000: return "💎 VIP"
        elif self.loyalty_points > 2000: return "🥇 ذهبي"
        elif self.loyalty_points > 500: return "🥈 فضي"
        return "🥉 عادي"

    class Meta:
        verbose_name = _("عميل / شركة")
        verbose_name_plural = _("سجل العملاء والشركات (CRM)")
    def __str__(self): return f"{self.name} - {self.vip_tier}"

class MaintenanceContract(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, limit_choices_to={'is_b2b_company': True}, related_name='contracts')
    contract_code = models.CharField(max_length=50, unique=True, default=uuid.uuid4)
    start_date = models.DateField(default=timezone.now, verbose_name=_("بداية العقد"))
    end_date = models.DateField(verbose_name=_("نهاية العقد"))
    total_value = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("قيمة العقد السنوية"))
    is_active = models.BooleanField(default=True, verbose_name=_("ساري المفعول"))
    class Meta: verbose_name_plural = _("عقود صيانة الشركات (B2B)")
    def __str__(self): return f"عقد {self.customer.name} - {self.contract_code[:6]}"

class Vehicle(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='vehicles', verbose_name=_("المالك"))
    chassis_number = models.CharField(max_length=17, unique=True, verbose_name=_("رقم الشاسيه (VIN)"))
    car_plate = models.CharField(max_length=20, blank=True, null=True, verbose_name=_("رقم اللوحة"))
    brand = models.CharField(max_length=50, default="BMW", verbose_name=_("ماركة السيارة"))
    model_name = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("الموديل"))
    color = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("لون السيارة"))
    transmission = models.CharField(max_length=20, choices=(('Auto', 'أوتوماتيك'), ('Manual', 'مانيوال')), default='Auto', verbose_name=_("نوع الفتيس"))
    last_mileage = models.IntegerField(default=0, verbose_name=_("آخر قراءة للعداد (كم)"))
    estimated_next_visit = models.DateField(blank=True, null=True, verbose_name=_("موعد الصيانة المتوقع"))
    
    ai_health_score = models.IntegerField(default=100, verbose_name=_("مؤشر صحة المركبة بالـ AI (%)"))
    predicted_failure_notes = models.TextField(blank=True, null=True, verbose_name=_("الأعطال المتوقعة قريباً (AI)"))

    class Meta:
        verbose_name = _("مركبة")
        verbose_name_plural = _("سجل المركبات")
    def __str__(self): return f"{self.car_plate or 'بدون لوحة'} - {self.chassis_number[-6:]}"

class VehicleTelemetryLog(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='telemetry_logs')
    dtc_codes_found = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("أكواد الأعطال المرصودة"))
    battery_voltage = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True, verbose_name=_("فولتية البطارية"))
    raw_json_data = models.JSONField(blank=True, null=True, verbose_name=_("البيانات الخام من جهاز الفحص"))
    
    # 🚀 ابتكار: רادار التدخل السريع للحفاظ على سلامة العميل
    requires_immediate_attention = models.BooleanField(default=False, verbose_name=_("تحذير: عطل حرج يتطلب تدخلاً فورياً"))
    
    timestamp = models.DateTimeField(auto_now_add=True)
    class Meta: verbose_name_plural = _("سجل الفحوصات الرقمية الحية (IoT)")

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
class Treasury(models.Model):
    TYPE_CHOICES = (('cash', _('كاش')), ('bank', _('حساب بنكي')), ('visa', _('فيزا')), ('wallet', _('محفظة')))
    name = models.CharField(max_length=100, verbose_name=_("اسم الخزنة/الحساب"))
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='treasuries', verbose_name=_("الفرع"))
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='cash', verbose_name=_("النوع"))
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00, verbose_name=_("الرصيد الأساسي"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    history = HistoricalRecords()
    class Meta:
        verbose_name = _("خزنة / حساب")
        verbose_name_plural = _("الخزائن والحسابات")
        unique_together = ('name', 'branch')
    def __str__(self): return f"{self.name} ({self.balance})"

class ExpenseCategory(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name=_("بند المصروف"))
    class Meta: verbose_name_plural = _("بنود المصروفات")
    def __str__(self): return self.name

class FinancialTransaction(models.Model):
    TRANSACTION_TYPES = (('in', _('إيداع / إيراد')), ('out', _('سحب / مصروف')))
    CURRENCY_CHOICES = (('EGP', 'جنية مصري'), ('AED', 'درهم إماراتي'), ('USD', 'دولار أمريكي')) 

    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, related_name='transactions', verbose_name=_("الخزنة"))
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES, verbose_name=_("النوع"))
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='EGP', verbose_name=_("العملة"))
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, default=1.0000, verbose_name=_("سعر الصرف وقت العملية"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("المبلغ (بالعملة المحلية)"))
    
    category = models.ForeignKey(ExpenseCategory, null=True, blank=True, on_delete=models.SET_NULL, verbose_name=_("البند"))
    description = models.CharField(max_length=255, verbose_name=_("البيان"))
    date = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))
    
    sale_invoice = models.ForeignKey('SaleInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='payments', verbose_name=_("فاتورة بيع"))
    purchase_invoice = models.ForeignKey('PurchaseInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='payments', verbose_name=_("فاتورة شراء"))
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, verbose_name=_("دفعة من عميل")) 
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL, verbose_name=_("دفعة لمورد")) 
    history = HistoricalRecords()
    class Meta: verbose_name_plural = _("الخزينة (حركات مالية)")
    def __str__(self): return f"{self.amount} {self.currency} - {self.treasury.name}"

# =====================================================================
# 📦 6. الفواتير والعمليات المتطورة (Odoo Standard Workflow)
# =====================================================================
class PurchaseInvoice(models.Model):
    STATUS_CHOICES = (('draft', _('مسودة')), ('posted', _('تم الاستلام (مقفولة)')))
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, verbose_name=_("المورد")) 
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, verbose_name=_("فرع الاستلام")) 
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, verbose_name=_("الدفع من خزنة"))
    
    is_b2b_secured = models.BooleanField(default=False, verbose_name=_("شراء آمن عبر Mouss Tec"))
    bidding_ref = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("مرجع المزاد المالي (Escrow)"))
    
    date_created = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ")) 
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name=_("الإجمالي"))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name=_("المدفوع"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name=_("الحالة")) 
    is_applied = models.BooleanField(default=False, editable=False)

    def update_total(self):
        total = sum((Decimal(str(item.quantity or 0)) * Decimal(str(item.cost_price or 0))) for item in self.items.all())
        self.total_amount = Decimal(str(total))
        self.save(update_fields=['total_amount'])

    def __str__(self): return f"PO #{self.id} - {self.vendor.name}"

class PurchaseInvoiceItem(models.Model):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=1, verbose_name=_("الكمية")) 
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("سعر الشراء")) 
    @property
    def total_price(self): return Decimal(str(self.quantity or 0)) * Decimal(str(self.cost_price or 0))

class SaleInvoice(models.Model):
    INVOICE_TYPES = (('sale', _('بيع قطع غيار')), ('maintenance', _('صيانة شاملة')))
    STATUS_CHOICES = (
        ('quotation', _('عرض سعر (مسودة)')), 
        ('in_progress', _('قيد العمل بالورشة')),
        ('quality_check', _('فحص الجودة (QA)')),
        ('ready', _('جاهز للتسليم')),
        ('posted', _('تم التسليم والاعتماد')),
    )
    
    invoice_type = models.CharField(max_length=20, choices=INVOICE_TYPES, verbose_name=_("النوع"))
    is_return = models.BooleanField(default=False, verbose_name=_("فاتورة مرتجع؟"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='quotation', verbose_name=_("الحالة التشغيلية"))
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, verbose_name=_("العميل"))
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("المركبة"))
    
    maintenance_contract = models.ForeignKey(MaintenanceContract, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("مخصوم من عقد الصيانة"))
    
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, verbose_name=_("الفرع"))
    date_created = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))
    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, null=True, blank=True, verbose_name=_("سداد على خزنة"))
    
    mileage = models.IntegerField(blank=True, null=True, verbose_name=_("قراءة العداد (كم)"))
    notes = models.TextField(blank=True, null=True, verbose_name=_("ملاحظات وشكوى العميل"))
    
    labor_cost_manual = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("مصنعية إضافية عامة"))
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("خصم إجمالي"))
    tax_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_("ضريبة القيمة المضافة %"))
    
    total_core_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, editable=False, verbose_name=_("إجمالي التوالف المستحقة"))
    
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name=_("الإجمالي النهائي"))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name=_("المدفوع"))
    is_applied = models.BooleanField(default=False, editable=False)

    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, editable=False)
    net_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, editable=False)

    @property
    def due_amount(self):
        if self.maintenance_contract: return Decimal('0.00')
        return max(Decimal(str(self.total_amount)) - Decimal(str(self.paid_amount)), Decimal('0.00'))

    def update_total(self):
        items_total_price = Decimal('0.00')
        items_total_cost = Decimal('0.00')
        calculated_core_charge = Decimal('0.00')
        
        if self.items.exists():
            for item in self.items.all():
                items_total_price += (Decimal(str(item.quantity or 0)) * Decimal(str(item.unit_price or 0)))
                items_total_cost += (Decimal(str(item.quantity or 0)) * Decimal(str(item.cost_at_sale or 0)))
                if not item.is_core_returned:
                    calculated_core_charge += (Decimal(str(item.quantity or 0)) * Decimal(str(item.core_charge_applied or 0)))

        services_total_price = sum(Decimal(str(srv.price)) for srv in self.service_items.all()) if self.service_items.exists() else Decimal('0.00')

        subtotal = items_total_price + services_total_price + calculated_core_charge + Decimal(str(self.labor_cost_manual or 0)) - Decimal(str(self.discount or 0))
        tax_amount = (subtotal * Decimal(str(self.tax_percentage or 0))) / Decimal('100.00')
        
        self.total_amount = subtotal + tax_amount
        self.total_cost = items_total_cost
        self.total_core_charge = calculated_core_charge
        self.net_profit = (items_total_price - items_total_cost) + services_total_price + Decimal(str(self.labor_cost_manual or 0)) - Decimal(str(self.discount or 0))

        if self.paid_amount == Decimal('0.00') and self.status == 'posted' and not self.maintenance_contract:
            self.paid_amount = self.total_amount
            
        self.save(update_fields=['total_amount', 'paid_amount', 'total_cost', 'net_profit', 'total_core_charge'])

    def __str__(self): return f"INV #{self.id} - {self.customer.name}"

class SaleInvoiceItem(models.Model):
    invoice = models.ForeignKey(SaleInvoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=1, verbose_name=_("الكمية")) 
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("سعر البيع")) 
    cost_at_sale = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, editable=False)
    
    core_charge_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, editable=False)
    is_core_returned = models.BooleanField(default=False, verbose_name=_("تم استلام القطعة التالفة؟"))
    warranty_end_date = models.DateField(blank=True, null=True, verbose_name=_("تاريخ انتهاء الضمان"))
    
    @property
    def total_price(self): return Decimal(str(self.quantity or 0)) * Decimal(str(self.unit_price or 0))
    
    def save(self, *args, **kwargs):
        if not self.pk and self.product:
            self.cost_at_sale = self.product.average_cost if self.product.average_cost > 0 else self.product.purchase_price
            self.core_charge_applied = self.product.core_charge
            if self.invoice.customer and getattr(self.invoice.customer, 'is_b2b_company', False):
                self.unit_price = self.product.b2b_wholesale_price if self.product.b2b_wholesale_price > 0 else self.product.retail_price
                
            if self.product.warranty_months > 0:
                self.warranty_end_date = timezone.now().date() + timedelta(days=30 * self.product.warranty_months)
        super().save(*args, **kwargs)

class SaleInvoiceServiceItem(models.Model):
    invoice = models.ForeignKey(SaleInvoice, on_delete=models.CASCADE, related_name='service_items')
    service = models.ForeignKey(ServiceCatalog, on_delete=models.PROTECT, verbose_name=_("الخدمة المنفذة"))
    technician = models.ForeignKey(EmployeeProfile, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to={'role': 'tech'}, verbose_name=_("الفني المنفذ"))
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("قيمة المصنعية"))
    
    actual_hours = models.DecimalField(max_digits=4, decimal_places=1, default=1.0, verbose_name=_("ساعات العمل المُباعة"))
    start_time = models.DateTimeField(blank=True, null=True, verbose_name=_("بداية العمل الفعلي"))
    end_time = models.DateTimeField(blank=True, null=True, verbose_name=_("نهاية العمل الفعلي"))

    def save(self, *args, **kwargs):
        if not self.pk and self.service and not self.price:
            self.price = self.service.labor_price
            self.actual_hours = self.service.estimated_hours
        super().save(*args, **kwargs)

class VehicleInspection(models.Model):
    STATUS_COLORS = (('green', _('ممتاز')), ('yellow', _('يحتاج متابعة')), ('red', _('تغيير فوري')))
    invoice = models.OneToOneField(SaleInvoice, on_delete=models.CASCADE, related_name='inspection_report', verbose_name=_("أمر الشغل"))
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, verbose_name=_("السيارة"))
    
    brakes_status = models.CharField(max_length=10, choices=STATUS_COLORS, default='green', verbose_name=_("الفرامل"))
    engine_oil_status = models.CharField(max_length=10, choices=STATUS_COLORS, default='green', verbose_name=_("زيت المحرك"))
    tires_status = models.CharField(max_length=10, choices=STATUS_COLORS, default='green', verbose_name=_("الإطارات"))
    battery_status = models.CharField(max_length=10, choices=STATUS_COLORS, default='green', verbose_name=_("البطارية"))
    technician_notes = models.TextField(blank=True, verbose_name=_("ملاحظات الفني"))
    attachment = models.ImageField(upload_to='inspections/', blank=True, null=True, verbose_name=_("صورة إثبات التلف"))
    inspection_timestamp = models.DateTimeField(auto_now_add=True, verbose_name=_("بصمة زمنية للفحص"))
    class Meta: verbose_name = _("فحص رقمي وتوثيق مرئي")

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
# 🧠 الإشارات الذكية والأتمتة (Atomic Smart Automation Signals)
# =====================================================================

@receiver(post_save, sender=User)
def create_employee_profile(sender, instance, created, **kwargs):
    if created and connection.schema_name != 'public':
        EmployeeProfile.objects.get_or_create(user=instance)

@receiver(pre_save, sender=Product)
def track_product_price_changes(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = Product.objects.get(pk=instance.pk)
            if old.retail_price != instance.retail_price or old.average_cost != instance.average_cost:
                ProductPriceHistory.objects.create(
                    product=instance,
                    old_retail=old.retail_price, new_retail=instance.retail_price,
                    old_cost=old.average_cost, new_cost=instance.average_cost
                )
        except Product.DoesNotExist: pass

# 🚀 وكيل مزامنة سوق B2B (Mouss Tec Central Marketplace Agent)
@receiver(post_save, sender=Product)
def sync_b2b_marketplace(sender, instance, **kwargs):
    if connection.schema_name == 'public': return 

    try:
        from django.apps import apps
        with schema_context('public'):
            GlobalB2BMarketplace = apps.get_model('clients', 'GlobalB2BMarketplace')
            Client = apps.get_model('clients', 'Client')
            
            tenant = Client.objects.filter(schema_name=connection.schema_name).first()
            if not tenant: return

            total_qty = instance.total_inventory_qty
            
            if instance.is_b2b_published and instance.is_active and total_qty > 0:
                GlobalB2BMarketplace.objects.update_or_create(
                    tenant=tenant,
                    part_number=instance.part_number,
                    condition=instance.condition,
                    defaults={
                        'product_name': instance.name,
                        'brand': instance.brand,
                        'wholesale_price': instance.b2b_wholesale_price if instance.b2b_wholesale_price > 0 else instance.retail_price,
                        'available_qty': total_qty,
                    }
                )
                logger.info(f"🌐 [B2B AGENT]: Synced '{instance.part_number}' to central market. Qty: {total_qty}")
            else:
                deleted, _ = GlobalB2BMarketplace.objects.filter(
                    tenant=tenant, part_number=instance.part_number, condition=instance.condition
                ).delete()
                if deleted: logger.info(f"🛑 [B2B AGENT]: Removed '{instance.part_number}' from central market.")
    except Exception as e:
        logger.error(f"🔴 [B2B AGENT ERROR]: Market sync failed for '{instance.part_number}' - {e}")

@receiver(post_save, sender=ScrapDismantlingJob)
def execute_scrap_dismantling_yield(sender, instance, **kwargs):
    if instance.is_completed and not getattr(instance, '_yield_processed', False):
        instance._yield_processed = True
        with transaction.atomic():
            for yield_item in instance.yields.all():
                product = yield_item.product
                
                if instance.branch:
                    inv, _ = Inventory.objects.get_or_create(product=product, branch=instance.branch, defaults={'quantity': 0})
                    Inventory.objects.filter(pk=inv.pk).update(quantity=F('quantity') + yield_item.quantity) # 🚀 تحديث آمن

                total_current_qty = product.total_inventory_qty
                old_value = Decimal(str(max(total_current_qty - yield_item.quantity, 0))) * Decimal(str(product.average_cost))
                new_value = Decimal(str(yield_item.quantity)) * Decimal(str(yield_item.estimated_cost_allocation))
                
                if total_current_qty > 0:
                    Product.objects.filter(pk=product.pk).update(
                        average_cost=(old_value + new_value) / Decimal(str(total_current_qty)),
                        purchase_price=yield_item.estimated_cost_allocation 
                    )

@receiver(pre_save, sender=SaleInvoiceItem)
def handle_core_charge_refund(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = SaleInvoiceItem.objects.get(pk=instance.pk)
            if not old_instance.is_core_returned and instance.is_core_returned:
                if instance.core_charge_applied > 0 and instance.invoice.customer:
                    with transaction.atomic():
                        total_refund = Decimal(str(instance.quantity)) * instance.core_charge_applied
                        
                        # 🚀 إغلاق الثغرة المحاسبية (Race Condition)
                        Customer.objects.filter(pk=instance.invoice.customer.pk).update(balance=F('balance') - total_refund)
                        
                        if instance.invoice.treasury:
                            FinancialTransaction.objects.create(
                                treasury=instance.invoice.treasury, transaction_type='out',
                                amount=total_refund, description=f"استرداد تأمين كور لقطعة {instance.product.name} (INV #{instance.invoice.id})",
                                customer=instance.invoice.customer
                            )
        except SaleInvoiceItem.DoesNotExist: pass

# 🛡️ إشارات تحديث الفواتير
@receiver(post_save, sender=SaleInvoiceItem)
@receiver(post_delete, sender=SaleInvoiceItem)
@receiver(post_save, sender=SaleInvoiceServiceItem)
@receiver(post_delete, sender=SaleInvoiceServiceItem)
def auto_update_sale_invoice_items(sender, instance, **kwargs):
    if hasattr(instance, 'invoice') and instance.invoice: 
        instance.invoice.update_total()

@receiver(post_save, sender=PurchaseInvoiceItem)
@receiver(post_delete, sender=PurchaseInvoiceItem)
def auto_update_purchase_invoice_items(sender, instance, **kwargs):
    if hasattr(instance, 'invoice') and instance.invoice: 
        instance.invoice.update_total()

@receiver(post_save, sender=FinancialTransaction)
def update_treasury_balance(sender, instance, created, **kwargs):
    """🚀 تحديث ذري (Atomic Update) لمنع تدمير حسابات الخزينة والموردين والعملاء"""
    if created:
        with transaction.atomic():
            amount = Decimal(str(instance.amount)) 
            
            if instance.transaction_type == 'in': 
                Treasury.objects.filter(pk=instance.treasury.pk).update(balance=F('balance') + amount)
            elif instance.transaction_type == 'out': 
                Treasury.objects.filter(pk=instance.treasury.pk).update(balance=F('balance') - amount)
            
            if instance.customer and instance.transaction_type == 'in':
                Customer.objects.filter(pk=instance.customer.pk).update(balance=F('balance') - amount)
                
            if instance.vendor and instance.transaction_type == 'out':
                Vendor.objects.filter(pk=instance.vendor.pk).update(balance=F('balance') - amount)

@receiver(post_save, sender=SaleInvoice)
def execute_sale_stock_and_finance(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        with transaction.atomic():
            if not instance.maintenance_contract:
                if instance.treasury and instance.paid_amount > 0 and not instance.payments.exists():
                    FinancialTransaction.objects.create(
                        treasury=instance.treasury, transaction_type='in',
                        amount=instance.paid_amount, description=f"إيراد مبيعات/صيانة INV #{instance.id}",
                        sale_invoice=instance, customer=instance.customer
                    )
                
                if instance.due_amount > Decimal('0.00'):
                    Customer.objects.filter(pk=instance.customer.pk).update(balance=F('balance') + instance.due_amount)

            for service_item in instance.service_items.select_related('technician', 'service'):
                if service_item.technician and service_item.service.tech_commission_percent > 0:
                    commission = (service_item.price * service_item.service.tech_commission_percent) / Decimal('100.00')
                    EmployeeProfile.objects.filter(pk=service_item.technician.pk).update(commission_balance=F('commission_balance') + commission)

            if instance.vehicle and instance.mileage and instance.mileage > instance.vehicle.last_mileage:
                instance.vehicle.last_mileage = instance.mileage
                instance.vehicle.estimated_next_visit = timezone.now().date() + timedelta(days=120)
                instance.vehicle.save(update_fields=['last_mileage', 'estimated_next_visit'])
                
            if instance.total_amount > 0 and not instance.is_return:
                points_earned = int(instance.total_amount / 100)
                Customer.objects.filter(pk=instance.customer.pk).update(loyalty_points=F('loyalty_points') + points_earned)

            # 📦 وكيل المخازن الآمن
            for item in instance.items.select_related('product'):
                inv, _ = Inventory.objects.get_or_create(product=item.product, branch=instance.branch, defaults={'quantity': 0})
                if instance.is_return: 
                    Inventory.objects.filter(pk=inv.pk).update(quantity=F('quantity') + item.quantity)
                else: 
                    Inventory.objects.filter(pk=inv.pk).update(quantity=F('quantity') - item.quantity)
                
                item.product.save(update_fields=['ai_price_elasticity']) 
                
            SaleInvoice.objects.filter(pk=instance.pk).update(is_applied=True)

@receiver(post_save, sender=PurchaseInvoice)
def execute_purchase_stock_and_finance(sender, instance, **kwargs):
    if instance.status == 'posted' and not instance.is_applied:
        with transaction.atomic():
            if instance.treasury and instance.paid_amount > 0 and not instance.payments.exists():
                FinancialTransaction.objects.create(
                    treasury=instance.treasury, transaction_type='out',
                    amount=instance.paid_amount, description=f"سداد مشتريات PO #{instance.id}",
                    purchase_invoice=instance, vendor=instance.vendor
                )
            
            due = Decimal(str(instance.total_amount)) - Decimal(str(instance.paid_amount))
            if due > Decimal('0.00'):
                Vendor.objects.filter(pk=instance.vendor.pk).update(balance=F('balance') + due)

            for item in instance.items.select_related('product'):
                product = item.product
                
                inv, _ = Inventory.objects.get_or_create(product=product, branch=instance.branch, defaults={'quantity': 0})
                Inventory.objects.filter(pk=inv.pk).update(quantity=F('quantity') + item.quantity)

                total_current_qty = product.total_inventory_qty
                old_value = Decimal(str(max(total_current_qty - item.quantity, 0))) * Decimal(str(product.average_cost))
                new_value = Decimal(str(item.quantity)) * Decimal(str(item.cost_price))
                
                if total_current_qty > 0:
                    Product.objects.filter(pk=product.pk).update(
                        average_cost=(old_value + new_value) / Decimal(str(total_current_qty)),
                        purchase_price=item.cost_price
                    )
                
            PurchaseInvoice.objects.filter(pk=instance.pk).update(is_applied=True)

@receiver(pre_save, sender=StockTransfer)
def execute_stock_transfer(sender, instance, **kwargs):
    if instance.id:
        old_instance = StockTransfer.objects.get(id=instance.id)
        
        if old_instance.status == 'pending' and instance.status == 'in_transit':
            with transaction.atomic():
                from_inv = Inventory.objects.get(product=instance.product, branch=instance.from_branch)
                if from_inv.quantity < instance.quantity: raise ValidationError("الكمية لا تكفي للتحويل!")
                Inventory.objects.filter(pk=from_inv.pk).update(quantity=F('quantity') - instance.quantity)
                
        elif old_instance.status == 'in_transit' and instance.status == 'completed':
            with transaction.atomic():
                to_inv, _ = Inventory.objects.get_or_create(product=instance.product, branch=instance.to_branch, defaults={'quantity': 0})
                Inventory.objects.filter(pk=to_inv.pk).update(quantity=F('quantity') + instance.quantity)
                
        elif old_instance.status == 'in_transit' and instance.status == 'cancelled':
            with transaction.atomic():
                from_inv = Inventory.objects.get(product=instance.product, branch=instance.from_branch)
                Inventory.objects.filter(pk=from_inv.pk).update(quantity=F('quantity') + instance.quantity)