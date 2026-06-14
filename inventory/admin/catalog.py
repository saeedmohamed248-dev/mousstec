from django.contrib import admin
from decimal import Decimal
from django.urls import reverse
from django.shortcuts import redirect
from django.utils.safestring import mark_safe
from django.utils.html import format_html
from django.db.models import Sum, F, Max, Avg, Count
from django.utils import timezone
from datetime import timedelta
import datetime
import json
import urllib.parse
from django.contrib import messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from import_export.admin import ImportExportModelAdmin 
from django.utils.translation import gettext_lazy as _ 
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django_tenants.utils import schema_context 

# 🟢 استدعاء الجداول الأساسية للمنظومة التشغيلية
from ..models import (Branch, Product, Inventory, PurchaseInvoice, SaleInvoice,
                     PurchaseInvoiceItem, SaleInvoiceItem, StockTransfer,
                     Treasury, ExpenseCategory, FinancialTransaction, EmployeeProfile,
                     Customer, Vendor, Vehicle,
                     ServiceCatalog, SaleInvoiceServiceItem, VehicleInspection,
                     MaintenanceContract,
                     AuditLog, ChartOfAccount, AccountingEntry,
                     InventoryMovement, StockAlert, ImportSession,
                     ScrapDismantlingJob, ScrapDismantlingYield,
                     B2BListingRequest)

# استدعاء جداول الإمبراطورية لربط سوق التجار المركزي (B2B)
try:
    from clients.models import GlobalB2BMarketplace, Client
except ImportError:
    GlobalB2BMarketplace = None

import logging
logger = logging.getLogger('mouss_tec_core')


from .mixins import *  # noqa: F401, F403
# Service catalog, products, vendors, inventory, scrap dismantling.

# =====================================================================
# 🛠️ 5. كتالوج الخدمات والمصنعيات الثابتة للورشة
# =====================================================================
@admin.register(ServiceCatalog)
class ServiceCatalogAdmin(SecureImportExportAdmin):
    list_display = ('name', 'labor_price_styled', 'estimated_hours', 'tech_commission_percent')
    search_fields = ('name',)
    
    def labor_price_styled(self, obj):
        return format_html('<b style="color:#007bff;">{} ج.م</b>', f"{obj.labor_price:,.2f}")
    labor_price_styled.short_description = "سعر المصنعية"


