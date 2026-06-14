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

# Sale + purchase invoices and their line/service/inspection items.

from .organization import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403
from .customers import *  # noqa: F401, F403
from .finance import *  # noqa: F401, F403

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
        from decimal import ROUND_HALF_UP
        tax_amount = ((subtotal * Decimal(str(self.tax_percentage or 0))) / Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        self.total_amount = subtotal + tax_amount
        self.total_cost = items_total_cost
        self.total_core_charge = calculated_core_charge
        # Net profit: revenue minus cost. Tax is excluded from both sides
        # (collected from customer, passed to gov — never counted as income or expense).
        gross_margin = (items_total_price - items_total_cost) + services_total_price + Decimal(str(self.labor_cost_manual or 0)) - Decimal(str(self.discount or 0))
        self.net_profit = gross_margin

        # 🛡️ Auto-fill paid_amount ONLY when a treasury is set — otherwise we
        # mark the invoice as paid without any ledger entry, creating phantom
        # revenue. Without a treasury the invoice stays as receivable.
        if (self.paid_amount == Decimal('0.00')
                and self.status == 'posted'
                and self.treasury_id
                and not self.maintenance_contract):
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

    # 🧮 Accountant Review — explicit billing decision
    # When False, the line stays on the Job Card for audit/diagnosis evidence
    # but is excluded from the customer-facing invoice totals.
    is_billable = models.BooleanField(
        default=True, verbose_name=_("يُدرج في فاتورة العميل"),
        help_text=_("افصلها لو الجزء استُخدم للفحص فقط أو كان ضماناً."),
    )
    billing_note = models.CharField(
        max_length=200, blank=True,
        verbose_name=_("ملاحظة المحاسب"),
        help_text=_("اختياري — تظهر للمراجعة الداخلية فقط."),
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

            # 🛠️ B2B auto-pricing — only when cashier did NOT supply a unit_price.
            # Previously this silently overwrote the user-typed price for B2B
            # customers, and crashed when both wholesale & retail prices were
            # null (NoneType > 0 TypeError → 500 → "ارجع للدعم").
            needs_autoprice = self.unit_price is None or Decimal(str(self.unit_price)) <= 0
            if needs_autoprice and self.invoice.customer and getattr(self.invoice.customer, 'is_b2b_company', False):
                wholesale = self.product.b2b_wholesale_price or Decimal('0.00')
                retail = self.product.retail_price or Decimal('0.00')
                self.unit_price = wholesale if wholesale > 0 else retail

            # Final safety net — DB requires NOT NULL on unit_price
            if self.unit_price is None:
                self.unit_price = Decimal('0.00')

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

    # 🧮 Accountant Review — explicit billing decision (matches SaleInvoiceItem)
    is_billable = models.BooleanField(
        default=True, verbose_name=_("يُدرج في فاتورة العميل"),
    )
    billing_note = models.CharField(
        max_length=200, blank=True,
        verbose_name=_("ملاحظة المحاسب"),
    )

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

