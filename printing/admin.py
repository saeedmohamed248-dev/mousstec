"""
🎨 Mousstec Printing Admin — لوحة تحكم المطابع والتصميم
"""
from django.contrib import admin
from django.utils.html import format_html
from django.db.models import Sum, Count, Avg
from django.utils import timezone
from django.db import connection
from .models import (
    PrintBranch, PrintCustomer, MachineProfile, Designer,
    DesignerWorkLog, PrintOrder, PrintJob, PrintMaterial,
    PrintTreasury, PrintTransaction,
)


class PrintSecureAdmin(admin.ModelAdmin):
    """حماية: حظر الوصول من الـ public schema لجداول الطباعة"""
    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_view_permission(request, obj)


# =====================================================================
# 🏢 الفروع والعملاء
# =====================================================================

@admin.register(PrintBranch)
class PrintBranchAdmin(PrintSecureAdmin):
    list_display = ('name', 'phone', 'is_active')
    search_fields = ('name',)


@admin.register(PrintCustomer)
class PrintCustomerAdmin(PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'whatsapp', 'orders_count')
    search_fields = ('name', 'company', 'phone')
    list_filter = ('created_at',)

    def orders_count(self, obj):
        count = obj.printorder_set.count()
        return format_html('<b>{}</b>', count)
    orders_count.short_description = "عدد الطلبات"


# =====================================================================
# 🖨️ ماكينات الطباعة
# =====================================================================

@admin.register(MachineProfile)
class MachineProfileAdmin(PrintSecureAdmin):
    list_display = ('name', 'machine_type_badge', 'brand', 'branch', 'hourly_cost_display', 'status_badge')
    list_filter = ('machine_type', 'is_active', 'branch')
    search_fields = ('name', 'brand', 'model_number')
    fieldsets = (
        ('📋 بيانات الماكينة', {
            'fields': ('name', 'machine_type', 'brand', 'model_number', 'branch', 'is_active')
        }),
        ('⚡ تكاليف التشغيل', {
            'fields': ('power_consumption_kwh', 'electricity_rate_per_kwh', 'hourly_labor_cost'),
            'description': 'أدخل بيانات الاستهلاك لحساب تكلفة التشغيل تلقائياً'
        }),
        ('🎨 تكلفة الأحبار (CMYK)', {
            'fields': ('ink_cyan_cost_per_ml', 'ink_magenta_cost_per_ml', 'ink_yellow_cost_per_ml', 'ink_black_cost_per_ml'),
            'classes': ('collapse',)
        }),
        ('📊 الصيانة والإحصائيات', {
            'fields': ('total_print_hours', 'maintenance_due_date', 'notes'),
            'classes': ('collapse',)
        }),
    )

    def machine_type_badge(self, obj):
        colors = {
            'digital': '#3b82f6', 'offset': '#8b5cf6', 'large_format': '#f59e0b',
            'dtf': '#ec4899', 'uv': '#06b6d4', 'sublimation': '#ef4444',
            'cutter': '#10b981', 'laminator': '#6366f1', 'other': '#64748b',
        }
        c = colors.get(obj.machine_type, '#64748b')
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;">{}</span>', c, obj.get_machine_type_display())
    machine_type_badge.short_description = "النوع"

    def hourly_cost_display(self, obj):
        cost = obj.hourly_operating_cost
        return format_html('<b style="color:#f59e0b;">{} ج.م/ساعة</b>', f"{float(cost):,.2f}")
    hourly_cost_display.short_description = "تكلفة التشغيل"

    def status_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#10b981;font-weight:bold;">🟢 تعمل</span>')
        return format_html('<span style="color:#ef4444;font-weight:bold;">🔴 متوقفة</span>')
    status_badge.short_description = "الحالة"


# =====================================================================
# 🎨 المصممين وسجل الأعمال
# =====================================================================

class DesignerWorkLogInline(admin.TabularInline):
    model = DesignerWorkLog
    extra = 1
    fields = ('date', 'title', 'execution_type', 'duration_hours', 'client_rating', 'customer')


@admin.register(Designer)
class DesignerAdmin(PrintSecureAdmin):
    list_display = ('__str__', 'specialization', 'branch', 'month_works', 'month_hours', 'avg_rating', 'is_active')
    list_filter = ('is_active', 'branch')
    inlines = [DesignerWorkLogInline]

    def month_works(self, obj):
        stats = obj.get_month_stats()
        return stats.get('total_works') or 0
    month_works.short_description = "أعمال الشهر"

    def month_hours(self, obj):
        stats = obj.get_month_stats()
        h = stats.get('total_hours') or 0
        return format_html('<b>{}</b> ساعة', f"{float(h):.1f}")
    month_hours.short_description = "ساعات الشهر"

    def avg_rating(self, obj):
        stats = obj.get_month_stats()
        r = stats.get('avg_rating')
        if not r:
            return '-'
        stars = '⭐' * int(round(r))
        return format_html('<span title="{:.1f}">{}</span>', r, stars)
    avg_rating.short_description = "التقييم"


@admin.register(DesignerWorkLog)
class DesignerWorkLogAdmin(PrintSecureAdmin):
    list_display = ('designer', 'title', 'execution_badge', 'duration_hours', 'rating_display', 'date')
    list_filter = ('execution_type', 'date', 'designer')
    search_fields = ('title', 'description')
    date_hierarchy = 'date'

    def execution_badge(self, obj):
        colors = {'manual': '#3b82f6', 'ai_generated': '#8b5cf6', 'ai_assisted': '#06b6d4'}
        c = colors.get(obj.execution_type, '#64748b')
        return format_html('<span style="background:{};color:white;padding:3px 8px;border-radius:8px;font-size:11px;font-weight:700;">{}</span>', c, obj.get_execution_type_display())
    execution_badge.short_description = "نوع التنفيذ"

    def rating_display(self, obj):
        if obj.client_rating:
            return '⭐' * obj.client_rating
        return '-'
    rating_display.short_description = "التقييم"