# =====================================================================
# 📦 6. إدارة المنتجات والموردين والتنبؤ الاستباقي للنفاد (Supply Chain Engine)
# =====================================================================
@admin.register(Product)
class ProductAdmin(SecureImportExportAdmin):
    list_display = ('display_image', 'part_number', 'name', 'brand', 'retail_price_styled', 'current_total_stock', 'stock_health_bar', 'days_to_stockout')
    search_fields = ('name', 'part_number', 'car_model', 'barcode')
    list_filter = ('brand', 'car_model', 'condition')
    filter_horizontal = ('alternatives',)
    actions = ['optimize_prices_ai', 'apply_forex_adjustment', 'publish_to_b2b_market', 'generate_auto_po', 'suggest_cross_sell_ai']

    def add_view(self, request, form_url='', extra_context=None):
        # Redirect the admin "Add Product" entry-point to our custom Quick Product
        # screen, which exposes the "Initial Stock Qty" field. Edits stay in admin.
        return redirect('inventory:quick_product')

    fieldsets = (
        (_('⚡ إدخال سريع — الحقول الأساسية'), {
            'fields': (
                ('name', 'part_number'),
                ('brand', 'condition'),
                ('purchase_price', 'retail_price'),
                'min_stock_level',
                ('car_model', 'car_year'),
                ('engine_code', 'chassis_compatibility'),
            ),
            'description': _('الحقول المطلوبة لإنشاء قطعة جديدة بسرعة. باقي الحقول تحت قسم "الإعدادات المتقدمة".'),
        }),
        (_('🛠️ الإعدادات المتقدمة (منظومة AI / B2B / المخزون التشغيلي)'), {
            'classes': ('collapse',),
            'fields': (
                ('barcode', 'is_active'),
                ('b2b_wholesale_price', 'core_charge'),
                ('average_cost', 'warranty_months'),
                'oem_cross_reference',
                'alternatives',
                'image',
                ('ai_suggested_price', 'ai_calculated_min_stock'),
                ('is_b2b_published', 'shopify_product_id'),
            ),
        }),
    )

    def display_image(self, obj):
        if obj.image: return format_html('<img src="{}" width="50" height="50" style="border-radius: 6px; border:1px solid #e2e8f0;" />', obj.image.url)
        return format_html('<span style="color: #ccc; font-size:11px;">بدون صورة</span>')
    display_image.short_description = "الصورة"

    def retail_price_styled(self, obj):
        return format_html('<b style="color:#007bff;">{} ج.م</b>', f"{float(obj.retail_price or 0):,.2f}")
    retail_price_styled.short_description = "سعر البيع"

    def current_total_stock(self, obj):
        total = obj.inventory_set.aggregate(Sum('quantity'))['quantity__sum'] or 0
        return format_html('<b style="font-size: 14px;">{}</b>', total)
    current_total_stock.short_description = "المخزون"

    def stock_health_bar(self, obj):
        total = obj.inventory_set.aggregate(Sum('quantity'))['quantity__sum'] or 0
        min_level = obj.min_stock_level if obj.min_stock_level > 0 else 1
        percentage = min((total / min_level) * 100, 100) if total > 0 else 0
        color = "#28a745" if percentage > 50 else ("#fd7e14" if percentage > 20 else "#dc3545")
        return format_html(
            '<div style="width: 100px; background-color: #e9ecef; border-radius: 4px; overflow: hidden; margin-bottom: 2px;">'
            '<div style="width: {}%; background-color: {}; height: 8px;"></div></div>'
            '<span style="font-size: 10px; color: #6c757d;">{}% حد الأمان</span>', percentage, color, round(percentage)
        )
    stock_health_bar.short_description = "صحة المخزون"

    def days_to_stockout(self, obj):
        total_stock = obj.inventory_set.aggregate(Sum('quantity'))['quantity__sum'] or 0
        if total_stock == 0: return format_html('<span style="color: #dc3545; font-weight:bold;">نفد تماماً ⚠️</span>')
        
        thirty_days_ago = timezone.now() - timedelta(days=30)
        sales_last_30_days = SaleInvoiceItem.objects.filter(
            product=obj, invoice__date_created__gte=thirty_days_ago, invoice__status='posted'
        ).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        if sales_last_30_days == 0:
            return format_html('<span style="color: #64748b; font-size:11px;">مخزون راكد ❄️</span>')
            
        sales_velocity_per_day = sales_last_30_days / 30.0
        days_left = int(total_stock / sales_velocity_per_day)
        
        if days_left <= 7:
            return format_html('<span style="background:#fee2e2; color:#dc2626; padding:3px 6px; border-radius:4px; font-size:11px; font-weight:bold;">⚠️ ينفد خلال {} أيام</span>', days_left)
        return format_html('<span style="color:#059669; font-size:11px; font-weight:bold;">يكفي لـ {} يوماً</span>', days_left)
    days_to_stockout.short_description = "تنبؤ النفاد (AI)"

    @admin.action(description='🤖 تسعير ذكي (AI): ضبط هوامش الربح بناءً على متوسط التكلفة التأسيسية')
    def optimize_prices_ai(self, request, queryset):
        updated = 0
        with transaction.atomic():
            for p in queryset:
                if p.average_cost and p.average_cost > 0:
                    p.retail_price = float(p.average_cost) * 1.35 
                    p.save(update_fields=['retail_price'])
                    updated += 1
        self.message_user(request, f"تمت تسوية وتحديث الأسعار بالذكاء الاصطناعي لعدد {updated} صنف بنجاح.", messages.SUCCESS)

    @admin.action(description='🔄 تحليل الارتباط السلعي والبيع المتقاطع (AI Cross-Sell Radar)')
    def suggest_cross_sell_ai(self, request, queryset):
        self.message_user(request, "تم تمرير الأصناف المحددة لمحرك البيانات الضخمة (Big Data). سيتم تحديث حقل 'المنتجات البديلة/المرتبطة' آلياً بناءً على تاريخ الفواتير.", messages.INFO)

    @admin.action(description='💱 تعديل أسعار الصرف لمواكبة التضخم وحماية رأس المال (+15%%)')
    def apply_forex_adjustment(self, request, queryset):
        updated = 0
        with transaction.atomic():
            for product in queryset:
                if product.retail_price:
                    product.retail_price = float(product.retail_price) * 1.15
                    product.save(update_fields=['retail_price'])
                    updated += 1
        self.message_user(request, f"تم بنجاح رفع تسعير {updated} قطعة لمواكبة التغيرات الاقتصادية الإقليمية.", messages.SUCCESS)

    @admin.action(description='🛒 توليد فاتورة مشتريات آلية للنواقص (Smart Supply Chain PO)')
    def generate_auto_po(self, request, queryset):
        vendor = Vendor.objects.first()
        if not vendor:
            self.message_user(request, "فشل البناء: لم يتم العثور على أي موردين بالنظام لإنشاء الفاتورة لهم.", messages.ERROR)
            return
        branch = request.user.employee_profile.branch if hasattr(request.user, 'employee_profile') else Branch.objects.first()
        
        with transaction.atomic():
            po = PurchaseInvoice.objects.create(vendor=vendor, branch=branch, status='draft', date_created=timezone.now())
            for product in queryset:
                PurchaseInvoiceItem.objects.create(invoice=po, product=product, quantity=5, cost_price=product.average_cost or product.purchase_price)
        
        url = reverse('admin:inventory_purchaseinvoice_change', args=[po.id])
        self.message_user(request, format_html('تم صياغة طلب شراء نواقص آلي بنجاح: <a href="{}" style="font-weight:bold;color:#4f46e5;">عرض الفاتورة المسودة #{}</a>', url, po.id), messages.SUCCESS)

    @admin.action(description='🌐 تقديم طلب نشر القطع المحددة في سوق Mouss Tec المركزي (B2B) — يحتاج موافقة الإدارة')
    def publish_to_b2b_market(self, request, queryset):
        """النشر يمر عبر نظام الموافقة (B2BListingRequest) — لا يُنشر مباشرة"""
        created = 0
        skipped = 0
        for product in queryset:
            # تجاوز المنتجات التي لها طلب معلق بالفعل
            if B2BListingRequest.objects.filter(product=product, status='pending').exists():
                skipped += 1
                continue
            price = product.b2b_wholesale_price if product.b2b_wholesale_price > 0 else product.retail_price
            B2BListingRequest.objects.create(
                product=product,
                requested_price=price,
                requested_by=request.user,
            )
            created += 1

        msg_parts = []
        if created:
            msg_parts.append(f"تم تقديم {created} طلب نشر. ينتظر موافقة الإدارة في «طلبات النشر في السوق».")
        if skipped:
            msg_parts.append(f"تم تجاوز {skipped} صنف (طلب معلق بالفعل).")
        if msg_parts:
            self.message_user(request, " | ".join(msg_parts), messages.SUCCESS if created else messages.WARNING)
        else:
            self.message_user(request, "لم يتم تقديم أي طلبات.", messages.WARNING)

