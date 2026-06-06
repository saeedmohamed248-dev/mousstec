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
        # Defensive: loyalty_points may be a CombinedExpression (F()) right after
        # save() with F() updates — refresh from DB if so to get the real int.
        pts = self.loyalty_points
        try:
            pts = int(pts)
        except (TypeError, ValueError):
            try:
                self.refresh_from_db(fields=['loyalty_points'])
                pts = int(self.loyalty_points)
            except Exception:
                return "🥉 عادي"
        if pts > 5000: return "💎 VIP"
        elif pts > 2000: return "🥇 ذهبي"
        elif pts > 500: return "🥈 فضي"
        return "🥉 عادي"

    class Meta:
        verbose_name = _("عميل / شركة")
        verbose_name_plural = _("سجل العملاء والشركات (CRM)")

    def save(self, *args, **kwargs):
        if self.phone:
            import re as _re
            phone = _re.sub(r'[\s\-\(\)]+', '', self.phone)
            if phone.startswith('00'):
                phone = '+' + phone[2:]
            elif phone.startswith('0') and not phone.startswith('+'):
                phone = '+2' + phone 
            self.phone = phone
        super().save(*args, **kwargs)

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
    SYSTEM_KEYS = (
        ('salaries',  _('رواتب وأجور')),
        ('rent',      _('إيجار')),
        ('utilities', _('مرافق')),
        ('other',     _('أخرى')),
    )
    name = models.CharField(max_length=100, unique=True, verbose_name=_("بند المصروف"))
    system_key = models.CharField(
        max_length=30, choices=SYSTEM_KEYS, blank=True, db_index=True,
        verbose_name=_("مفتاح النظام"),
        help_text=_("مفتاح ثابت للتعرف الآلي — مثلاً 'salaries' يفعّل قائمة الموظفين تلقائياً"),
    )
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

    employee = models.ForeignKey(EmployeeProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name='financial_transactions', verbose_name=_("الموظف (للرواتب/السلف)"))

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
        # 🚀 [FIX BY QA]: التحديث لمعالجة O(1) Aggregate بدلاً من الـ Loops المرهقة للسيرفر
        agg = self.items.aggregate(
            total=Sum(ExpressionWrapper(F('quantity') * F('cost_price'), output_field=DecimalField()))
        )
        self.total_amount = agg['total'] or Decimal('0.00')
        self.save(update_fields=['total_amount'])

    def __str__(self): return f"PO #{self.id} - {self.vendor.name}"

class PurchaseInvoiceItem(models.Model):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=1, verbose_name=_("الكمية"), validators=[MinValueValidator(1, message="الكمية يجب أن تكون 1 على الأقل")])
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("سعر الشراء"), validators=[MinValueValidator(Decimal('0.01'), message="سعر الشراء يجب أن يكون أكبر من صفر")]) 
    @property
    def total_price(self): return Decimal(str(self.quantity or 0)) * Decimal(str(self.cost_price or 0))

