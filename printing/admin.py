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
    PrintTreasury, PrintTransaction, ProductType, StaffPermission,
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

    # 🛡️ [FIX]: Block add/change/delete from public schema too
    def has_add_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_delete_permission(request, obj)


# =====================================================================
# 🏢 الفروع والعملاء
# =====================================================================

@admin.register(PrintBranch)
class PrintBranchAdmin(PrintSecureAdmin):
    list_display = ('name', 'phone', 'is_active')
    search_fields = ('name',)


@admin.register(PrintCustomer)
class PrintCustomerAdmin(PrintSecureAdmin):
    list_display = ('name', 'company', 'phone', 'whatsapp', 'orders_count', 'statement_link')
    search_fields = ('name', 'company', 'phone')
    list_filter = ('created_at',)

    def orders_count(self, obj):
        count = obj.printorder_set.count()
        return format_html('<b>{}</b>', count)
    orders_count.short_description = "عدد الطلبات"

    def statement_link(self, obj):
        return format_html(
            '<a href="/printing/customer/{}/statement/" target="_blank" '
            'style="background:linear-gradient(135deg,#ec4899,#8b5cf6); color:#fff; '
            'padding:5px 12px; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.82rem;">'
            '📒 كشف حساب</a>', obj.pk)
    statement_link.short_description = "كشف حساب"


# =====================================================================
# 🖨️ ماكينات الطباعة
# =====================================================================

@admin.register(MachineProfile)
class MachineProfileAdmin(PrintSecureAdmin):
    list_display = ('name', 'machine_type_badge', 'brand', 'branch', 'hourly_cost_display', 'status_badge')
    list_filter = ('machine_type', 'is_active', 'branch')
    search_fields = ('name', 'brand', 'model_number')
    list_select_related = ('branch',)
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
        return format_html('<span title="{}">{}</span>', f"{r:.1f}", stars)
    avg_rating.short_description = "التقييم"


@admin.register(DesignerWorkLog)
class DesignerWorkLogAdmin(PrintSecureAdmin):
    list_display = ('designer', 'title', 'execution_badge', 'duration_hours', 'rating_display', 'date')
    list_filter = ('execution_type', 'date', 'designer')
    search_fields = ('title', 'description')
    list_select_related = ('designer',)
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
    fields = ('product_type_text', 'description', 'machine', 'paper_size', 'quantity', 'copies', 'unit_price', 'total_price', 'design_file', 'is_complete')
    autocomplete_fields = ['product_type']


@admin.register(PrintOrder)
class PrintOrderAdmin(PrintSecureAdmin):
    list_display = ('order_number', 'customer', 'status_badge', 'total_display', 'paid_display', 'remaining_display', 'has_files_badge', 'date_created')
    list_filter = ('status', 'branch', 'date_created')
    search_fields = ('order_number', 'customer__name')
    list_select_related = ('customer', 'branch')
    date_hierarchy = 'date_created'
    inlines = [PrintJobInline]
    fieldsets = (
        ('📋 بيانات الطلب', {
            'fields': ('order_number', 'customer', 'branch', 'status', 'date_due'),
        }),
        ('💰 المالي', {
            'fields': ('total_amount', 'discount', 'paid_amount'),
        }),
        ('📁 ملفات المشروع', {
            'fields': ('project_file', 'project_file_2', 'project_file_3'),
            'description': 'ارفع ملفات المشروع الأصلية (PSD, AI, PDF, إلخ) — يتم حفظها بأمان على السيرفر',
        }),
        ('📝 ملاحظات', {
            'fields': ('notes',),
            'classes': ('collapse',),
        }),
    )

    def has_files_badge(self, obj):
        count = sum(1 for f in [obj.project_file, obj.project_file_2, obj.project_file_3] if f)
        if count:
            return format_html('<span style="background:#8b5cf6;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">📁 {}</span>', count)
        return '-'
    has_files_badge.short_description = "ملفات"

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
    list_display = ('description', 'product_type_badge', 'order', 'machine', 'quantity', 'total_price', 'cost_display', 'profit_display', 'is_complete')
    list_filter = ('is_complete', 'machine', 'product_type', 'paper_size')
    search_fields = ('description', 'product_type_text')
    list_select_related = ('order', 'machine', 'product_type')
    autocomplete_fields = ['product_type']

    def product_type_badge(self, obj):
        name = obj.product_type_text or (obj.product_type.name if obj.product_type else '-')
        if name == '-':
            return '-'
        return format_html('<span style="background:#6366f1;color:white;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:bold;">{}</span>', name)
    product_type_badge.short_description = "نوع البند"

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
    list_select_related = ('branch',)

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


# =====================================================================
# 🏷️ أنواع البنود
# =====================================================================

@admin.register(ProductType)
class ProductTypeAdmin(PrintSecureAdmin):
    list_display = ('name', 'usage_count_display', 'created_at')
    search_fields = ('name',)
    ordering = ('-usage_count',)

    def usage_count_display(self, obj):
        return format_html('<b style="color:#6366f1;">{}</b> مرة', obj.usage_count)
    usage_count_display.short_description = "عدد الاستخدام"


# =====================================================================
# 🔐 صلاحيات الموظفين
# =====================================================================

@admin.register(StaffPermission)
class StaffPermissionAdmin(PrintSecureAdmin):
    list_display = (
        'user', 'can_view_treasury', 'can_view_profits',
        'can_view_project_files', 'can_use_ai_studio',
        'can_manage_stock', 'can_view_reports',
    )
    list_filter = (
        'can_view_treasury', 'can_view_profits',
        'can_use_ai_studio', 'can_view_reports',
    )
    list_editable = (
        'can_view_treasury', 'can_view_profits',
        'can_view_project_files', 'can_use_ai_studio',
        'can_manage_stock', 'can_view_reports',
    )
    fieldsets = (
        ('👤 الموظف', {'fields': ('user',)}),
        ('💰 الصلاحيات المالية', {
            'fields': ('can_view_treasury', 'can_manage_treasury', 'can_view_profits'),
            'description': '⚠️ صلاحيات حساسة — فقط للمديرين والمحاسبين',
        }),
        ('📋 الطلبات', {
            'fields': ('can_create_orders', 'can_edit_orders', 'can_delete_orders', 'can_view_all_orders'),
        }),
        ('📁 الملفات والعملاء', {
            'fields': ('can_view_project_files', 'can_upload_project_files', 'can_manage_customers'),
        }),
        ('📦 المخزون والمصممين', {
            'fields': ('can_manage_stock', 'can_view_designers'),
        }),
        ('🤖 AI وتقارير', {
            'fields': ('can_use_ai_studio', 'can_view_reports'),
        }),
    )