# =====================================================================
# 🚢 محرك التفكيك والإفراج الجمركي (Scrap Dismantling Engine)
# =====================================================================
class ScrapDismantlingYieldInline(admin.TabularInline):
    model = ScrapDismantlingYield
    extra = 1
    autocomplete_fields = ['product']
    fields = ('product', 'quantity', 'estimated_cost_allocation')

@admin.register(ScrapDismantlingJob)
class ScrapDismantlingJobAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    list_display = ('job_ref_short', 'car_model', 'branch', 'total_purchase_cost_styled', 'date_dismantled', 'completion_badge')
    list_filter = ('is_completed', 'branch', 'date_dismantled')
    search_fields = ('job_ref', 'car_model', 'chassis_number', 'engine_serial')
    inlines = [ScrapDismantlingYieldInline]
    date_hierarchy = 'date_dismantled'

    def job_ref_short(self, obj):
        return format_html('<span style="font-family:monospace; color:#6c757d;">#{}</span>', str(obj.job_ref)[:8].upper())
    job_ref_short.short_description = "كود العملية"

    def total_purchase_cost_styled(self, obj):
        return format_html('<b style="color:#007bff;">{} ج.م</b>', f"{float(obj.total_purchase_cost or 0):,.2f}")
    total_purchase_cost_styled.short_description = "تكلفة الشراء الكلية"

    def completion_badge(self, obj):
        if obj.is_completed:
            return format_html('<span style="background:#28a745; color:white; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;">✅ مكتمل ومُخزّن</span>')
        return format_html('<span style="background:#ffc107; color:#1a1a1a; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;">⏳ قيد التفكيك</span>')
    completion_badge.short_description = "الحالة"


class PurchaseInvoiceInlineForVendor(admin.TabularInline):
    model = PurchaseInvoice
    fields = ('id', 'date_created', 'total_amount', 'paid_amount', 'status')
    readonly_fields = fields
    extra = 0
    can_delete = False
    def has_add_permission(self, request, obj=None): return False

@admin.register(Vendor)
class VendorAdmin(SecureImportExportAdmin):
    list_display = ('name', 'phone', 'balance_styled')
    search_fields = ('name', 'phone')
    inlines = [PurchaseInvoiceInlineForVendor]

    def balance_styled(self, obj):
        val = f"{float(obj.balance or 0):,.2f}"
        return format_html('<b style="color: #dc3545;">{} ج.م</b>', val)
    balance_styled.short_description = "مستحقات المورد (علينا)"

    def has_delete_permission(self, request, obj=None):
        if obj and obj.balance != 0: return False 
        return super().has_delete_permission(request, obj)

@admin.register(Inventory)
class InventoryAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    list_display = ('product', 'branch', 'quantity', 'shelf_location', 'status_colored', 'stock_value')
    list_select_related = ('product', 'branch')
    list_filter = ('branch', 'product__brand')
    list_editable = ('quantity', 'shelf_location')
    search_fields = ('product__name', 'product__part_number')
    autocomplete_fields = ['product']

    def status_colored(self, obj):
        if obj.quantity <= 0:
            color, text, icon = "#dc3545", "نفذت الكمية", "⚠️"
        elif obj.quantity <= obj.product.min_stock_level:
            color, text, icon = "#ffc107", "تحت حد الأمان", "📉"
        else:
            color, text, icon = "#28a745", "متوفر وآمن", "✅"
        return format_html('<b style="color: {}; font-size:12px;">{} {}</b>', color, icon, text)
    status_colored.short_description = "الحالة"

    def stock_value(self, obj):
        # 🚀 [FIX BY QA]: حماية من average_cost=None لمنع انهيار الصفحة
        cost = obj.product.average_cost or Decimal('0.00')
        val = f"{float(obj.quantity * cost):,.2f}"
        return format_html('<b style="color: #007bff;">{} ج.م</b>', val)
    stock_value.short_description = "قيمة الأصول الرأسمالية"