class SaleInvoice(models.Model):
    INVOICE_TYPES = (('sale', _('بيع قطع غيار')), ('maintenance', _('صيانة شاملة')))
    STATUS_CHOICES = (
        ('quotation', _('عرض سعر (مسودة)')), 
        ('in_progress', _('قيد العمل بالمركز')),
        ('quality_check', _('فحص الجودة (QA)')),
        ('ready', _('جاهز للتسليم')),
        ('posted', _('تم التسليم والاعتماد')),
    )
    
    invoice_type = models.CharField(max_length=20, choices=INVOICE_TYPES, verbose_name=_("النوع"))
    is_return = models.BooleanField(default=False, verbose_name=_("فاتورة مرتجع؟"))
    original_invoice = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='return_invoices', verbose_name=_("الفاتورة الأصلية (للمرتجع)"),
    )
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
        # 🚀 [FIX BY QA]: نقل حساب إجمالي الفاتورة إلى الـ DB Engine مباشرة (O(1)) بدلاً من لوب بايثون 
        # هذا ينهي تماماً أزمة استنزاف المعالج للعمليات الضخمة ويُسرع الحفظ
        items_agg = self.items.aggregate(
            t_price=Sum(ExpressionWrapper(F('quantity') * F('unit_price'), output_field=DecimalField())),
            t_cost=Sum(ExpressionWrapper(F('quantity') * F('cost_at_sale'), output_field=DecimalField())),
            t_core=Sum(ExpressionWrapper(F('quantity') * F('core_charge_applied'), output_field=DecimalField()), filter=Q(is_core_returned=False))
        )
        
        items_total_price = items_agg['t_price'] or Decimal('0.00')
        items_total_cost = items_agg['t_cost'] or Decimal('0.00')
        calculated_core_charge = items_agg['t_core'] or Decimal('0.00')

        services_agg = self.service_items.aggregate(t_srv=Sum('price'))
        services_total_price = services_agg['t_srv'] or Decimal('0.00')

        subtotal = items_total_price + services_total_price + calculated_core_charge + Decimal(str(self.labor_cost_manual or 0)) - Decimal(str(self.discount or 0))
        tax_amount = (subtotal * Decimal(str(self.tax_percentage or 0))) / Decimal('100.00')
        
        self.total_amount = subtotal + tax_amount
        self.total_cost = items_total_cost
        self.total_core_charge = calculated_core_charge
        # Net profit: revenue minus cost, minus tax (tax is paid to government, not profit)
        gross_margin = (items_total_price - items_total_cost) + services_total_price + Decimal(str(self.labor_cost_manual or 0)) - Decimal(str(self.discount or 0))
        self.net_profit = gross_margin - tax_amount

        if self.paid_amount == Decimal('0.00') and self.status == 'posted' and not self.maintenance_contract:
            self.paid_amount = self.total_amount
            
        self.save(update_fields=['total_amount', 'paid_amount', 'total_cost', 'net_profit', 'total_core_charge'])

    def __str__(self): return f"INV #{self.id} - {self.customer.name}"

class SaleInvoiceItem(models.Model):
    invoice = models.ForeignKey(SaleInvoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=1, verbose_name=_("الكمية"), validators=[MinValueValidator(1, message="الكمية يجب أن تكون 1 على الأقل")])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("سعر البيع"), validators=[MinValueValidator(Decimal('0.00'), message="السعر لا يمكن أن يكون سالباً")]) 
    cost_at_sale = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, editable=False)
    
    core_charge_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, editable=False)
    is_core_returned = models.BooleanField(default=False, verbose_name=_("تم استلام القطعة التالفة؟"))
    warranty_end_date = models.DateField(blank=True, null=True, verbose_name=_("تاريخ انتهاء الضمان"))

    # 💰 Pillar 3 — Sales commission attribution
    salesperson = models.ForeignKey(
        EmployeeProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sold_items',
        limit_choices_to={'role__in': ['sales', 'cashier', 'manager']},
        verbose_name=_("بائع القطعة (للعمولة)"),
    )
    commission_accrued = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00, editable=False,
        verbose_name=_("العمولة المُحتسبة"),
    )
    
    @property
    def total_price(self): return Decimal(str(self.quantity or 0)) * Decimal(str(self.unit_price or 0))
    
    def clean(self):
        super().clean()
        if self.quantity is not None and self.quantity < 1:
            raise ValidationError({'quantity': 'الكمية يجب أن تكون 1 على الأقل'})
        if self.unit_price is not None and self.unit_price < 0:
            raise ValidationError({'unit_price': 'السعر لا يمكن أن يكون سالباً'})

        # 🛡️ [FIX BY QA]: فحص توفر المخزون الحي قبل الحفظ لتلافي الـ 500 Error من قاعدة البيانات
        # المرتجع يضيف مخزون فلا يحتاج لفحص التوفر
        is_return = False
        try:
            if hasattr(self, 'invoice') and self.invoice:
                is_return = getattr(self.invoice, 'is_return', False)
        except Exception:
            pass
        if hasattr(self, 'product') and self.product and self.quantity and not is_return:
            branch = None
            try:
                if hasattr(self, 'invoice') and self.invoice:
                    branch = self.invoice.branch
            except Exception: pass
            
            if branch:
                inv = Inventory.objects.filter(product=self.product, branch=branch).first()
                available_qty = inv.quantity if inv else 0
                
                if self.pk:
                    old_qty = SaleInvoiceItem.objects.filter(pk=self.pk).values_list('quantity', flat=True).first() or 0
                    available_qty += old_qty
                    
                if self.quantity > available_qty:
                    raise ValidationError({
                        'quantity': f'المخزون بفرع ({branch.name}) غير كافٍ لإتمام الصرف. المتاح حالياً هو: {available_qty} وحدة فقط.'
                    })

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
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name=_("قيمة المصنعية"),
                                help_text=_("اتركه فارغاً ليتم ملؤه تلقائياً من كتالوج الخدمات"))

    actual_hours = models.DecimalField(max_digits=4, decimal_places=1, default=1.0, verbose_name=_("ساعات العمل المُباعة"))
    start_time = models.DateTimeField(blank=True, null=True, verbose_name=_("بداية العمل الفعلي"))
    end_time = models.DateTimeField(blank=True, null=True, verbose_name=_("نهاية العمل الفعلي"))

    def save(self, *args, **kwargs):
        # Auto-fill price from service catalog if not provided
        if self.service and (self.price is None or self.price == Decimal('0.00')):
            self.price = self.service.labor_price
            self.actual_hours = self.service.estimated_hours
        # Safety: ensure price is never None at DB level
        if self.price is None:
            self.price = Decimal('0.00')
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
# NOTE: All signal handlers have been moved to inventory/signals.py
# which delegates to inventory/services/ (Service Layer pattern).
# Do NOT add @receiver handlers here — they belong in signals.py.
# =====================================================================