# =====================================================================
# 📋 طلبات ومهام الطباعة
# =====================================================================

class PrintJobInline(admin.TabularInline):
    model = PrintJob
    extra = 1
    fields = ('description', 'machine', 'paper_size', 'quantity', 'copies', 'unit_price', 'total_price', 'is_complete')


@admin.register(PrintOrder)
class PrintOrderAdmin(PrintSecureAdmin):
    list_display = ('order_number', 'customer', 'status_badge', 'total_display', 'paid_display', 'remaining_display', 'date_created')
    list_filter = ('status', 'branch', 'date_created')
    search_fields = ('order_number', 'customer__name')
    date_hierarchy = 'date_created'
    inlines = [PrintJobInline]

    def status_badge(self, obj):
        colors = {
            'draft': '#94a3b8', 'confirmed': '#3b82f6', 'in_progress': '#f59e0b',
            'ready': '#8b5cf6', 'delivered': '#10b981', 'cancelled': '#ef4444',
        }
        c = colors.get(obj.status, '#64748b')
        return format_html('<span style="background:{};color:white;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;">{}</span>', c, obj.get_status_display())
    status_badge.short_description = "الحالة"

    def total_display(self, obj):
        return format_html('<b>{}</b> ج.م', f"{float(obj.net_total):,.2f}")
    total_display.short_description = "الإجمالي"

    def paid_display(self, obj):
        return format_html('<span style="color:#10b981;font-weight:bold;">{}</span> ج.م', f"{float(obj.paid_amount):,.2f}")
    paid_display.short_description = "المدفوع"

    def remaining_display(self, obj):
        r = obj.remaining
        color = '#ef4444' if r > 0 else '#10b981'
        return format_html('<span style="color:{};font-weight:bold;">{}</span> ج.م', color, f"{float(r):,.2f}")
    remaining_display.short_description = "المتبقي"


@admin.register(PrintJob)
class PrintJobAdmin(PrintSecureAdmin):
    list_display = ('description', 'order', 'machine', 'quantity', 'total_price', 'cost_display', 'profit_display', 'is_complete')
    list_filter = ('is_complete', 'machine', 'paper_size')

    def cost_display(self, obj):
        cost = obj.calculated_cost
        return format_html('<span style="color:#f59e0b;">{}</span> ج.م', f"{float(cost):,.2f}")
    cost_display.short_description = "التكلفة الفعلية"

    def profit_display(self, obj):
        p = obj.profit
        color = '#10b981' if p >= 0 else '#ef4444'
        return format_html('<span style="color:{};font-weight:bold;">{}</span> ج.م', color, f"{float(p):,.2f}")
    profit_display.short_description = "الربح"


# =====================================================================
# 📦 خامات الطباعة
# =====================================================================

@admin.register(PrintMaterial)
class PrintMaterialAdmin(PrintSecureAdmin):
    list_display = ('name', 'category', 'quantity_display', 'cost_per_unit', 'stock_value_display', 'stock_alert')
    list_filter = ('category', 'branch')
    search_fields = ('name', 'sku')

    def quantity_display(self, obj):
        return format_html('<b>{}</b> {}', f"{float(obj.quantity):,.1f}", obj.unit)
    quantity_display.short_description = "الكمية"

    def stock_value_display(self, obj):
        return format_html('<b>{}</b> ج.م', f"{float(obj.stock_value):,.2f}")
    stock_value_display.short_description = "القيمة"

    def stock_alert(self, obj):
        if obj.is_low_stock:
            return format_html('<span style="background:#ef4444;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">⚠️ منخفض</span>')
        return format_html('<span style="color:#10b981;">✅ متوفر</span>')
    stock_alert.short_description = "المخزون"


# =====================================================================
# 💰 الخزينة
# =====================================================================

class PrintTransactionInline(admin.TabularInline):
    model = PrintTransaction
    extra = 1
    fields = ('transaction_type', 'amount', 'description', 'date')


@admin.register(PrintTreasury)
class PrintTreasuryAdmin(PrintSecureAdmin):
    list_display = ('name', 'branch', 'balance_display', 'is_active')
    inlines = [PrintTransactionInline]

    def balance_display(self, obj):
        color = '#10b981' if obj.balance >= 0 else '#ef4444'
        return format_html('<b style="color:{};">{} ج.م</b>', color, f"{float(obj.balance):,.2f}")
    balance_display.short_description = "الرصيد"


@admin.register(PrintTransaction)
class PrintTransactionAdmin(PrintSecureAdmin):
    list_display = ('type_badge', 'amount_display', 'treasury', 'description', 'date')
    list_filter = ('transaction_type', 'treasury', 'date')

    def type_badge(self, obj):
        if obj.transaction_type == 'in':
            return format_html('<span style="color:#10b981;font-weight:bold;">🟢 إيداع</span>')
        return format_html('<span style="color:#ef4444;font-weight:bold;">🔴 مصروف</span>')
    type_badge.short_description = "النوع"

    def amount_display(self, obj):
        color = '#10b981' if obj.transaction_type == 'in' else '#ef4444'
        return format_html('<b style="color:{};">{} ج.م</b>', color, f"{float(obj.amount):,.2f}")
    amount_display.short_description = "المبلغ"