# =====================================================================
# 📋 سجل المراجعة والتدقيق (Audit Trail — Immutable Event Log)
# =====================================================================
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
class ChartOfAccount(models.Model):
    ACCOUNT_TYPES = (
        ('asset', _('أصول (Assets)')),
        ('liability', _('خصوم (Liabilities)')),
        ('equity', _('حقوق ملكية (Equity)')),
        ('revenue', _('إيرادات (Revenue)')),
        ('expense', _('مصروفات (Expenses)')),
    )
    code = models.CharField(max_length=20, unique=True, verbose_name=_("رقم الحساب"))
    name = models.CharField(max_length=200, verbose_name=_("اسم الحساب"))
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES, verbose_name=_("نوع الحساب"))
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='children', verbose_name=_("الحساب الأب"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    description = models.TextField(blank=True, verbose_name=_("وصف"))

    class Meta:
        verbose_name = _("حساب محاسبي")
        verbose_name_plural = _("دليل الحسابات (Chart of Accounts)")
        ordering = ['code']

    def __str__(self):
        return f"{self.code} — {self.name}"

    @property
    def balance(self):
        agg = self.entries.aggregate(
            total_debit=models.Sum('debit'),
            total_credit=models.Sum('credit')
        )
        d = agg['total_debit'] or Decimal('0')
        c = agg['total_credit'] or Decimal('0')
        if self.account_type in ('asset', 'expense'):
            return d - c
        return c - d


class AccountingEntry(models.Model):
    entry_date = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("تاريخ القيد"))
    reference = models.CharField(max_length=100, db_index=True, verbose_name=_("المرجع"))
    description = models.CharField(max_length=255, verbose_name=_("البيان"))
    account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name='entries', verbose_name=_("الحساب"))
    debit = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'), verbose_name=_("مدين"))
    credit = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'), verbose_name=_("دائن"))
    sale_invoice = models.ForeignKey('SaleInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='accounting_entries')
    purchase_invoice = models.ForeignKey('PurchaseInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='accounting_entries')
    financial_transaction = models.ForeignKey('FinancialTransaction', null=True, blank=True, on_delete=models.SET_NULL, related_name='accounting_entries')
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = _("قيد محاسبي")
        verbose_name_plural = _("القيود المحاسبية (Accounting Ledger)")
        ordering = ['-entry_date']
        indexes = [
            models.Index(fields=['reference']),
            models.Index(fields=['account', '-entry_date']),
        ]

    def clean(self):
        """Validate that either debit or credit is set, not both."""
        if self.debit > 0 and self.credit > 0:
            raise ValidationError(_("القيد لا يمكن أن يكون مدين ودائن في نفس الوقت."))
        if self.debit == 0 and self.credit == 0:
            raise ValidationError(_("القيد يجب أن يحتوي على قيمة مدينة أو دائنة."))

    @classmethod
    def validate_balanced(cls, reference):
        """Verify all entries for a given reference are balanced (total debit == total credit)."""
        agg = cls.objects.filter(reference=reference).aggregate(
            total_debit=models.Sum('debit'),
            total_credit=models.Sum('credit')
        )
        total_debit = agg['total_debit'] or Decimal('0')
        total_credit = agg['total_credit'] or Decimal('0')
        if total_debit != total_credit:
            raise ValidationError(
                _(f"القيود غير متوازنة للمرجع {reference}: "
                  f"مدين={total_debit}, دائن={total_credit}")
            )
        return True

    def __str__(self):
        side = f"مدين {self.debit}" if self.debit > 0 else f"دائن {self.credit}"
        return f"{self.reference} | {self.account.name} | {side}"


# =====================================================================
# 🏦 المطابقة البنكية (Bank Reconciliation)
# =====================================================================
class BankStatement(models.Model):
    """كشف بنكي مستورد من البنك — لمطابقته مع حركات الخزينة."""
    treasury = models.ForeignKey(
        'Treasury', on_delete=models.CASCADE, related_name='bank_statements',
        verbose_name=_("الخزينة / الحساب البنكي")
    )
    statement_date = models.DateField(verbose_name=_("تاريخ الكشف"))
    period_start = models.DateField(verbose_name=_("بداية الفترة"))
    period_end = models.DateField(verbose_name=_("نهاية الفترة"))
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, verbose_name=_("الرصيد الافتتاحي"))
    closing_balance = models.DecimalField(max_digits=15, decimal_places=2, verbose_name=_("الرصيد الختامي"))
    uploaded_file = models.FileField(upload_to='bank_statements/%Y/%m/', blank=True, null=True)
    is_reconciled = models.BooleanField(default=False, verbose_name=_("تمت المطابقة"))
    reconciled_at = models.DateTimeField(null=True, blank=True)
    reconciled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("كشف بنكي")
        verbose_name_plural = _("🏦 كشوف البنوك")
        ordering = ['-statement_date']

    def __str__(self):
        return f"كشف {self.treasury.name} — {self.statement_date}"


class BankStatementLine(models.Model):
    """سطر واحد من الكشف البنكي."""
    DIRECTION_CHOICES = (
        ('debit', _('سحب (مدين)')),
        ('credit', _('إيداع (دائن)')),
    )
    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE, related_name='lines')
    transaction_date = models.DateField()
    description = models.CharField(max_length=300)
    reference = models.CharField(max_length=100, blank=True, db_index=True, verbose_name=_("مرجع البنك"))
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)

    # Reconciliation linkage
    matched_transaction = models.ForeignKey(
        'FinancialTransaction', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='bank_lines', verbose_name=_("الحركة المالية المطابقة"),
    )
    match_confidence = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        help_text=_("0-100 — ثقة المطابقة التلقائية")
    )
    is_matched = models.BooleanField(default=False, db_index=True)
    matched_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = _("سطر كشف بنكي")
        verbose_name_plural = _("سطور كشوف البنوك")
        ordering = ['transaction_date', 'pk']
        indexes = [
            models.Index(fields=['statement', 'is_matched']),
            models.Index(fields=['transaction_date', 'amount']),
        ]

    def __str__(self):
        sign = '+' if self.direction == 'credit' else '-'
        return f"{self.transaction_date} | {sign}{self.amount} | {self.description[:50]}"

    def auto_match(self):
        """
        🤖 محاولة مطابقة تلقائية مع حركة في FinancialTransaction.
        يبحث بنفس التاريخ ±3 أيام ونفس المبلغ.
        يرجع الـ confidence score (0-100).
        """
        from datetime import timedelta as _td
        if self.is_matched:
            return 100

        target_type = 'in' if self.direction == 'credit' else 'out'
        candidates = FinancialTransaction.objects.filter(
            treasury=self.statement.treasury,
            transaction_type=target_type,
            amount=self.amount,
            date__date__gte=self.transaction_date - _td(days=3),
            date__date__lte=self.transaction_date + _td(days=3),
        ).exclude(bank_lines__is_matched=True)

        # Best match: same date + amount = 100% confidence
        exact = candidates.filter(date__date=self.transaction_date).first()
        if exact:
            self.matched_transaction = exact
            self.match_confidence = Decimal('100.00')
            self.is_matched = True
            self.matched_at = timezone.now()
            self.save(update_fields=['matched_transaction', 'match_confidence', 'is_matched', 'matched_at'])
            return 100

        # Near match: same amount within ±3 days = 80%
        near = candidates.first()
        if near:
            self.matched_transaction = near
            self.match_confidence = Decimal('80.00')
            self.is_matched = True
            self.matched_at = timezone.now()
            self.save(update_fields=['matched_transaction', 'match_confidence', 'is_matched', 'matched_at'])
            return 80

        return 0


# =====================================================================
# 📦 سجل حركات المخزون (Inventory Movement Tracker)
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


class VehicleDiagnosticReport(models.Model):
    """OBD scan attached to a Job Card (SaleInvoice with invoice_type='maintenance').
    Mobile app POSTs JSON to /api/obd/ingest/ with VIN + fault_codes + live PIDs."""
    SCAN_CHOICES = (
        ('pre_repair',  _('فحص قبل الإصلاح')),
        ('post_repair', _('فحص بعد الإصلاح / تأكيد جودة')),
        ('ad_hoc',      _('فحص خارجي')),
    )

    job_card = models.ForeignKey(
        'SaleInvoice', on_delete=models.CASCADE, null=True, blank=True,
        related_name='diagnostic_reports',
        verbose_name=_("بطاقة الإصلاح المرتبطة"),
    )
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.PROTECT, related_name='diagnostic_reports',
        verbose_name=_("المركبة"),
    )
    engineer = models.ForeignKey(
        EmployeeProfile, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['engineer', 'tech']},
        related_name='diagnostic_reports',
        verbose_name=_("المهندس / الفني"),
    )

    scan_type = models.CharField(max_length=20, choices=SCAN_CHOICES, verbose_name=_("نوع الفحص"))
    scanned_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("وقت الفحص"))

    fault_codes = models.JSONField(default=list, blank=True, verbose_name=_("أكواد الأعطال (DTCs)"),
        help_text=_("مصفوفة مثل: ['P0171', 'P0300']"))
    live_data = models.JSONField(default=dict, blank=True, verbose_name=_("القراءات الحية (PIDs)"),
        help_text=_("RPM, coolant_temp_c, maf_gs, ..."))

    device_id = models.CharField(max_length=80, blank=True, verbose_name=_("معرّف جهاز المسح"))
    raw_payload = models.JSONField(default=dict, blank=True, editable=False)

    severity_score = models.IntegerField(default=0, verbose_name=_("درجة الخطورة (0-100)"))

    # ── Source provenance + AI analysis (Diagnostics Room save flow) ──
    SOURCE_MOBILE_INGEST = 'mobile_ingest'
    SOURCE_DIAG_ROOM = 'diag_room'
    SOURCE_CHOICES = (
        (SOURCE_MOBILE_INGEST, _('OBD مباشر من الموبايل')),
        (SOURCE_DIAG_ROOM,     _('غرفة تشخيص الأعطال (Web Bluetooth)')),
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MOBILE_INGEST,
        verbose_name=_("مصدر التقرير"),
    )
    vin_snapshot = models.CharField(
        max_length=17, blank=True, verbose_name=_("VIN المقروء من السيارة"),
        help_text=_("VIN كما قرأه الـ ELM327 من السيارة — للتدقيق."),
    )
    ai_summary = models.TextField(
        blank=True, verbose_name=_("ملخص تحليل الذكاء الاصطناعي"),
        help_text=_("التحليل النهائي اللي هيشوفه مستشار الخدمة والعميل."),
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='diagnostic_reports_created',
        verbose_name=_("أنشأه"),
    )

    class Meta:
        verbose_name = _("تقرير OBD")
        verbose_name_plural = _("تقارير OBD التشخيصية")
        indexes = [
            models.Index(fields=['vehicle', '-scanned_at']),
            models.Index(fields=['job_card', 'source']),
        ]

    def __str__(self):
        return f"OBD #{self.id} — {self.vehicle} ({self.get_scan_type_display()})"


def _diag_photo_upload_path(instance, filename):
    """Group photos by report id + year/month so an advisor browsing a job
    card doesn't hit a single 100k-file flat folder."""
    from django.utils import timezone as _tz
    now = _tz.now()
    return (
        f'diagnostics/{now:%Y/%m}/report_{instance.report_id or "new"}/'
        f'{filename}'
    )


class VehicleDiagnosticPhoto(models.Model):
    """Photo evidence attached to a VehicleDiagnosticReport.

    Typical use: technician snaps a melted connector / cut harness in the
    AI Diagnostics Room. We persist the bytes so the service advisor can
    show the customer exactly what justified the labour line.
    """
    report = models.ForeignKey(
        VehicleDiagnosticReport, on_delete=models.CASCADE,
        related_name='photos', verbose_name=_("التقرير"),
    )
    image = models.ImageField(
        upload_to=_diag_photo_upload_path, verbose_name=_("الصورة"),
    )
    caption = models.CharField(
        max_length=240, blank=True, verbose_name=_("وصف مختصر"),
    )
    uploaded_at = models.DateTimeField(
        default=timezone.now, db_index=True, verbose_name=_("وقت الرفع"),
    )

    class Meta:
        verbose_name = _("صورة تشخيص")
        verbose_name_plural = _("صور التشخيص")
        ordering = ['uploaded_at']

    def __str__(self):
        return f"Photo #{self.id} — report #{self.report_id}"


class RepairLog(models.Model):
    """Technician's per-task work log on a Job Card — drives the Tech Workspace timer & flags."""
    STATUS_CHOICES = (
        ('open',    _('قيد التنفيذ')),
        ('paused',  _('متوقف مؤقتاً')),
        ('done',    _('مكتمل')),
        ('blocked', _('بانتظار قطع إضافية')),
    )

    job_card = models.ForeignKey(
        'SaleInvoice', on_delete=models.CASCADE, related_name='repair_logs',
        verbose_name=_("بطاقة الإصلاح"),
    )
    technician = models.ForeignKey(
        EmployeeProfile, on_delete=models.PROTECT, related_name='repair_logs',
        limit_choices_to={'role__in': ['tech', 'engineer']},
        verbose_name=_("الفني"),
    )

    task_title = models.CharField(max_length=160, verbose_name=_("عنوان المهمة"))
    tech_notes = models.TextField(blank=True, verbose_name=_("ملاحظات فنية"))

    started_at = models.DateTimeField(default=timezone.now, verbose_name=_("وقت البدء"))
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name=_("وقت الانتهاء"))
    paused_seconds = models.IntegerField(default=0, verbose_name=_("ثوانٍ توقف"))
    last_paused_at = models.DateTimeField(null=True, blank=True, editable=False)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='open', verbose_name=_("الحالة"))
    needs_extra_parts = models.BooleanField(default=False, verbose_name=_("يحتاج قطع إضافية؟"))
    extra_parts_note = models.TextField(blank=True, verbose_name=_("تفاصيل القطع المطلوبة"))

    class Meta:
        verbose_name = _("سجل إصلاح")
        verbose_name_plural = _("سجلات الإصلاح (Tech)")
        indexes = [models.Index(fields=['job_card', '-started_at']),
                   models.Index(fields=['technician', 'status'])]

    @property
    def duration_minutes(self) -> int:
        end = self.ended_at or timezone.now()
        total = (end - self.started_at).total_seconds() - (self.paused_seconds or 0)
        return max(int(total // 60), 0)

    def __str__(self):
        return f"RepairLog #{self.id} — {self.task_title} ({self.get_status_display()})"


class RepairLogMedia(models.Model):
    MEDIA_KIND = (
        ('before', _('قبل الإصلاح')),
        ('after',  _('بعد الإصلاح')),
        ('issue',  _('مشكلة/تحذير')),
    )
    log = models.ForeignKey(RepairLog, on_delete=models.CASCADE, related_name='media')
    kind = models.CharField(max_length=10, choices=MEDIA_KIND, default='before', verbose_name=_("النوع"))
    image = models.ImageField(upload_to='repair_logs/%Y/%m/', verbose_name=_("الصورة"))
    caption = models.CharField(max_length=200, blank=True, verbose_name=_("تعليق"))
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("مرفق سجل إصلاح")
        verbose_name_plural = _("مرفقات سجلات الإصلاح")


class CustomerFeedback(models.Model):
    """Public UUID link sent to customer after invoice 'posted' for rating + signature."""
    sale_invoice = models.OneToOneField(
        'SaleInvoice', on_delete=models.CASCADE, related_name='feedback',
        verbose_name=_("الفاتورة"),
    )
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
        verbose_name=_("رمز الرابط العام"))

    rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        verbose_name=_("التقييم (1-5)"),
    )
    comment = models.TextField(blank=True, verbose_name=_("ملاحظات العميل"))

    received_in_good_condition = models.BooleanField(default=False,
        verbose_name=_("استلمت السيارة بحالة جيدة"))
    signature_image = models.ImageField(upload_to='signatures/%Y/%m/', null=True, blank=True,
        verbose_name=_("توقيع رقمي"))

    sent_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = _("تقييم وتوقيع عميل")
        verbose_name_plural = _("تقييمات وتوقيعات العملاء")

    @property
    def public_url(self) -> str:
        return f"/feedback/{self.public_token}/"

    def __str__(self):
        return f"Feedback INV#{self.sale_invoice_id} ({self.rating or '—'})"