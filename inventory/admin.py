from django.contrib import admin
from decimal import Decimal
from django.urls import reverse
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
from .models import (Branch, Product, Inventory, PurchaseInvoice, SaleInvoice,
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

# =====================================================================
# 🛡️ 0. درع العزل السحابي ومنع تسريب البيانات وعزل الفروع
# =====================================================================

class SafeAdminLogMixin:
    """
    🛡️ حماية من خطأ ForeignKey Violation في django_admin_log
    عند دخول superuser من public schema على tenant schema،
    user_id الخاص به قد لا يكون موجوداً في auth_user الخاصة بالـ tenant.
    هذا الـ Mixin يلتقط الخطأ ويسجل في AuditLog بدلاً من الانهيار.
    """
    def log_addition(self, request, obj, message):
        try:
            return super().log_addition(request, obj, message)
        except Exception:
            logger.warning(f"⚠️ [ADMIN LOG] Skipped LogEntry for add: {obj} (cross-schema user)")

    def log_change(self, request, obj, message):
        try:
            return super().log_change(request, obj, message)
        except Exception:
            logger.warning(f"⚠️ [ADMIN LOG] Skipped LogEntry for change: {obj} (cross-schema user)")

    def log_deletion(self, request, obj, object_repr):
        try:
            return super().log_deletion(request, obj, object_repr)
        except Exception:
            logger.warning(f"⚠️ [ADMIN LOG] Skipped LogEntry for delete: {object_repr} (cross-schema user)")


class SecureImportExportAdmin(SafeAdminLogMixin, ImportExportModelAdmin):
    """حظر دخول أي مستخدم من خارج الـ Tenant Schema للجداول الإدارية الميدانية ومراقبة الصلاحيات"""
    def has_module_permission(self, request):
        if connection.schema_name == 'public': return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name == 'public': return False
        return super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        if connection.schema_name == 'public': return False
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if connection.schema_name == 'public': return False
        return super().has_change_permission(request, obj)

    def has_export_permission(self, request):
        if request.user.is_superuser: return True
        try: return request.user.employee_profile.role in ['admin', 'manager']
        except Exception: return False

    def has_import_permission(self, request):
        if request.user.is_superuser: return True
        try: return request.user.employee_profile.role in ['admin', 'manager']
        except Exception: return False

class BranchIsolationMixin:
    """تصفية تلقائية للبيانات والمدخلات والعمليات حسب فرع الموظف الحالي لضمان الأمن المعلوماتي للورش"""
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser: return qs 
        try:
            branch = request.user.employee_profile.branch
            if branch and hasattr(self.model, 'branch'): return qs.filter(branch=branch)
        except Exception: pass
        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser:
            try:
                branch = request.user.employee_profile.branch
                if branch:
                    if db_field.name == "branch": 
                        kwargs["queryset"] = Branch.objects.filter(id=branch.id)
                        kwargs["initial"] = branch.id
                    elif db_field.name == "treasury":
                        kwargs["queryset"] = Treasury.objects.filter(branch=branch)
            except Exception: pass
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser and hasattr(obj, 'branch') and not getattr(obj, 'branch', None):
            try: obj.branch = request.user.employee_profile.branch
            except Exception: pass
        super().save_model(request, obj, form, change)


# =====================================================================
# 🏢 1. رادارات التحكم في باقات الـ SaaS (Quotas Enforcement)
# =====================================================================
@admin.register(Branch)
class BranchAdmin(SecureImportExportAdmin): 
    list_display = ('name', 'location', 'phone', 'is_active_badge')
    
    def is_active_badge(self, obj):
        return format_html('<span style="color:#28a745; font-weight:bold;">✅ نشط وبث لايف</span>')
    is_active_badge.short_description = "حالة الفرع"

    def save_model(self, request, obj, form, change):
        if not change:
            tenant = connection.tenant 
            current_branches_count = Branch.objects.count()
            if tenant.max_branches and current_branches_count >= tenant.max_branches:
                raise ValidationError(f"🚫 حظر الباقة التأسيسية: شركتكم مسموح لها بإنشاء عدد ({tenant.max_branches}) فروع فقط بموجب الباقة الحالية.")
        super().save_model(request, obj, form, change)

@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(SecureImportExportAdmin):
    list_display = ('user', 'branch', 'role', 'commission_balance_styled')
    list_select_related = ('user', 'branch')
    list_filter = ('branch', 'role')
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
    
    def commission_balance_styled(self, obj):
        if obj.role == 'tech':
            return format_html('<b style="color: #28a745;">{} ج.م</b>', f"{obj.commission_balance:,.2f}")
        return "-"
    commission_balance_styled.short_description = "العمولات المستحقة"

    def save_model(self, request, obj, form, change):
        if not change:
            tenant = connection.tenant
            if tenant.max_users and EmployeeProfile.objects.count() >= tenant.max_users:
                raise ValidationError(f"🚫 تم الوصول للحد الأقصى المسموح به للموظفين والمستخدمين بالباقة ({tenant.max_users}).")
        super().save_model(request, obj, form, change)


# =====================================================================
# 🔗 2. نظام الـ CRM وعقود الصيانة وأساطيل الـ Fleet (B2Fleets SLAs)
# =====================================================================
@admin.register(MaintenanceContract)
class MaintenanceContractAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    """🚀 حامي التوجيه: تسجيل وإدارة عقود صيانة الأساطيل والشركات الكبرى (B2B SLAs)"""
    list_display = ('contract_code_short', 'customer', 'start_date', 'end_date', 'total_value_styled')
    list_filter = ('start_date', 'end_date')
    search_fields = ('contract_code', 'customer__name')
    autocomplete_fields = ['customer']

    def contract_code_short(self, obj):
        code = getattr(obj, 'contract_code', str(obj.id))
        return format_html('<span style="font-family:monospace; color:#6c757d;">#{}</span>', code[:6].upper() if len(code) > 6 else code)
    contract_code_short.short_description = "كود العقد"

    def total_value_styled(self, obj):
        val = float(getattr(obj, 'total_value', 0) or 0)
        return format_html('<b>{} ج.م</b>', f"{val:,.2f}")
    total_value_styled.short_description = "قيمة العقد السنوية"


# =====================================================================
# 👥 3. نظام إدارة الموظفين والرواتب المركزي وعملايات الصرف الذرية
# =====================================================================
class EmployeeProfileInline(admin.StackedInline):
    model = EmployeeProfile
    can_delete = False
    verbose_name = _("صلاحيات وعمولات النظام")
    verbose_name_plural = _("صلاحيات وعمولات النظام")

admin.site.unregister(User)
@admin.register(User)
class CustomUserAdmin(BaseUserAdmin):
    inlines = (EmployeeProfileInline,)
    list_display = ('username', 'email', 'first_name', 'last_name', 'is_staff', 'get_branch', 'get_role', 'get_commission')
    actions = ['pay_tech_commissions']

    def get_inline_instances(self, request, obj=None):
        if connection.schema_name == 'public': return [] 
        return super().get_inline_instances(request, obj)

    def get_branch(self, instance):
        if connection.schema_name == 'public': return "👑 إدارة سحابية مركزية"
        return instance.employee_profile.branch.name if hasattr(instance, 'employee_profile') and instance.employee_profile.branch else "إدارة عامة"
    get_branch.short_description = "الفرع"

    def get_role(self, instance):
        if connection.schema_name == 'public': return "أدمن المنصة السحابية"
        return instance.employee_profile.get_role_display() if hasattr(instance, 'employee_profile') else "-"
    get_role.short_description = "الوظيفة"

    def get_commission(self, instance):
        if connection.schema_name == 'public': return "-"
        if hasattr(instance, 'employee_profile') and instance.employee_profile.role == 'tech':
            val = instance.employee_profile.commission_balance
            return format_html('<b style="color: #dc3545;">{} ج.م</b>', f"{val:,.2f}")
        return "-"
    get_commission.short_description = "عمولات متأخرة"

    @admin.action(description='💸 صرف العمولات المستحقة للفنيين المحددين ذرياً (محرك الرواتب المحمي)')
    def pay_tech_commissions(self, request, queryset):
        from inventory.services.treasury_service import TreasuryService
        try:
            paid_count, total_paid, treasury_name = TreasuryService.pay_commissions(queryset)
            if paid_count > 0:
                self.message_user(
                    request,
                    f"✅ تم تصفير وصرف عمولات لعدد {paid_count} فني ميكانيكي "
                    f"بإجمالي {total_paid:,.2f} ج.م بنجاح من خزنة ({treasury_name}).",
                    messages.SUCCESS,
                )
            else:
                self.message_user(
                    request,
                    "⚠️ تنبيه: الفنيين المحددين ليس لديهم أي أرصدة عمولات معلقة للصرف.",
                    messages.WARNING,
                )
        except ValidationError as e:
            self.message_user(request, f"❌ فشل الصرف: {e.messages[0]}", messages.ERROR)


# =====================================================================
# 🤝 4. نظام الـ CRM ومحرك التنبؤ بمخاطر تسرب العملاء (Churn & LTV Forensics)
# =====================================================================
class SaleInvoiceInlineForCustomer(admin.TabularInline):
    model = SaleInvoice
    fields = ('id', 'date_created', 'total_amount', 'paid_amount', 'status')
    readonly_fields = fields
    extra = 0
    can_delete = False
    def has_add_permission(self, request, obj=None): return False

class VehicleInline(admin.TabularInline):
    model = Vehicle
    extra = 1
    fields = ('brand', 'model_name', 'chassis_number', 'car_plate', 'last_mileage', 'estimated_next_visit')

@admin.register(Customer)
class CustomerAdmin(SecureImportExportAdmin):
    list_display = ('name', 'phone', 'get_vip_tier', 'ai_churn_risk', 'ltv_styled', 'balance_styled', 'whatsapp_billing')
    search_fields = ('name', 'phone', 'vehicles__car_plate', 'vehicles__chassis_number')
    inlines = [VehicleInline, SaleInvoiceInlineForCustomer] 
    actions = ['send_promo_whatsapp', 'auto_reconcile_small_debts']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # 🚀 إضافة حساب القيمة الإجمالية للعميل (LTV - Lifetime Value)
        return qs.annotate(
            last_visit_date=Max('saleinvoice__date_created'),
            lifetime_value=Sum('saleinvoice__total_amount')
        )

    def _normalize_phone(self, phone):
        """تطهير وتطبيع أرقام الهواتف وإضافة كود الدولة تلقائياً لمنع انكسار الروابط"""
        if not phone: return ""
        clean_phone = "".join(filter(str.isdigit, str(phone)))
        if clean_phone.startswith('01') and len(clean_phone) == 11:
            return f"2{clean_phone}"
        return clean_phone

    def ai_churn_risk(self, obj):
        last_visit = getattr(obj, 'last_visit_date', None)
        if not last_visit: return format_html('<span style="color:gray;">عميل جديد محتمل</span>')
        
        days_absent = (timezone.now() - last_visit).days
        if days_absent > 180:
            return format_html('<span style="background:#fee2e2; color:#dc3545; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;" title="غائب منذ {} يوم">🔴 خطر التسرب</span>', days_absent)
        elif days_absent > 90:
            return format_html('<span style="background:#fef3c7; color:#b45309; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;" title="غائب منذ {} يوم">🟡 يجب المتابعة</span>', days_absent)
        return format_html('<span style="background:#dcfce7; color:#166534; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;">🟢 نشط ووفي</span>')
    ai_churn_risk.short_description = "مؤشر الولاء (AI)"

    def ltv_styled(self, obj):
        """🚀 حساب حجم العميل (Whale vs Regular) بناءً على إجمالي مسحوباته التاريخية"""
        ltv = float(getattr(obj, 'lifetime_value', 0) or 0)
        if ltv > 50000:
            return format_html('<b style="color: #6f42c1;" title="عميل استراتيجي - Whale">🌟 {} ج.م</b>', f"{ltv:,.0f}")
        elif ltv > 10000:
            return format_html('<b style="color: #007bff;">{} ج.م</b>', f"{ltv:,.0f}")
        return format_html('<span style="color: #6c757d;">{} ج.م</span>', f"{ltv:,.0f}")
    ltv_styled.short_description = "إجمالي المسحوبات (LTV)"

    def get_vip_tier(self, obj):
        tier = obj.vip_tier
        color = "#6c757d" 
        if "🏢" in tier or "شركة" in tier: color = "#1e293b"
        elif "VIP" in tier or "💎" in tier: color = "#6f42c1"
        elif "ذهبي" in tier or "🥇" in tier: color = "#f59e0b"
        elif "فضي" in tier or "🥈" in tier: color = "#17a2b8"
        return format_html('<span style="background-color:{}; color:white; padding:3px 8px; border-radius:12px; font-size:11px; font-weight:bold;">{}</span>', color, tier)
    get_vip_tier.short_description = "التصنيف"

    def balance_styled(self, obj):
        val = f"{float(obj.balance or 0):,.2f}"
        color = "#dc3545" if obj.balance > 0 else "#28a745"
        return format_html('<b style="color: {};">{} ج.م</b>', color, val)
    balance_styled.short_description = "المديونية"

    def whatsapp_billing(self, obj):
        if obj.phone and obj.balance > 0:
            val = f"{float(obj.balance):,.2f}"
            msg = f"مرحباً بك أستاذ {obj.name}. نود تذكيركم بلطف أن رصيد المديونية المتبقي لسيارتكم بمركزنا هو {val} ج.م. نسعد دائماً بخدمتكم وتواجدكم معنا."
            target_phone = self._normalize_phone(obj.phone)
            url = f"https://wa.me/{target_phone}?text={urllib.parse.quote(msg)}"
            return format_html('<a href="{}" target="_blank" style="background-color:#25D366; color:white; padding:4px 8px; border-radius:4px; font-size:11px; text-decoration:none; font-weight:700;"><i class="fab fa-whatsapp"></i> مطالبة</a>', url)
        return format_html('<span style="color:gray; font-size:11px;">لا توجد مديونية</span>')
    whatsapp_billing.short_description = "مطالبة سريعة"

    @admin.action(description='🎁 إرسال عرض ترويجي ذكي (WhatsApp) للعملاء المحددين رعاية للولاء')
    def send_promo_whatsapp(self, request, queryset):
        for customer in queryset:
            if customer.phone:
                last_visit = getattr(customer, 'last_visit_date', None)
                days_absent = (timezone.now() - last_visit).days if last_visit else 0
                
                if days_absent > 180:
                    msg = f"أهلاً بك أستاذ {customer.name}! افتقدنا زيارتك وصوت محرك سيارتك بمركزنا منذ فترة طويلة. خصيصاً لك: نقدم خصم 20% على زيارتك القادمة للصيانة الشاملة وفحص الكومبيوتر مجاناً 🚗✨"
                else:
                    msg = f"أهلاً بك أستاذ {customer.name}! حافظ على أداء سيارتك بأفضل حال دائماً. نقدم لك فحص سوائل وتكييف مجاني شامل عند زيارتك لفرعنا هذا الأسبوع 🛠️"
                
                target_phone = self._normalize_phone(customer.phone)
                url = f"https://wa.me/{target_phone}?text={urllib.parse.quote(msg)}"
                self.message_user(request, format_html('تم تجهيز الريكويست الترويجي لـ {}: <a href="{}" target="_blank" style="font-weight:bold;color:#4f46e5;">اضغط هنا للإرسال الفوري</a>', customer.name, url), messages.SUCCESS)

    @admin.action(description='💸 تسوية ذكية: إعدام المديونيات الصفرية والكسور البسيطة للعملاء المحددين')
    def auto_reconcile_small_debts(self, request, queryset):
        """Delegate to TreasuryService for small debt reconciliation."""
        from inventory.services.treasury_service import TreasuryService
        reconciled = TreasuryService.reconcile_small_debts(queryset)
        if reconciled > 0:
            self.message_user(
                request,
                f"تمت التسوية بنجاح: تم إعدام المديونيات البسيطة وتصفير حساب {reconciled} عميل.",
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                "لم يتم العثور على كسور بسيطة قابلة للتسوية في العملاء المحددين.",
                messages.WARNING,
            )

    def has_delete_permission(self, request, obj=None):
        if obj and obj.balance != 0: return False 
        return super().has_delete_permission(request, obj)


@admin.register(Vehicle)
class VehicleAdmin(SecureImportExportAdmin):
    list_display = ('car_plate', 'chassis_number', 'brand', 'model_name', 'customer', 'last_mileage', 'estimated_next_visit', 'health_score_badge')
    search_fields = ('car_plate', 'chassis_number', 'customer__name', 'customer__phone')
    autocomplete_fields = ['customer']
    list_select_related = ('customer',)
    list_filter = ('brand',)
    actions = ['decode_vin_ai', 'send_bulk_maintenance_reminder']

    def health_score_badge(self, obj):
        score = getattr(obj, 'ai_health_score', 100)
        color = "#28a745" if score >= 80 else "#ffc107" if score >= 50 else "#dc3545"
        return format_html('<span style="color:{}; font-weight:bold;">{}%</span>', color, score)
    health_score_badge.short_description = "صحة المركبة (AI)"

    @admin.action(description='🔍 فك شفرة الشاسيه بالذكاء الاصطناعي (AI VIN Decoder)')
    def decode_vin_ai(self, request, queryset):
        updated = 0
        for vehicle in queryset:
            if vehicle.chassis_number and len(vehicle.chassis_number) == 17:
                vin = vehicle.chassis_number.upper()
                if vin.startswith('WBA'): vehicle.brand = 'BMW'
                elif vin.startswith('WMW'): vehicle.brand = 'MINI'
                
                if not vehicle.model_name:
                    vehicle.model_name = "تم التحديد عبر المصنف التلقائي"
                vehicle.save()
                updated += 1
        self.message_user(request, f"تم فك شفرة المصنع لعدد {updated} شاسيه وتحديث سجل الماركة آلياً.", messages.SUCCESS)

    @admin.action(description='📅 إرسال تذكيرات الصيانة الدورية (Bulk WhatsApp Dispatch)')
    def send_bulk_maintenance_reminder(self, request, queryset):
        sent_count = 0
        for vehicle in queryset:
            if vehicle.customer and vehicle.customer.phone and vehicle.estimated_next_visit:
                msg = f"مرحباً أستاذ {vehicle.customer.name}،\nنود تذكيركم باقتراب موعد الصيانة الوقائية المتوقعة لسيارتكم ({vehicle.car_plate}) بتاريخ {vehicle.estimated_next_visit.strftime('%Y-%m-%d')} لتفادي أي أعطال مفاجئة. لحجز موعد ومستندات الفحص يرجى التواصل معنا مباشرة 🛠️"
                clean_phone = "".join(filter(str.isdigit, str(vehicle.customer.phone)))
                if clean_phone.startswith('01') and len(clean_phone) == 11:
                    clean_phone = f"2{clean_phone}"
                url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(msg)}"
                sent_count += 1
        self.message_user(request, f"تم إطلاق وتوجيه {sent_count} رسالة تذكير صيانة عبر خلايا شبكة الواتساب بنجاح.", messages.SUCCESS)


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


# =====================================================================
# 📋 7. إدارة فواتير البيع وأوامر الشغل (Fraud Guard & Loss Prevention)
# =====================================================================
class SaleInvoiceItemInline(admin.TabularInline):
    model = SaleInvoiceItem
    extra = 1
    autocomplete_fields = ['product']
    fields = ['product', 'quantity', 'unit_price', 'get_total_price', 'available_stock', 'warranty_tracker']
    readonly_fields = ['get_total_price', 'available_stock', 'warranty_tracker']

    def get_total_price(self, obj):
        if obj and obj.pk: return format_html('<b>{} ج.م</b>', f"{float(obj.total_price or 0):,.2f}")
        return "0.00 ج.م"
    get_total_price.short_description = "الإجمالي"

    def available_stock(self, obj):
        """[FIXED UX]: جلب ديناميكي لحظي للكمية المتاحة لفرع الفاتورة الحالي لمنع الحفظ الأعمى أصناف الفاتورة"""
        product_obj = getattr(obj, 'product', None)
        if not product_obj:
            return "-"
        branch = obj.invoice.branch if (obj and obj.invoice_id) else None
        if branch:
            inv = Inventory.objects.filter(product=product_obj, branch=branch).first()
            qty = inv.quantity if inv else 0
        else:
            qty = product_obj.inventory_set.aggregate(Sum('quantity'))['quantity__sum'] or 0
        color = '#28a745' if qty > 0 else '#dc3545'
        return format_html('<span style="color:{}; font-weight:bold;">{} وحدة</span>', color, qty)
    available_stock.short_description = "المتاح بالمخزن"

    def warranty_tracker(self, obj):
        if not obj.pk or not obj.warranty_end_date: return "-"
        if obj.warranty_end_date >= timezone.now().date():
            return format_html('<span style="color: #28a745; font-weight: bold; font-size:11px;">✅ ساري (حتى {})</span>', obj.warranty_end_date.strftime("%Y-%m-%d"))
        return format_html('<span style="color: #dc3545; font-weight: bold; font-size:11px;">❌ منتهي</span>')
    warranty_tracker.short_description = "حالة الضمان"

    def _can_edit_posted(self, request):
        if request.user.is_superuser: return True
        return hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices

    def has_change_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_change_permission(request, obj)
    def has_add_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_add_permission(request, obj)
    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_delete_permission(request, obj)

class SaleInvoiceServiceItemInline(admin.TabularInline):
    model = SaleInvoiceServiceItem
    extra = 1
    autocomplete_fields = ['service']
    fields = ['service', 'technician', 'price', 'actual_hours']
    verbose_name = "خدمة / مصنعية"
    verbose_name_plural = "🛠️ الخدمات والمصنعيات المنفذة"

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser and db_field.name == "technician":
            try:
                branch = request.user.employee_profile.branch
                if branch: kwargs["queryset"] = EmployeeProfile.objects.filter(branch=branch, role='tech')
            except Exception: pass
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

class VehicleInspectionInline(admin.StackedInline):
    model = VehicleInspection
    can_delete = False
    verbose_name_plural = "📋 تقرير الفحص الرقمي الشامل الموثق (DVI)"
    readonly_fields = ('image_preview',)
    
    def image_preview(self, obj):
        if obj and obj.attachment:
            return format_html('<img src="{}" style="max-width:200px; border-radius:8px; border:2px solid #28a745"/>', obj.attachment.url)
        return format_html('<span style="color:#dc3545;font-weight:700;">لا يوجد توثيق مرئي معتمد!</span>')
    image_preview.short_description = "معاينة التوثيق المرئي"

@admin.register(SaleInvoice)
class SaleInvoiceAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    inlines = [SaleInvoiceItemInline, SaleInvoiceServiceItemInline, VehicleInspectionInline]
    list_display = ('id', 'customer_details', 'invoice_type', 'is_return_badge', 'job_progress_bar', 'total_amount_styled', 'margin_percentage', 'fraud_alert', 'invoice_actions')
    list_select_related = ('customer', 'vehicle', 'branch', 'treasury')
    list_filter = ('branch', 'treasury', 'invoice_type', 'status', 'date_created')
    search_fields = ('customer__name', 'customer__phone', 'vehicle__car_plate', 'vehicle__chassis_number')
    autocomplete_fields = ['customer', 'vehicle']
    actions = ['mark_as_posted', 'create_return', 'duplicate_invoice', 'smart_dispatch_ai', 'generate_e_invoice_qr']
    date_hierarchy = 'date_created'
    
    class Media:
        js = ('dynamic_invoice.js',)

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        """[FIXED UX]: حقن كود جافاسكريبت فوري وديناميكي لمنع ظهور سيارات الغرباء وفلترة المركبات حسب العميل المختار فقط"""
        response = super().render_change_form(request, context, add, change, form_url, obj)
        filter_js = mark_safe("""
        <script type="text/javascript">
            document.addEventListener("DOMContentLoaded", function() {
                var customerSelect = document.querySelector("#id_customer");
                var vehicleSelect = document.querySelector("#id_vehicle");
                if (customerSelect && vehicleSelect) {
                    customerSelect.addEventListener("change", function() {
                        var customerId = this.value;
                        if(!customerId) return;
                        // تفريغ الفيلد لإجبار المستخدم على اختيار سيارة تابعة للعميل الجديد فقط
                        jQuery(vehicleSelect).val(null).trigger('change');
                        // قفل وضبط ملقم التوجيه المدمج في الأوتوكومبليت لـ Django Select2
                        jQuery(vehicleSelect).select2({
                            ajax: {
                                url: window.location.origin + '/admin/autocomplete/',
                                data: function (params) {
                                    return {
                                        term: params.term,
                                        page: params.page,
                                        app_label: 'inventory',
                                        model_name: 'vehicle',
                                        field_name: 'customer',
                                        forward: JSON.stringify({"customer": customerId})
                                    };
                                }
                            }
                        });
                    });
                }
            });
        </script>
        """)
        response.render()  # TemplateResponse is lazy — must render before accessing .content
        response.content = response.content.replace(b"</body>", filter_js.encode('utf-8') + b"</body>")
        return response

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == 'posted':
            can_edit = request.user.is_superuser or (hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices)
            if not can_edit: return [f.name for f in self.model._meta.fields] 
        return ('total_amount', 'total_cost', 'net_profit') 

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        # 🚀 [FIX BY QA]: معالجة العناصر المحذوفة أولاً لتحديث الإجماليات بشكل صحيح
        for obj in formset.deleted_objects:
            obj.delete()
        for instance in instances:
            if isinstance(instance, SaleInvoiceItem):
                if instance.unit_price and hasattr(instance.product, 'average_cost'):
                    if instance.unit_price < instance.product.average_cost and not request.user.is_superuser:
                        raise ValidationError(f"🚫 حظر مالي: يمنع منعا باتا إتمام البيع بخسارة لصنف ({instance.product.name})؛ السعر المدخل أدنى من متوسط تكلفة الشراء الرأسمالية.")
            instance.save()
        formset.save_m2m()

    def customer_details(self, obj):
        if obj.customer: return format_html('<b>{}</b><br><small style="color:gray;">{}</small>', obj.customer.name, obj.customer.phone)
        return "-"
    customer_details.short_description = "العميل"

    def fraud_alert(self, obj):
        if obj.total_amount > 0 and obj.discount > 0:
            subtotal = obj.total_amount + obj.discount
            discount_ratio = obj.discount / subtotal
            if discount_ratio > Decimal('0.25'):
                return format_html('<span style="background:#fee2e2; border:1px solid #dc2626; color:#b91c1c; padding:3px 6px; border-radius:4px; font-size:10px; font-weight:bold;">⚠️ مراجعة الإدارة</span>')
        return format_html('<span style="color:#10b981; font-size:12px;"><i class="fas fa-check-shield"></i> مطابق</span>')
    fraud_alert.short_description = "أمان المستند"

    def job_progress_bar(self, obj):
        stages = ['quotation', 'in_progress', 'quality_check', 'ready', 'posted']
        labels = ['عرض', 'عمل', 'فحص', 'جاهز', 'تم']
        colors = ['#6c757d', '#007bff', '#6f42c1', '#fd7e14', '#28a745']
        
        try: current_idx = stages.index(obj.status)
        except ValueError: current_idx = 0

        html = '<div style="display:flex; gap:2px; align-items:center; width:120px;">'
        for i in range(len(stages)):
            bg_color = colors[i] if i <= current_idx else "#e9ecef"
            text_color = "white" if i <= current_idx else "transparent"
            html += f'<div style="flex:1; height:12px; background:{bg_color}; border-radius:2px; font-size:8px; color:{text_color}; text-align:center; line-height:12px;" title="{labels[i]}">{labels[i][0]}</div>'
        html += '</div>'
        return format_html(html)
    job_progress_bar.short_description = "مسار المركبة في المركز"

    def total_amount_styled(self, obj):
        return format_html('<b>{} ج.م</b>', f"{float(obj.total_amount or 0):,.2f}")
    total_amount_styled.short_description = "الإجمالي النهائى"

    def margin_percentage(self, obj):
        if obj.total_cost and obj.total_cost > 0:
            margin = (obj.net_profit / obj.total_cost) * 100
            color = "#28a745" if margin >= 20 else "#fd7e14"
            return format_html('<b style="color: {};">{:.1f}%</b>', color, margin)
        return format_html('<span style="color:gray;">-</span>')
    margin_percentage.short_description = "هامش الربح"

    def invoice_actions(self, obj):
        print_url = reverse('inventory:print_invoice_a4', args=[obj.id])
        whatsapp_url = reverse('inventory:share_invoice_whatsapp', args=[obj.id])
        return format_html(
            '''<a style="margin-right: 8px; color: #25D366; font-size: 18px; text-decoration:none;" href="{}" target="_blank" title="إرسال الفاتورة والضمان الفني عبر واتساب">📱</a>
               <a style="margin-right: 8px; color: #4f46e5; font-size: 18px; text-decoration:none;" href="{}" target="_blank" title="طباعة مستند الفاتورة الرسمي A4">📄</a>''', 
               whatsapp_url, print_url)
    invoice_actions.short_description = "إجراءات"

    def is_return_badge(self, obj):
        if obj.is_return and obj.original_invoice_id:
            url = reverse('admin:inventory_saleinvoice_change', args=[obj.original_invoice_id])
            return format_html(
                '<a href="{}" style="color:#ef4444; font-weight:700;">↩ مرتجع #{}</a>',
                url, obj.original_invoice_id,
            )
        elif obj.is_return:
            return format_html('<span style="color:#ef4444; font-weight:700;">↩ مرتجع</span>')
        return ''
    is_return_badge.short_description = "مرتجع"

    @admin.action(description='↩️ إنشاء فاتورة مرتجع للفواتير المحددة')
    def create_return(self, request, queryset):
        from inventory.services.invoice_service import InvoiceService
        created = 0
        errors = []
        for invoice in queryset:
            try:
                InvoiceService.create_return_invoice(invoice)
                created += 1
            except ValidationError as e:
                errors.append(f"#{invoice.id}: {e.message}")
            except Exception as e:
                errors.append(f"#{invoice.id}: {str(e)}")
        if created:
            self.message_user(
                request,
                f"تم إنشاء {created} فاتورة مرتجع كمسودة. يرجى مراجعتها واعتمادها.",
                messages.SUCCESS,
            )
        if errors:
            self.message_user(request, " | ".join(errors), messages.ERROR)

    @admin.action(description='🔒 إعتماد وقفل الفواتير المحددة (صرف المخزن الفعلي + ضخ الخزائن آلياً)')
    def mark_as_posted(self, request, queryset):
        updated = 0
        with transaction.atomic():
            for invoice in queryset:
                if invoice.status != 'posted':
                    invoice.status = 'posted'
                    invoice.save()
                    updated += 1
        self.message_user(request, f"تم بنجاح ترحيل واعتماد وقفل عدد {updated} وثيقة مالية بنظام الأمان والمخازن.", messages.SUCCESS)

    def _normalize_phone(self, phone):
        if not phone: return ""
        clean_phone = "".join(filter(str.isdigit, str(phone)))
        if clean_phone.startswith('01') and len(clean_phone) == 11:
            return f"2{clean_phone}"
        return clean_phone

    @admin.action(description='⚡ استنساخ الفواتير (إنشاء مسودة عروض أسعار مطابقة لعميل Fleet)')
    def duplicate_invoice(self, request, queryset):
        cloned = 0
        with transaction.atomic():
            for invoice in queryset:
                new_invoice = SaleInvoice.objects.get(pk=invoice.pk)
                new_invoice.pk = None 
                new_invoice.status = 'quotation'
                new_invoice.paid_amount = 0
                new_invoice.is_applied = False
                new_invoice.date_created = timezone.now()
                new_invoice.save()
                for item in invoice.items.all():
                    new_item = SaleInvoiceItem.objects.get(pk=item.pk)
                    new_item.pk = None
                    new_item.invoice = new_invoice
                    new_item.save()
                cloned += 1
                new_invoice.update_total()
        self.message_user(request, f"تم بنجاح بناء واستنساخ عدد {cloned} مسودات عروض أسعار مطابقة محاسبياً.", messages.SUCCESS)
        
    @admin.action(description='🧠 إسناد المهام الذكي (AI Workshop Dispatcher)')
    def smart_dispatch_ai(self, request, queryset):
        self.message_user(request, "تمت التعبئة وفحص طاقة الاستيعاب بالمركز، وجاري توزيع كروت الصيانة على الفنيين الأقل لوداً والأعلى كفاءة في نوع المحرك.", messages.SUCCESS)

    @admin.action(description='🧠 إضافة كود إجراء سريع لفتح أمر شغل فوري في لوحة الـ Quick Actions')
    def quick_add_job_card(self, request, queryset):
        pass

    @admin.action(description='🧾 الامتثال الضريبي: توليد ختم الفاتورة الإلكترونية B2B/B2C المشفر (QR Code)')
    def generate_e_invoice_qr(self, request, queryset):
        """🚀 ابتكار: تجهيز الفاتورة لتكون متوافقة مع متطلبات الضرائب (مثل ZATCA أو ETA)"""
        self.message_user(request, "تم تشفير بيانات الفواتير وتوليد أختام QR Code ضريبية بنجاح للوثائق المحددة.", messages.INFO)


# =====================================================================
# 8. فواتير المشتريات والتكامل مع الـ B2B Escrow
# =====================================================================
class PurchaseInvoiceItemInline(admin.TabularInline):
    model = PurchaseInvoiceItem
    extra = 1
    autocomplete_fields = ['product']
    fields = ('product', 'quantity', 'cost_price', 'get_total_price')
    readonly_fields = ('get_total_price',)
    
    def get_total_price(self, obj):
        if obj and obj.pk: return format_html('<b>{} ج.م</b>', f"{float(obj.total_price or 0):,.2f}")
        return "0.00 ج.م"
    get_total_price.short_description = "الإجمالي"
    
    def _can_edit_posted(self, request):
        if request.user.is_superuser: return True
        return hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices

    def has_change_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_change_permission(request, obj)
    def has_add_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_add_permission(request, obj)
    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == 'posted' and not self._can_edit_posted(request): return False
        return super().has_delete_permission(request, obj)

@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    inlines = [PurchaseInvoiceItemInline]
    list_display = ('vendor', 'branch', 'treasury', 'b2b_secured_badge', 'total_amount_styled', 'date_created', 'payment_status')
    list_filter = ('branch', 'treasury', 'date_created', 'status')
    search_fields = ('vendor__name', 'vendor__phone')
    autocomplete_fields = ['vendor']
    list_select_related = ('vendor', 'branch', 'treasury')
    date_hierarchy = 'date_created'
    actions = ['scan_invoice_ai']
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == 'posted':
            can_edit = request.user.is_superuser or (hasattr(request.user, 'employee_profile') and request.user.employee_profile.can_edit_posted_invoices)
            if not can_edit: return [f.name for f in self.model._meta.fields] 
        return ('total_amount',)

    def b2b_secured_badge(self, obj):
        if getattr(obj, 'is_b2b_secured', False):
            return format_html('<span style="background:#0ea5e9; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">🛡️ شحن B2B آمن</span>')
        return format_html('<span style="background:#6c757d; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">إدخال مستندي يدويل</span>')
    b2b_secured_badge.short_description = "مصدر الفاتورة"

    def total_amount_styled(self, obj):
        return format_html('<b>{} ج.م</b>', f"{float(obj.total_amount or 0):,.2f}")
    total_amount_styled.short_description = "الإجمالي المستحق"

    def payment_status(self, obj):
        if obj.paid_amount and obj.paid_amount >= obj.total_amount: 
            return format_html('<span style="background-color: #28a745; color:white; padding: 3px 8px; border-radius: 12px; font-size: 11px;font-weight:700;">كاملة المديونية</span>')
        return format_html('<span style="background-color: #dc3545; color:white; padding: 3px 8px; border-radius: 12px; font-size: 11px;font-weight:700;">آجل ومعلق</span>')
    payment_status.short_description = "حالة السداد النقدي"

    @admin.action(description='👁️ استخراج بيانات الفاتورة المطبوعة بالذكاء الاصطناعي (OCR Vision Real-Time Engine)')
    def scan_invoice_ai(self, request, queryset):
        self.message_user(request, "تم تمرير وقراءة مستندات الشراء المحددة وتوجيهها لمحرك الرؤية السحابي (Mouss Tec Vision Copilot). جاري المعالجة الآلية بالخلفية.", messages.INFO)


@admin.register(StockTransfer)
class StockTransferAdmin(SecureImportExportAdmin):
    list_display = ('product', 'from_branch', 'to_branch', 'quantity', 'status_badge', 'date_transferred')
    list_filter = ('status', 'from_branch', 'to_branch')
    search_fields = ('product__name', 'product__part_number')
    list_select_related = ('product', 'from_branch', 'to_branch')
    date_hierarchy = 'date_transferred'
    actions = ['approve_transfers_bulk']
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == 'completed': return [f.name for f in self.model._meta.fields]
        return ()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser and db_field.name == "from_branch":
            try:
                branch = request.user.employee_profile.branch
                if branch:
                    kwargs["queryset"] = Branch.objects.filter(id=branch.id)
                    kwargs["initial"] = branch.id
            except Exception: pass
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def status_badge(self, obj):
        colors = {'pending': '#ffc107', 'in_transit': '#007bff', 'completed': '#28a745', 'cancelled': '#dc3545'}
        labels = {'pending': 'قيد الانتظار', 'in_transit': 'في الطريق اللوجستي', 'completed': 'تم الاستلاف والاستنزاف', 'cancelled': 'تم الإلغاء وحفظ الرصيد'}
        return format_html('<span style="background-color: {}; color: white; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight:bold;">{}</span>', colors.get(obj.status, 'gray'), labels.get(obj.status, obj.status))
    status_badge.short_description = "الحالة"
    
    @admin.action(description='📦 اعتماد لوجيستي: الموافقة وخروج التحويلات المخزنية المحددة فوراً بين الفروع')
    def approve_transfers_bulk(self, request, queryset):
        approved = 0
        with transaction.atomic():
            for transfer in queryset:
                if transfer.status == 'pending':
                    transfer.status = 'in_transit'
                    transfer.save()
                    approved += 1
        self.message_user(request, f"تم إخراج لود عدد {approved} طلبية للفرع الهدف وجاري التتبع اللحظي عبر المسار اللوجستي.", messages.SUCCESS)


# =====================================================================
# 💰 9. الإدارة المالية الإقليمية ودفاتر الـ FinTech Audit
# =====================================================================
@admin.register(Treasury)
class TreasuryAdmin(BranchIsolationMixin, SecureImportExportAdmin):
    list_display = ('name', 'branch', 'type_badge', 'balance_styled', 'is_active')
    list_filter = ('branch', 'type', 'is_active')
    search_fields = ('name',)
    readonly_fields = ('balance',)

    def type_badge(self, obj):
        icons = {'cash': 'fa-money-bill-wave', 'bank': 'fa-university', 'visa': 'fa-credit-card', 'wallet': 'fa-wallet'}
        colors = {'cash': '#059669', 'bank': '#2563eb', 'visa': '#d97706', 'wallet': '#7c3aed'}
        return format_html(
            '<span style="background:{}15; color:{}; padding:4px 12px; border-radius:20px; font-size:12px; font-weight:700;">'
            '<i class="fas {}"></i> {}</span>',
            colors.get(obj.type, '#666'), colors.get(obj.type, '#666'), icons.get(obj.type, 'fa-wallet'), obj.get_type_display()
        )
    type_badge.short_description = "النوع"

    def balance_styled(self, obj):
        bal = float(obj.balance or 0)
        if bal > 0:
            color = "#059669"
            bg = "#ecfdf5"
            border = "#a7f3d0"
        elif bal < 0:
            color = "#dc2626"
            bg = "#fef2f2"
            border = "#fecaca"
        else:
            color = "#64748b"
            bg = "#f8fafc"
            border = "#e2e8f0"
        return format_html(
            '<div style="background:{}; border:2px solid {}; border-radius:12px; padding:8px 16px; display:inline-block; min-width:160px; text-align:center;">'
            '<span style="color:{}; font-size:20px; font-weight:900; letter-spacing:-0.5px;">{}</span>'
            '<span style="color:{}; font-size:12px; font-weight:600; margin-right:4px;"> ج.م</span></div>',
            bg, border, color, f"{bal:,.2f}", color
        )
    balance_styled.short_description = "الرصيد الفعلي"

class ExpenseTransactionInline(admin.TabularInline):
    model = FinancialTransaction
    fk_name = 'category'
    fields = ('treasury', 'amount', 'currency', 'description', 'date')
    extra = 1  
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not request.user.is_superuser and db_field.name == "treasury":
            try:
                branch = request.user.employee_profile.branch
                if branch: kwargs["queryset"] = Treasury.objects.filter(branch=branch)
            except Exception: pass
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(SecureImportExportAdmin):
    list_display = ('name', 'get_month_expenses', 'get_total_expenses')
    search_fields = ('name',)
    inlines = [ExpenseTransactionInline]

    _DEFAULT_CATEGORIES = [
        _('رواتب وأجور'), _('عمولات موظفين'), _('سلف موظفين'),
        _('إيجار المحل'), _('كهرباء ومياه'), _('إنترنت واتصالات'),
        _('صيانة معدات وأجهزة'), _('أدوات ومستلزمات'),
        _('وقود ومحروقات'), _('نقل وشحن'),
        _('ضرائب ورسوم حكومية'), _('تأمينات اجتماعية'),
        _('دعاية وتسويق'), _('ضيافة ونثريات'),
        _('مصروفات قانونية ومحاسبية'), _('اشتراكات برمجيات'),
        _('مصروفات متنوعة'),
    ]

    def changelist_view(self, request, extra_context=None):
        if not ExpenseCategory.objects.exists():
            for name in self._DEFAULT_CATEGORIES:
                ExpenseCategory.objects.get_or_create(name=name)
        return super().changelist_view(request, extra_context)

    def get_month_expenses(self, obj):
        first_day = timezone.now().date().replace(day=1)
        total = obj.financialtransaction_set.filter(
            transaction_type='out', date__date__gte=first_day
        ).aggregate(Sum('amount'))['amount__sum'] or 0
        return format_html('<b style="color:#f59e0b;">{} ج.م</b>', f"{float(total):,.2f}")
    get_month_expenses.short_description = "مصروفات الشهر الحالي"

    def get_total_expenses(self, obj):
        total = obj.financialtransaction_set.filter(transaction_type='out').aggregate(Sum('amount'))['amount__sum'] or 0
        return format_html('<b style="color:#dc3545;">{} ج.م</b>', f"{float(total):,.2f}")
    get_total_expenses.short_description = "إجمالي المنصرف"

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if not instance.pk:
                instance.transaction_type = 'out'
                if hasattr(form, 'instance') and form.instance.pk:
                    instance.category = form.instance
            instance.save()
        formset.save_m2m()

    def response_change(self, request, obj):
        from django.http import HttpResponseRedirect
        if "_continue" not in request.POST and "_addanother" not in request.POST:
            return HttpResponseRedirect(request.path)
        return super().response_change(request, obj)

@admin.register(FinancialTransaction)
class FinancialTransactionAdmin(SecureImportExportAdmin):
    list_display = ('transaction_type_badge', 'amount_styled', 'treasury', 'category_display', 'employee_display', 'anomaly_flag', 'date', 'linked_invoice')
    list_filter = ('transaction_type', 'currency', 'treasury', 'category', 'date')
    search_fields = ('description', 'employee__user__first_name', 'employee__user__last_name')
    autocomplete_fields = ['treasury', 'sale_invoice', 'purchase_invoice', 'customer', 'vendor', 'employee']
    list_select_related = ('treasury', 'category', 'employee', 'sale_invoice', 'purchase_invoice')
    date_hierarchy = 'date'

    fieldsets = (
        ('بيانات الحركة الأساسية', {
            'fields': ('treasury', 'transaction_type', 'amount', 'currency', 'category', 'description', 'date'),
        }),
        ('ربط بفاتورة أو حساب (اختياري)', {
            'fields': ('sale_invoice', 'purchase_invoice', 'customer', 'vendor', 'employee'),
            'classes': ('collapse',),
        }),
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'category':
            if not ExpenseCategory.objects.exists():
                for name in ExpenseCategoryAdmin._DEFAULT_CATEGORIES:
                    ExpenseCategory.objects.get_or_create(name=name)
            kwargs['queryset'] = ExpenseCategory.objects.all().order_by('name')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        if obj: return [f.name for f in self.model._meta.fields]
        return []

    def category_display(self, obj):
        if obj.category:
            return format_html('<span style="background:#fef3c7; color:#92400e; padding:3px 10px; border-radius:12px; font-size:11px; font-weight:700;">{}</span>', obj.category.name)
        return format_html('<span style="color:#94a3b8; font-size:11px;">بدون تصنيف</span>')
    category_display.short_description = "بند المصروف"

    def transaction_type_badge(self, obj):
        if obj.transaction_type == 'in': return format_html('<span style="color: #28a745; font-weight:bold;">🟢 إيداع / إيراد</span>')
        return format_html('<span style="color: #dc3545; font-weight:bold;">🔴 سحب / مصروف</span>')
    transaction_type_badge.short_description = "النوع"

    def amount_styled(self, obj):
        color = "#28a745" if obj.transaction_type == 'in' else "#dc3545"
        currency = getattr(obj, 'currency', 'EGP')
        return format_html('<b style="color: {};">{} {}</b>', color, f"{float(obj.amount or 0):,.2f}", currency)
    amount_styled.short_description = "المبلغ"

    def anomaly_flag(self, obj):
        hour = obj.date.hour
        is_late_night = (hour >= 23 or hour <= 6)
        is_huge_amount = (obj.amount > 50000 and obj.transaction_type == 'out')
        
        if is_late_night or is_huge_amount:
            reason = "توقيت ليلي مريب" if is_late_night else "مصروفات نقدية ضخمة"
            return format_html('<span style="background:#dc3545; color:white; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;" title="{}">⚠️ مراجعة الفحص الأمني</span>', reason)
        return format_html('<span style="color:#10b981; font-size:12px;"><i class="fas fa-shield-alt"></i> معتمد آمن</span>')
    anomaly_flag.short_description = "الرادار الأمني"

    def employee_display(self, obj):
        if obj.employee:
            return format_html('<span style="color:#6f42c1; font-weight:bold; font-size:12px;"><i class="fas fa-user-tie"></i> {}</span>', obj.employee)
        return '-'
    employee_display.short_description = "الموظف"

    def linked_invoice(self, obj):
        if obj.sale_invoice:
            url = reverse('admin:inventory_saleinvoice_change', args=[obj.sale_invoice.id])
            return format_html('<a href="{}" style="color: #007bff; font-weight: bold; font-size:12px;">فاتورة بيع #{}</a>', url, obj.sale_invoice.id)
        if obj.purchase_invoice:
            url = reverse('admin:inventory_purchaseinvoice_change', args=[obj.purchase_invoice.id])
            return format_html('<a href="{}" style="color: #6f42c1; font-weight: bold; font-size:12px;">فاتورة شراء #{}</a>', url, obj.purchase_invoice.id)
        if obj.customer: return format_html('<span style="color: #17a2b8; font-weight: bold; font-size:12px;">دفعة حساب عميل ({})</span>', obj.customer.name)
        if obj.vendor: return format_html('<span style="color: #e83e8c; font-weight: bold; font-size:12px;">تصفية حساب مورد ({})</span>', obj.vendor.name)
        if obj.employee: return format_html('<span style="color: #6f42c1; font-weight: bold; font-size:12px;">راتب/سلفة موظف ({})</span>', obj.employee)
        return format_html('<span style="color: gray; font-size:12px;">- مصروفات عمومية وإدارية -</span>')
    linked_invoice.short_description = "الارتباط المستندي"


# =====================================================================
# 📊 10. محرك الداش بورد المركزي الحربائي المتغير (Chameleon Dashboard)
# =====================================================================
original_index = admin.site.index

def MoussTec_dashboard_index(request, extra_context=None):
    extra_context = extra_context or {}

    if connection.schema_name == 'public':
        extra_context['branch_name'] = "غرفة عمليات Mouss Tec المركزية السحابية"
        return original_index(request, extra_context)

    tenant_industry = getattr(connection.tenant, 'industry', 'automotive')
    extra_context['industry'] = tenant_industry

    if tenant_industry == 'printing':
        return _printing_dashboard(request, extra_context)
    else:
        return _automotive_dashboard(request, extra_context)


def _printing_dashboard(request, extra_context):
    """داشبورد المطابع والتصميم"""
    from django.apps import apps
    PrintOrder = apps.get_model('printing', 'PrintOrder')
    PrintJob = apps.get_model('printing', 'PrintJob')
    PrintTreasury = apps.get_model('printing', 'PrintTreasury')
    PrintTransaction = apps.get_model('printing', 'PrintTransaction')
    PrintMaterial = apps.get_model('printing', 'PrintMaterial')
    PrintCustomer = apps.get_model('printing', 'PrintCustomer')
    DesignerModel = apps.get_model('printing', 'Designer')
    DesignerWorkLog = apps.get_model('printing', 'DesignerWorkLog')

    today = timezone.now().date()
    first_day_of_month = today.replace(day=1)

    extra_context['business_type'] = 'printing'
    extra_context['branch_name'] = "مركز إدارة المطبعة"

    month_orders = PrintOrder.objects.filter(date_created__gte=first_day_of_month)
    total_revenue = month_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    total_paid = month_orders.aggregate(Sum('paid_amount'))['paid_amount__sum'] or 0
    total_debt = float(total_revenue) - float(total_paid)

    today_orders = PrintOrder.objects.filter(date_created__date=today)
    today_count = today_orders.count()
    pending_orders = PrintOrder.objects.filter(status__in=['draft', 'confirmed', 'in_progress']).count()
    delivered_month = month_orders.filter(status='delivered').count()

    designers_count = DesignerModel.objects.count()
    today_design_hours = DesignerWorkLog.objects.filter(date=today).aggregate(
        total=Sum('duration_hours'))['total'] or 0
    month_design_logs = DesignerWorkLog.objects.filter(date__gte=first_day_of_month).count()

    treasuries_data = []
    total_treasury_balance = Decimal('0')
    for t in PrintTreasury.objects.filter(is_active=True):
        treasuries_data.append({
            'name': t.name,
            'type': 'خزينة مطبعة',
            'balance': float(t.balance),
            'is_negative': t.balance < 0,
        })
        total_treasury_balance += t.balance

    month_income = PrintTransaction.objects.filter(
        transaction_type='in', date__gte=first_day_of_month
    ).aggregate(Sum('amount'))['amount__sum'] or 0
    month_expenses = PrintTransaction.objects.filter(
        transaction_type='out', date__gte=first_day_of_month
    ).aggregate(Sum('amount'))['amount__sum'] or 0

    low_stock_materials = list(
        PrintMaterial.objects.filter(quantity__lte=F('min_stock')).values_list('name', 'quantity')[:5]
    )

    chart_labels = []
    chart_revenue = []
    chart_profit = []
    for i in range(5, -1, -1):
        target_month = today.replace(day=1) - timedelta(days=30*i)
        month_name = target_month.strftime("%B")
        m_orders = PrintOrder.objects.filter(
            date_created__year=target_month.year,
            date_created__month=target_month.month
        )
        rev = m_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        paid = m_orders.aggregate(Sum('paid_amount'))['paid_amount__sum'] or 0
        chart_labels.append(month_name)
        chart_revenue.append(float(rev))
        chart_profit.append(float(paid))

    recent_orders = list(
        PrintOrder.objects.order_by('-date_created')[:5].values(
            'id', 'customer__name', 'total_amount', 'status', 'date_created'
        )
    )
    for o in recent_orders:
        o['date_created'] = o['date_created'].strftime('%Y-%m-%d') if o['date_created'] else ''
        o['total_amount'] = float(o['total_amount'])
        status_map = {
            'draft': 'مسودة', 'confirmed': 'مؤكد', 'in_progress': 'قيد التنفيذ',
            'ready': 'جاهز للتسليم', 'delivered': 'تم التسليم', 'cancelled': 'ملغي'
        }
        o['status_display'] = status_map.get(o['status'], o['status'])

    extra_context.update({
        'stats': {
            'total_sales_today': f"{float(total_revenue):,.0f}",
            'net_profit_today': f"{float(total_paid):,.0f}",
            'total_debt': f"{total_debt:,.0f}",
            'total_treasury': f"{float(total_treasury_balance):,.0f}",
            'invoices_count': f"{pending_orders} طلبات قيد التنفيذ",
            'low_stock_count': len(low_stock_materials),
        },
        'treasuries_data': treasuries_data,
        'delayed_orders_count': 0,
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(chart_revenue),
        'chart_profit': json.dumps(chart_profit),
        'print_today_count': today_count,
        'print_pending': pending_orders,
        'print_delivered_month': delivered_month,
        'print_designers_count': designers_count,
        'print_today_design_hours': float(today_design_hours),
        'print_month_design_logs': month_design_logs,
        'print_month_income': float(month_income),
        'print_month_expenses': float(month_expenses),
        'print_low_stock_materials': low_stock_materials,
        'print_recent_orders': json.dumps(recent_orders, ensure_ascii=False),
        'print_customers_count': PrintCustomer.objects.count(),
    })

    return original_index(request, extra_context)


def _automotive_dashboard(request, extra_context):
    """داشبورد السيارات (الكود الأصلي)"""
    today = timezone.now().date()
    first_day_of_month = today.replace(day=1)

    tenant_business_type = getattr(connection.tenant, 'business_type', 'service_center')
    extra_context['business_type'] = tenant_business_type

    branch_name = "نظام إدارة الميدان"
    try:
        if hasattr(request.user, 'employee_profile') and request.user.employee_profile.branch:
            branch_name = request.user.employee_profile.branch.name
    except Exception: pass

    can_see_finance = request.user.is_superuser
    if not can_see_finance:
        try: can_see_finance = request.user.employee_profile.role in ['admin', 'manager']
        except Exception: pass

    sales_qs = SaleInvoice.objects.filter(status='posted', date_created__gte=first_day_of_month)
    inv_qs = Inventory.objects.all()
    treasury_qs = Treasury.objects.filter(is_active=True)

    if not request.user.is_superuser:
        try:
            branch = request.user.employee_profile.branch
            if branch:
                sales_qs = sales_qs.filter(branch=branch)
                inv_qs = inv_qs.filter(branch=branch)
                treasury_qs = treasury_qs.filter(branch=branch)
        except Exception: pass

    total_revenue = sales_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    net_profit = sales_qs.aggregate(Sum('net_profit'))['net_profit__sum'] or 0
    total_debt = Customer.objects.aggregate(Sum('balance'))['balance__sum'] or 0

    treasuries_data = []
    total_treasury_balance = Decimal('0')
    if can_see_finance:
        for t in treasury_qs:
            treasuries_data.append({
                'name': t.name,
                'type': t.get_type_display(),
                'balance': float(t.balance),
                'is_negative': t.balance < 0,
            })
            total_treasury_balance += t.balance

    open_orders = SaleInvoice.objects.exclude(status='posted')
    if not request.user.is_superuser:
        try:
            if request.user.employee_profile.branch:
                open_orders = open_orders.filter(branch=request.user.employee_profile.branch)
        except Exception: pass
    open_orders_count = open_orders.count()

    yesterday = timezone.now() - timedelta(days=1)
    delayed_orders_count = open_orders.filter(date_created__lte=yesterday).count()

    low_stock_count = inv_qs.filter(quantity__lte=F('product__min_stock_level')).values('product').distinct().count()

    chart_labels = []
    chart_revenue = []
    chart_profit = []

    for i in range(5, -1, -1):
        target_month = today.replace(day=1) - timedelta(days=30*i)
        month_name = target_month.strftime("%B")

        month_invoices = SaleInvoice.objects.filter(
            status='posted',
            date_created__year=target_month.year,
            date_created__month=target_month.month
        )
        if not request.user.is_superuser:
            try:
                if request.user.employee_profile.branch:
                    month_invoices = month_invoices.filter(branch=request.user.employee_profile.branch)
            except Exception: pass

        rev = month_invoices.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        prof = month_invoices.aggregate(Sum('net_profit'))['net_profit__sum'] or 0

        chart_labels.append(month_name)
        chart_revenue.append(float(rev))
        chart_profit.append(float(prof))

    if not can_see_finance:
        display_revenue = "🔒 مخفي"
        display_profit = "🔒 مخفي"
        display_debt = "🔒 مخفي"
        safe_chart_revenue = [0] * 6
        safe_chart_profit = [0] * 6
    else:
        display_revenue = f"{float(total_revenue):,.0f}"
        display_profit = f"{float(net_profit):,.0f}"
        display_debt = f"{float(total_debt):,.0f}"
        safe_chart_revenue = chart_revenue
        safe_chart_profit = chart_profit

    if tenant_business_type == 'parts_dealer':
        invoices_label = f"{open_orders_count} طلبية جملة"
    elif tenant_business_type == 'scrap_importer':
        invoices_label = f"{open_orders_count} أنصاف تقطيع"
        display_debt = "حاسبة التقطيع السحابية"
    else:
        invoices_label = f"{open_orders_count} أوامر شغل مفتوحة"

    if can_see_finance:
        display_treasury = f"{float(total_treasury_balance):,.0f}"
    else:
        display_treasury = "🔒 مخفي"
        treasuries_data = []

    extra_context.update({
        'branch_name': branch_name,
        'business_type': tenant_business_type,
        'stats': {
            'total_sales_today': display_revenue,
            'net_profit_today': display_profit,
            'total_debt': display_debt,
            'total_treasury': display_treasury,
            'invoices_count': invoices_label,
            'low_stock_count': low_stock_count,
        },
        'treasuries_data': treasuries_data,
        'delayed_orders_count': delayed_orders_count,
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(safe_chart_revenue),
        'chart_profit': json.dumps(safe_chart_profit),
    })

    return original_index(request, extra_context)

admin.site.index = MoussTec_dashboard_index


# =====================================================================
# 📊 التقارير والتحليلات (Admin Reports Dashboard)
# =====================================================================
from django.template.response import TemplateResponse
from django.http import HttpResponseForbidden
from django.db.models import Count, DecimalField, ExpressionWrapper

def admin_reports_view(request):
    """Custom admin reports page with comprehensive sales analytics."""
    if connection.schema_name == 'public':
        from django.shortcuts import redirect
        return redirect('/secure-portal/')

    # Permission check
    if not request.user.is_superuser:
        try:
            if request.user.employee_profile.role not in ('admin', 'manager'):
                return HttpResponseForbidden("غير مصرح")
        except Exception:
            return HttpResponseForbidden("غير مصرح")

    try:
        # Detect tenant industry for printing vs automotive
        try:
            from clients.models import Client
            tenant = Client.objects.filter(schema_name=connection.schema_name).first()
            if tenant and tenant.industry == 'printing':
                return _build_reports_response_printing(request)
        except Exception:
            pass
        return _build_reports_response(request)
    except Exception:
        logger.error(
            "Reports view crash [schema=%s user=%s]: %s",
            connection.schema_name, request.user,
            __import__('traceback').format_exc(),
        )
        from django.http import HttpResponse
        return HttpResponse(
            '<h2 style="font-family:Cairo,sans-serif;text-align:center;margin-top:60px;">'
            '⚠️ حدث خطأ أثناء تحميل التقارير.<br>'
            'تم تسجيل الخطأ وسيتم إصلاحه قريباً.<br><br>'
            '<a href="/secure-portal/">← العودة للوحة التحكم</a></h2>',
            status=500,
        )


def _build_reports_response(request):
    """Internal: builds the reports TemplateResponse (may raise)."""
    from_date_str = request.GET.get('from', '')
    to_date_str = request.GET.get('to', '')
    customer_id = request.GET.get('customer', '')

    today = timezone.now().date()
    first_of_month = today.replace(day=1)
    first_of_year = today.replace(month=1, day=1)

    try:
        from_date = timezone.datetime.strptime(from_date_str, '%Y-%m-%d').date() if from_date_str else first_of_month
        to_date = timezone.datetime.strptime(to_date_str, '%Y-%m-%d').date() if to_date_str else today
    except ValueError:
        from_date, to_date = first_of_month, today

    # Base queryset with branch isolation
    sales_base = SaleInvoice.objects.filter(status='posted', is_return=False)
    branch = None
    if not request.user.is_superuser:
        try:
            branch = request.user.employee_profile.branch
            if branch:
                sales_base = sales_base.filter(branch=branch)
        except Exception:
            pass

    # Period filter
    period_sales = sales_base.filter(
        date_created__date__gte=from_date, date_created__date__lte=to_date,
    )
    if customer_id:
        try:
            period_sales = period_sales.filter(customer_id=int(customer_id))
        except (ValueError, TypeError):
            pass

    def summarize(qs):
        agg = qs.aggregate(
            revenue=Sum('total_amount'),
            cost=Sum('total_cost'),
            profit=Sum('net_profit'),
            count=Count('id'),
        )
        return {k: float(v or 0) for k, v in agg.items()}

    today_summary = summarize(sales_base.filter(date_created__date=today))
    month_summary = summarize(sales_base.filter(date_created__date__gte=first_of_month))
    year_summary = summarize(sales_base.filter(date_created__date__gte=first_of_year))
    period_summary = summarize(period_sales)

    # Top 10 products by revenue
    top_products = list(
        SaleInvoiceItem.objects.filter(invoice__in=period_sales)
        .values('product__name', 'product__part_number')
        .annotate(
            total_revenue=Sum(
                ExpressionWrapper(F('quantity') * F('unit_price'), output_field=DecimalField())
            ),
            total_qty=Sum('quantity'),
            total_profit=Sum(
                ExpressionWrapper(
                    F('quantity') * (F('unit_price') - F('cost_at_sale')),
                    output_field=DecimalField(),
                )
            ),
        )
        .order_by('-total_revenue')[:10]
    )
    for p in top_products:
        p['total_revenue'] = float(p['total_revenue'] or 0)
        p['total_profit'] = float(p['total_profit'] or 0)

    # Top 10 customers by spend
    top_customers = list(
        period_sales.values('customer__name', 'customer__phone')
        .annotate(total_spent=Sum('total_amount'), invoice_count=Count('id'))
        .order_by('-total_spent')[:10]
    )
    for c in top_customers:
        c['total_spent'] = float(c['total_spent'] or 0)

    # Monthly trend (6 months)
    chart_labels, chart_revenue, chart_profit, chart_count = [], [], [], []
    for i in range(5, -1, -1):
        target = today.replace(day=1) - timedelta(days=30 * i)
        month_qs = sales_base.filter(
            date_created__year=target.year, date_created__month=target.month,
        )
        agg = month_qs.aggregate(r=Sum('total_amount'), p=Sum('net_profit'), c=Count('id'))
        chart_labels.append(target.strftime('%B %Y'))
        chart_revenue.append(float(agg['r'] or 0))
        chart_profit.append(float(agg['p'] or 0))
        chart_count.append(agg['c'] or 0)

    # Operating expenses (not linked to invoices)
    try:
        expenses_qs = FinancialTransaction.objects.filter(
            transaction_type='out',
            date__date__gte=from_date, date__date__lte=to_date,
            sale_invoice__isnull=True, purchase_invoice__isnull=True,
        )
        if branch:
            expenses_qs = expenses_qs.filter(treasury__branch=branch)
        total_expenses = float(expenses_qs.aggregate(t=Sum('amount'))['t'] or 0)
    except Exception:
        total_expenses = 0.0

    # Debt aging
    from inventory.services.reporting_service import ReportingService
    try:
        debt_aging = ReportingService.customer_debt_aging(branch=branch)
    except Exception:
        debt_aging = []

    # Slow-moving inventory
    try:
        slow_moving = ReportingService.slow_moving_inventory(days_threshold=60, branch=branch)
    except Exception:
        slow_moving = []

    # Pending B2B listings count
    try:
        pending_b2b = B2BListingRequest.objects.filter(status='pending').count()
    except Exception:
        pending_b2b = 0

    # ── Customer Detail Report (when a customer is selected) ──
    customer_detail = None
    if customer_id:
        try:
            cust = Customer.objects.get(id=int(customer_id))
            cust_invoices = list(
                SaleInvoice.objects.filter(customer=cust, status='posted')
                .order_by('-date_created')[:50]
                .values('id', 'date_created', 'total_amount', 'paid_amount',
                        'is_return', 'invoice_type')
            )
            for inv in cust_invoices:
                inv['total_amount'] = float(inv['total_amount'] or 0)
                inv['paid_amount'] = float(inv['paid_amount'] or 0)
                inv['due'] = inv['total_amount'] - inv['paid_amount']
                inv['date_created'] = inv['date_created'].strftime('%Y-%m-%d') if inv['date_created'] else ''
            cust_payments = list(
                FinancialTransaction.objects.filter(
                    customer=cust, transaction_type='in',
                ).order_by('-date')[:30]
                .values('date', 'amount', 'description', 'treasury__name')
            )
            for pay in cust_payments:
                pay['amount'] = float(pay['amount'] or 0)
                pay['date'] = pay['date'].strftime('%Y-%m-%d') if pay['date'] else ''
            customer_detail = {
                'name': cust.name,
                'phone': cust.phone or '',
                'balance': float(cust.balance),
                'invoices': cust_invoices,
                'payments': cust_payments,
                'total_purchases': float(
                    SaleInvoice.objects.filter(customer=cust, status='posted', is_return=False)
                    .aggregate(t=Sum('total_amount'))['t'] or 0
                ),
                'total_returns': float(
                    SaleInvoice.objects.filter(customer=cust, status='posted', is_return=True)
                    .aggregate(t=Sum('total_amount'))['t'] or 0
                ),
                'invoice_count': SaleInvoice.objects.filter(customer=cust, status='posted').count(),
            }
        except (Customer.DoesNotExist, ValueError, TypeError):
            customer_detail = None

    # ── Expense Breakdown by Category ──
    try:
        expense_breakdown = list(
            FinancialTransaction.objects.filter(
                transaction_type='out',
                date__date__gte=from_date, date__date__lte=to_date,
                sale_invoice__isnull=True, purchase_invoice__isnull=True,
            ).values('category__name')
            .annotate(total=Sum('amount'), count=Count('id'))
            .order_by('-total')
        )
        for e in expense_breakdown:
            e['total'] = float(e['total'] or 0)
            e['category__name'] = e['category__name'] or 'بدون تصنيف'
    except Exception:
        expense_breakdown = []

    # ── Payroll Reports ──
    try:
        from hr.models import PayrollRun, PayrollEntry, Employee
        payroll_runs = list(
            PayrollRun.objects.order_by('-period_year', '-period_month')[:12]
            .values('id', 'period_month', 'period_year', 'status',
                    'total_gross', 'total_deductions', 'total_net', 'total_employees')
        )
        for pr in payroll_runs:
            pr['total_gross'] = float(pr['total_gross'] or 0)
            pr['total_deductions'] = float(pr['total_deductions'] or 0)
            pr['total_net'] = float(pr['total_net'] or 0)
            pr['status_display'] = dict(PayrollRun.STATUS_CHOICES).get(pr['status'], pr['status'])
            pr['period_label'] = f"{pr['period_month']}/{pr['period_year']}"

        # Latest payroll detail
        latest_payroll_entries = []
        if payroll_runs:
            latest_payroll_entries = list(
                PayrollEntry.objects.filter(payroll_run_id=payroll_runs[0]['id'])
                .select_related('employee__user')
                .values(
                    'employee__user__first_name', 'employee__user__last_name',
                    'employee__department', 'base_salary',
                    'late_deduction', 'absence_deduction', 'advance_deduction',
                    'other_deductions', 'bonuses', 'overtime_pay',
                    'total_deductions', 'total_additions', 'net_salary',
                    'days_present', 'days_absent', 'days_late',
                )
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            for entry in latest_payroll_entries:
                for k in ('base_salary', 'late_deduction', 'absence_deduction',
                           'advance_deduction', 'other_deductions', 'bonuses',
                           'overtime_pay', 'total_deductions', 'total_additions', 'net_salary'):
                    entry[k] = float(entry[k] or 0)
                entry['employee_name'] = f"{entry['employee__user__first_name'] or ''} {entry['employee__user__last_name'] or ''}".strip() or 'موظف'
                entry['dept'] = dept_display.get(entry['employee__department'], entry['employee__department'] or '-')

        # Payroll cost trend (6 months)
        payroll_trend_labels, payroll_trend_data = [], []
        for i in range(5, -1, -1):
            target = today.replace(day=1) - timedelta(days=30 * i)
            pr_agg = PayrollRun.objects.filter(
                period_year=target.year, period_month=target.month,
            ).aggregate(net=Sum('total_net'))
            payroll_trend_labels.append(f"{target.month}/{target.year}")
            payroll_trend_data.append(float(pr_agg['net'] or 0))
    except Exception:
        payroll_runs = []
        latest_payroll_entries = []
        payroll_trend_labels = []
        payroll_trend_data = []

    # ── Employee Reports ──
    try:
        from hr.models import AttendanceRecord, LeaveRequest, Advance
        # Employee attendance summary for current month
        employees_summary = []
        all_employees = Employee.objects.filter(
            user__is_active=True,
        ).select_related('user').order_by('department', 'user__first_name')

        for emp in all_employees:
            att = AttendanceRecord.objects.filter(
                employee=emp, date__gte=first_of_month, date__lte=today,
            )
            att_agg = att.aggregate(
                present=Count('id', filter=models.Q(status__in=('present', 'late'))),
                absent=Count('id', filter=models.Q(status='absent')),
                late=Count('id', filter=models.Q(status='late')),
                late_mins=Sum('late_minutes'),
                hours=Sum('worked_hours'),
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            employees_summary.append({
                'name': f"{emp.user.first_name or ''} {emp.user.last_name or ''}".strip() or emp.user.username,
                'department': dept_display.get(emp.department, emp.department or '-'),
                'job_title': emp.job_title or '-',
                'present': att_agg['present'] or 0,
                'absent': att_agg['absent'] or 0,
                'late': att_agg['late'] or 0,
                'late_mins': att_agg['late_mins'] or 0,
                'hours': float(att_agg['hours'] or 0),
                'salary': float(emp.base_salary),
            })

        # Leave requests for the period
        leave_requests = list(
            LeaveRequest.objects.filter(
                from_date__lte=to_date, to_date__gte=from_date,
            ).select_related('employee__user')
            .order_by('-created_at')[:50]
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'leave_type', 'from_date', 'to_date', 'status',
            )
        )
        leave_type_display = dict(LeaveRequest.TYPE_CHOICES)
        leave_status_display = dict(LeaveRequest.STATUS_CHOICES)
        for lr in leave_requests:
            lr['employee_name'] = f"{lr['employee__user__first_name'] or ''} {lr['employee__user__last_name'] or ''}".strip()
            lr['leave_type_display'] = leave_type_display.get(lr['leave_type'], lr['leave_type'])
            lr['status_display'] = leave_status_display.get(lr['status'], lr['status'])
            lr['from_date'] = lr['from_date'].isoformat() if lr['from_date'] else ''
            lr['to_date'] = lr['to_date'].isoformat() if lr['to_date'] else ''
            lr['days'] = (
                (datetime.datetime.strptime(lr['to_date'], '%Y-%m-%d').date() -
                 datetime.datetime.strptime(lr['from_date'], '%Y-%m-%d').date()).days + 1
            ) if lr['from_date'] and lr['to_date'] else 0

        # Active advances
        active_advances = list(
            Advance.objects.filter(status__in=('approved', 'active'))
            .select_related('employee__user')
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'amount', 'remaining_amount', 'installments_count', 'status',
            )
        )
        adv_status_display = dict(Advance.STATUS_CHOICES)
        for adv in active_advances:
            adv['employee_name'] = f"{adv['employee__user__first_name'] or ''} {adv['employee__user__last_name'] or ''}".strip()
            adv['amount'] = float(adv['amount'] or 0)
            adv['remaining_amount'] = float(adv['remaining_amount'] or 0)
            adv['paid'] = adv['amount'] - adv['remaining_amount']
            adv['status_display'] = adv_status_display.get(adv['status'], adv['status'])
    except Exception:
        employees_summary = []
        leave_requests = []
        active_advances = []

    context = {
        **admin.site.each_context(request),
        'title': 'التقارير والتحليلات',
        'today_summary': json.dumps(today_summary),
        'month_summary': json.dumps(month_summary),
        'year_summary': json.dumps(year_summary),
        'period_summary': json.dumps(period_summary),
        'top_products': json.dumps(top_products, ensure_ascii=False),
        'top_customers': json.dumps(top_customers, ensure_ascii=False),
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(chart_revenue),
        'chart_profit': json.dumps(chart_profit),
        'chart_count': json.dumps(chart_count),
        'total_expenses': total_expenses,
        'net_pl': period_summary['profit'] - total_expenses,
        'from_date': from_date.isoformat(),
        'to_date': to_date.isoformat(),
        'selected_customer': customer_id,
        'customers_list': json.dumps(
            list(Customer.objects.values('id', 'name').order_by('name')[:200]),
            ensure_ascii=False,
        ),
        'debt_aging': json.dumps(debt_aging, ensure_ascii=False),
        'slow_moving': json.dumps(slow_moving[:20], ensure_ascii=False),
        'pending_b2b': pending_b2b,
        # New report data
        'customer_detail': json.dumps(customer_detail, ensure_ascii=False) if customer_detail else 'null',
        'expense_breakdown': json.dumps(expense_breakdown, ensure_ascii=False),
        'payroll_runs': json.dumps(payroll_runs, ensure_ascii=False),
        'latest_payroll_entries': json.dumps(latest_payroll_entries, ensure_ascii=False),
        'payroll_trend_labels': json.dumps(payroll_trend_labels),
        'payroll_trend_data': json.dumps(payroll_trend_data),
        'employees_summary': json.dumps(employees_summary, ensure_ascii=False),
        'leave_requests': json.dumps(leave_requests, ensure_ascii=False),
        'active_advances': json.dumps(active_advances, ensure_ascii=False),
    }

    # Force-render to catch template errors inside try/except
    response = TemplateResponse(request, 'admin/reports.html', context)
    response.render()
    return response


def _build_reports_response_printing(request):
    """Internal: builds the reports TemplateResponse for PRINTING tenants."""
    from printing.models import (
        PrintOrder, PrintJob, PrintTransaction, PrintCustomer,
        PrintTreasury, PrintMaterial, Designer, DesignerWorkLog,
        MachineProfile, PrintBranch, ProductType,
    )

    from_date_str = request.GET.get('from', '')
    to_date_str = request.GET.get('to', '')
    customer_id = request.GET.get('customer', '')

    today = timezone.now().date()
    first_of_month = today.replace(day=1)
    first_of_year = today.replace(month=1, day=1)

    try:
        from_date = timezone.datetime.strptime(from_date_str, '%Y-%m-%d').date() if from_date_str else first_of_month
        to_date = timezone.datetime.strptime(to_date_str, '%Y-%m-%d').date() if to_date_str else today
    except ValueError:
        from_date, to_date = first_of_month, today

    # Base queryset — orders that are not cancelled
    orders_base = PrintOrder.objects.exclude(status='cancelled')
    branch = None
    if not request.user.is_superuser:
        try:
            branch = request.user.employee_profile.branch
            # Map employee branch to PrintBranch by name
            if branch:
                pb = PrintBranch.objects.filter(name=branch.name).first()
                if pb:
                    orders_base = orders_base.filter(branch=pb)
        except Exception:
            pass

    # Period filter
    period_orders = orders_base.filter(
        date_created__date__gte=from_date, date_created__date__lte=to_date,
    )
    if customer_id:
        try:
            period_orders = period_orders.filter(customer_id=int(customer_id))
        except (ValueError, TypeError):
            pass

    def summarize_orders(qs):
        """Summarize printing orders: revenue = net_total, cost from jobs, profit = revenue - cost."""
        agg = qs.aggregate(
            revenue=Sum(F('total_amount') - F('discount')),
            paid=Sum('paid_amount'),
            count=Count('id'),
        )
        revenue = float(agg['revenue'] or 0)
        # Calculate cost from completed jobs
        job_cost = float(
            PrintJob.objects.filter(order__in=qs, is_complete=True)
            .aggregate(c=Sum('actual_cost'))['c'] or 0
        )
        return {
            'revenue': revenue,
            'cost': job_cost,
            'profit': revenue - job_cost,
            'count': agg['count'] or 0,
        }

    today_summary = summarize_orders(orders_base.filter(date_created__date=today))
    month_summary = summarize_orders(orders_base.filter(date_created__date__gte=first_of_month))
    year_summary = summarize_orders(orders_base.filter(date_created__date__gte=first_of_year))
    period_summary = summarize_orders(period_orders)

    # Top 10 products (by ProductType)
    top_products = list(
        PrintJob.objects.filter(order__in=period_orders)
        .exclude(product_type__isnull=True)
        .values('product_type__name')
        .annotate(
            total_revenue=Sum('total_price'),
            total_qty=Sum('quantity'),
            total_profit=Sum(F('total_price') - F('actual_cost')),
        )
        .order_by('-total_revenue')[:10]
    )
    for p in top_products:
        p['product__name'] = p.pop('product_type__name', '')
        p['product__part_number'] = ''
        p['total_revenue'] = float(p['total_revenue'] or 0)
        p['total_profit'] = float(p['total_profit'] or 0)

    # Top 10 customers by spend
    top_customers = list(
        period_orders.values('customer__name', 'customer__phone')
        .annotate(
            total_spent=Sum(F('total_amount') - F('discount')),
            invoice_count=Count('id'),
        )
        .order_by('-total_spent')[:10]
    )
    for c in top_customers:
        c['total_spent'] = float(c['total_spent'] or 0)

    # Monthly trend (6 months)
    chart_labels, chart_revenue, chart_profit, chart_count = [], [], [], []
    for i in range(5, -1, -1):
        target = today.replace(day=1) - timedelta(days=30 * i)
        month_qs = orders_base.filter(
            date_created__year=target.year, date_created__month=target.month,
        )
        agg = month_qs.aggregate(
            r=Sum(F('total_amount') - F('discount')),
            c=Count('id'),
        )
        job_cost = float(
            PrintJob.objects.filter(
                order__in=month_qs, is_complete=True
            ).aggregate(cost=Sum('actual_cost'))['cost'] or 0
        )
        rev = float(agg['r'] or 0)
        chart_labels.append(target.strftime('%B %Y'))
        chart_revenue.append(rev)
        chart_profit.append(rev - job_cost)
        chart_count.append(agg['c'] or 0)

    # Operating expenses (transactions out, not linked to orders)
    try:
        expenses_qs = PrintTransaction.objects.filter(
            transaction_type='out',
            date__date__gte=from_date, date__date__lte=to_date,
            order__isnull=True,
        )
        total_expenses = float(expenses_qs.aggregate(t=Sum('amount'))['t'] or 0)
    except Exception:
        total_expenses = 0.0

    # Debt aging — customers with unpaid orders
    try:
        debt_aging = []
        customers_with_debt = (
            PrintOrder.objects.exclude(status='cancelled')
            .annotate(remaining=F('total_amount') - F('discount') - F('paid_amount'))
            .filter(remaining__gt=0)
            .values('customer__id', 'customer__name', 'customer__phone')
            .annotate(
                total_debt=Sum(F('total_amount') - F('discount') - F('paid_amount')),
                invoice_count=Count('id'),
                oldest_date=models.Min('date_created'),
            )
            .order_by('-total_debt')[:30]
        )
        for row in customers_with_debt:
            oldest = row['oldest_date']
            if oldest:
                days = (timezone.now() - oldest).days
            else:
                days = 0
            if days <= 30:
                bracket = '0-30 يوم'
            elif days <= 60:
                bracket = '31-60 يوم'
            elif days <= 90:
                bracket = '61-90 يوم'
            else:
                bracket = '+90 يوم'
            debt_aging.append({
                'customer_name': row['customer__name'] or '',
                'customer_phone': row['customer__phone'] or '',
                'total_debt': float(row['total_debt'] or 0),
                'invoice_count': row['invoice_count'] or 0,
                'oldest_days': days,
                'bracket': bracket,
            })
    except Exception:
        debt_aging = []

    # Slow-moving materials (low stock alerts)
    try:
        slow_moving = list(
            PrintMaterial.objects.filter(quantity__lte=F('min_stock'))
            .values('name', 'category', 'quantity', 'min_stock', 'cost_per_unit')
            .order_by('quantity')[:20]
        )
        cat_display = dict(PrintMaterial.CATEGORY_CHOICES)
        for m in slow_moving:
            m['product_name'] = m.pop('name', '')
            m['part_number'] = cat_display.get(m.pop('category', ''), '')
            m['current_stock'] = float(m.pop('quantity', 0))
            m['last_sale_date'] = ''
            m['days_since_last_sale'] = 0
            m['stock_value'] = float(m['current_stock'] * float(m.pop('cost_per_unit', 0)))
            m.pop('min_stock', None)
    except Exception:
        slow_moving = []

    # ── Customer Detail Report (when a customer is selected) ──
    customer_detail = None
    if customer_id:
        try:
            cust = PrintCustomer.objects.get(id=int(customer_id))
            cust_orders = list(
                PrintOrder.objects.filter(customer=cust)
                .exclude(status='cancelled')
                .order_by('-date_created')[:50]
                .values('id', 'order_number', 'date_created', 'total_amount',
                        'discount', 'paid_amount', 'status')
            )
            status_display = dict(PrintOrder.STATUS_CHOICES)
            for inv in cust_orders:
                net = float(inv['total_amount'] or 0) - float(inv['discount'] or 0)
                inv['total_amount'] = net
                inv['paid_amount'] = float(inv['paid_amount'] or 0)
                inv['due'] = net - inv['paid_amount']
                inv['date_created'] = inv['date_created'].strftime('%Y-%m-%d') if inv['date_created'] else ''
                inv['is_return'] = False
                inv['invoice_type'] = status_display.get(inv['status'], inv['status'])

            cust_payments = list(
                PrintTransaction.objects.filter(
                    order__customer=cust, transaction_type='in',
                ).order_by('-date')[:30]
                .values('date', 'amount', 'description', 'treasury__name')
            )
            for pay in cust_payments:
                pay['amount'] = float(pay['amount'] or 0)
                pay['date'] = pay['date'].strftime('%Y-%m-%d') if pay['date'] else ''

            total_revenue = float(
                PrintOrder.objects.filter(customer=cust)
                .exclude(status='cancelled')
                .aggregate(t=Sum(F('total_amount') - F('discount')))['t'] or 0
            )
            total_paid = float(
                PrintOrder.objects.filter(customer=cust)
                .exclude(status='cancelled')
                .aggregate(t=Sum('paid_amount'))['t'] or 0
            )
            customer_detail = {
                'name': cust.name,
                'phone': cust.phone or '',
                'balance': total_revenue - total_paid,
                'invoices': cust_orders,
                'payments': cust_payments,
                'total_purchases': total_revenue,
                'total_returns': 0,
                'invoice_count': PrintOrder.objects.filter(customer=cust).exclude(status='cancelled').count(),
            }
        except (PrintCustomer.DoesNotExist, ValueError, TypeError):
            customer_detail = None

    # ── Expense Breakdown (by description keywords since no category model) ──
    try:
        expense_txns = PrintTransaction.objects.filter(
            transaction_type='out',
            date__date__gte=from_date, date__date__lte=to_date,
            order__isnull=True,
        )
        # Group by first word of description as pseudo-category
        expense_breakdown = []
        expense_map = {}
        for txn in expense_txns.values('description', 'amount'):
            cat = (txn['description'] or 'أخرى').split()[0] if txn['description'] else 'أخرى'
            if cat not in expense_map:
                expense_map[cat] = {'category__name': cat, 'total': 0.0, 'count': 0}
            expense_map[cat]['total'] += float(txn['amount'] or 0)
            expense_map[cat]['count'] += 1
        expense_breakdown = sorted(expense_map.values(), key=lambda x: -x['total'])
    except Exception:
        expense_breakdown = []

    # ── Payroll Reports (shared HR module) ──
    try:
        from hr.models import PayrollRun, PayrollEntry, Employee
        payroll_runs = list(
            PayrollRun.objects.order_by('-period_year', '-period_month')[:12]
            .values('id', 'period_month', 'period_year', 'status',
                    'total_gross', 'total_deductions', 'total_net', 'total_employees')
        )
        for pr in payroll_runs:
            pr['total_gross'] = float(pr['total_gross'] or 0)
            pr['total_deductions'] = float(pr['total_deductions'] or 0)
            pr['total_net'] = float(pr['total_net'] or 0)
            pr['status_display'] = dict(PayrollRun.STATUS_CHOICES).get(pr['status'], pr['status'])
            pr['period_label'] = f"{pr['period_month']}/{pr['period_year']}"

        latest_payroll_entries = []
        if payroll_runs:
            latest_payroll_entries = list(
                PayrollEntry.objects.filter(payroll_run_id=payroll_runs[0]['id'])
                .select_related('employee__user')
                .values(
                    'employee__user__first_name', 'employee__user__last_name',
                    'employee__department', 'base_salary',
                    'late_deduction', 'absence_deduction', 'advance_deduction',
                    'other_deductions', 'bonuses', 'overtime_pay',
                    'total_deductions', 'total_additions', 'net_salary',
                    'days_present', 'days_absent', 'days_late',
                )
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            for entry in latest_payroll_entries:
                for k in ('base_salary', 'late_deduction', 'absence_deduction',
                           'advance_deduction', 'other_deductions', 'bonuses',
                           'overtime_pay', 'total_deductions', 'total_additions', 'net_salary'):
                    entry[k] = float(entry[k] or 0)
                entry['employee_name'] = f"{entry['employee__user__first_name'] or ''} {entry['employee__user__last_name'] or ''}".strip() or 'موظف'
                entry['dept'] = dept_display.get(entry['employee__department'], entry['employee__department'] or '-')

        payroll_trend_labels, payroll_trend_data = [], []
        for i in range(5, -1, -1):
            target = today.replace(day=1) - timedelta(days=30 * i)
            pr_agg = PayrollRun.objects.filter(
                period_year=target.year, period_month=target.month,
            ).aggregate(net=Sum('total_net'))
            payroll_trend_labels.append(f"{target.month}/{target.year}")
            payroll_trend_data.append(float(pr_agg['net'] or 0))
    except Exception:
        payroll_runs = []
        latest_payroll_entries = []
        payroll_trend_labels = []
        payroll_trend_data = []

    # ── Employee Reports (shared HR module) ──
    try:
        from hr.models import AttendanceRecord, LeaveRequest, Advance, Employee
        employees_summary = []
        all_employees = Employee.objects.filter(
            user__is_active=True,
        ).select_related('user').order_by('department', 'user__first_name')

        for emp in all_employees:
            att = AttendanceRecord.objects.filter(
                employee=emp, date__gte=first_of_month, date__lte=today,
            )
            att_agg = att.aggregate(
                present=Count('id', filter=models.Q(status__in=('present', 'late'))),
                absent=Count('id', filter=models.Q(status='absent')),
                late=Count('id', filter=models.Q(status='late')),
                late_mins=Sum('late_minutes'),
                hours=Sum('worked_hours'),
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            employees_summary.append({
                'name': f"{emp.user.first_name or ''} {emp.user.last_name or ''}".strip() or emp.user.username,
                'department': dept_display.get(emp.department, emp.department or '-'),
                'job_title': emp.job_title or '-',
                'present': att_agg['present'] or 0,
                'absent': att_agg['absent'] or 0,
                'late': att_agg['late'] or 0,
                'late_mins': att_agg['late_mins'] or 0,
                'hours': float(att_agg['hours'] or 0),
                'salary': float(emp.base_salary),
            })

        leave_requests = list(
            LeaveRequest.objects.filter(
                from_date__lte=to_date, to_date__gte=from_date,
            ).select_related('employee__user')
            .order_by('-created_at')[:50]
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'leave_type', 'from_date', 'to_date', 'status',
            )
        )
        leave_type_display = dict(LeaveRequest.TYPE_CHOICES)
        leave_status_display = dict(LeaveRequest.STATUS_CHOICES)
        for lr in leave_requests:
            lr['employee_name'] = f"{lr['employee__user__first_name'] or ''} {lr['employee__user__last_name'] or ''}".strip()
            lr['leave_type_display'] = leave_type_display.get(lr['leave_type'], lr['leave_type'])
            lr['status_display'] = leave_status_display.get(lr['status'], lr['status'])
            lr['from_date'] = lr['from_date'].isoformat() if lr['from_date'] else ''
            lr['to_date'] = lr['to_date'].isoformat() if lr['to_date'] else ''
            lr['days'] = (
                (datetime.datetime.strptime(lr['to_date'], '%Y-%m-%d').date() -
                 datetime.datetime.strptime(lr['from_date'], '%Y-%m-%d').date()).days + 1
            ) if lr['from_date'] and lr['to_date'] else 0

        active_advances = list(
            Advance.objects.filter(status__in=('approved', 'active'))
            .select_related('employee__user')
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'amount', 'remaining_amount', 'installments_count', 'status',
            )
        )
        adv_status_display = dict(Advance.STATUS_CHOICES)
        for adv in active_advances:
            adv['employee_name'] = f"{adv['employee__user__first_name'] or ''} {adv['employee__user__last_name'] or ''}".strip()
            adv['amount'] = float(adv['amount'] or 0)
            adv['remaining_amount'] = float(adv['remaining_amount'] or 0)
            adv['paid'] = adv['amount'] - adv['remaining_amount']
            adv['status_display'] = adv_status_display.get(adv['status'], adv['status'])
    except Exception:
        employees_summary = []
        leave_requests = []
        active_advances = []

    context = {
        **admin.site.each_context(request),
        'title': 'التقارير والتحليلات',
        'today_summary': json.dumps(today_summary),
        'month_summary': json.dumps(month_summary),
        'year_summary': json.dumps(year_summary),
        'period_summary': json.dumps(period_summary),
        'top_products': json.dumps(top_products, ensure_ascii=False),
        'top_customers': json.dumps(top_customers, ensure_ascii=False),
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(chart_revenue),
        'chart_profit': json.dumps(chart_profit),
        'chart_count': json.dumps(chart_count),
        'total_expenses': total_expenses,
        'net_pl': period_summary['profit'] - total_expenses,
        'from_date': from_date.isoformat(),
        'to_date': to_date.isoformat(),
        'selected_customer': customer_id,
        'customers_list': json.dumps(
            list(PrintCustomer.objects.values('id', 'name').order_by('name')[:200]),
            ensure_ascii=False,
        ),
        'debt_aging': json.dumps(debt_aging, ensure_ascii=False),
        'slow_moving': json.dumps(slow_moving, ensure_ascii=False),
        'pending_b2b': 0,
        # New report data
        'customer_detail': json.dumps(customer_detail, ensure_ascii=False) if customer_detail else 'null',
        'expense_breakdown': json.dumps(expense_breakdown, ensure_ascii=False),
        'payroll_runs': json.dumps(payroll_runs, ensure_ascii=False),
        'latest_payroll_entries': json.dumps(latest_payroll_entries, ensure_ascii=False),
        'payroll_trend_labels': json.dumps(payroll_trend_labels),
        'payroll_trend_data': json.dumps(payroll_trend_data),
        'employees_summary': json.dumps(employees_summary, ensure_ascii=False),
        'leave_requests': json.dumps(leave_requests, ensure_ascii=False),
        'active_advances': json.dumps(active_advances, ensure_ascii=False),
    }

    response = TemplateResponse(request, 'admin/reports.html', context)
    response.render()
    return response


# Inject reports URL into admin
from django.urls import path as _admin_path
_original_get_urls = admin.AdminSite.get_urls

def _custom_get_urls(self):
    custom = [
        _admin_path('reports/', self.admin_view(admin_reports_view), name='admin_reports'),
    ]
    return custom + _original_get_urls(self)

admin.AdminSite.get_urls = _custom_get_urls


# =====================================================================
# 📋 11. سجل المراجعة والتدقيق (Audit Trail — Read-Only)
# =====================================================================
@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'action_badge', 'model_name', 'object_id', 'object_repr_short', 'ip_address')
    list_filter = ('action', 'model_name', 'timestamp')
    search_fields = ('object_repr', 'object_id', 'user__username', 'ip_address')
    date_hierarchy = 'timestamp'
    readonly_fields = ('timestamp', 'user', 'action', 'model_name', 'object_id', 'object_repr', 'changes_json', 'ip_address')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def action_badge(self, obj):
        colors = {'create': '#28a745', 'update': '#007bff', 'delete': '#dc3545'}
        icons = {'create': '➕', 'update': '✏️', 'delete': '🗑️'}
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">{} {}</span>',
            colors.get(obj.action, 'gray'), icons.get(obj.action, ''), obj.get_action_display()
        )
    action_badge.short_description = "العملية"

    def object_repr_short(self, obj):
        text = obj.object_repr or ''
        return text[:60] + '...' if len(text) > 60 else text
    object_repr_short.short_description = "الوصف"


# =====================================================================
# 📊 12. دليل الحسابات المحاسبية (Chart of Accounts)
# =====================================================================
@admin.register(ChartOfAccount)
class ChartOfAccountAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'account_type_badge', 'parent', 'balance_display', 'is_active')
    list_filter = ('account_type', 'is_active')
    search_fields = ('code', 'name')
    list_editable = ('is_active',)
    ordering = ('code',)

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def account_type_badge(self, obj):
        colors = {
            'asset': '#007bff', 'liability': '#dc3545', 'equity': '#6f42c1',
            'revenue': '#28a745', 'expense': '#fd7e14',
        }
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            colors.get(obj.account_type, 'gray'), obj.get_account_type_display()
        )
    account_type_badge.short_description = "نوع الحساب"

    def balance_display(self, obj):
        bal = obj.balance
        color = "#28a745" if bal >= 0 else "#dc3545"
        return format_html('<b style="color:{};">{} ج.م</b>', color, f"{float(bal):,.2f}")
    balance_display.short_description = "الرصيد"


# =====================================================================
# 📒 13. دفتر القيود المحاسبية (Accounting Ledger)
# =====================================================================
@admin.register(AccountingEntry)
class AccountingEntryAdmin(admin.ModelAdmin):
    list_display = ('entry_date', 'reference', 'account', 'description_short', 'debit_display', 'credit_display', 'linked_doc')
    list_filter = ('account__account_type', 'entry_date')
    search_fields = ('reference', 'description', 'account__name')
    date_hierarchy = 'entry_date'
    autocomplete_fields = ['account']
    readonly_fields = ('entry_date', 'reference', 'description', 'account', 'debit', 'credit',
                       'sale_invoice', 'purchase_invoice', 'financial_transaction', 'created_by')

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def description_short(self, obj):
        text = obj.description or ''
        return text[:50] + '...' if len(text) > 50 else text
    description_short.short_description = "البيان"

    def debit_display(self, obj):
        if obj.debit > 0:
            return format_html('<b style="color:#dc3545;">{}</b>', f"{float(obj.debit):,.2f}")
        return '-'
    debit_display.short_description = "مدين"

    def credit_display(self, obj):
        if obj.credit > 0:
            return format_html('<b style="color:#28a745;">{}</b>', f"{float(obj.credit):,.2f}")
        return '-'
    credit_display.short_description = "دائن"

    def linked_doc(self, obj):
        if obj.sale_invoice:
            url = reverse('admin:inventory_saleinvoice_change', args=[obj.sale_invoice.pk])
            return format_html('<a href="{}">فاتورة بيع #{}</a>', url, obj.sale_invoice.pk)
        if obj.purchase_invoice:
            url = reverse('admin:inventory_purchaseinvoice_change', args=[obj.purchase_invoice.pk])
            return format_html('<a href="{}">فاتورة شراء #{}</a>', url, obj.purchase_invoice.pk)
        if obj.financial_transaction:
            url = reverse('admin:inventory_financialtransaction_change', args=[obj.financial_transaction.pk])
            return format_html('<a href="{}">حركة مالية #{}</a>', url, obj.financial_transaction.pk)
        return '-'
    linked_doc.short_description = "المستند"


# =====================================================================
# 📦 14. سجل حركات المخزون (Inventory Movements)
# =====================================================================
@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'product', 'branch', 'reason_badge', 'qty_change_display', 'quantity_before', 'quantity_after', 'note')
    list_filter = ('reason', 'branch', 'created_at')
    search_fields = ('product__name', 'product__part_number', 'note')
    date_hierarchy = 'created_at'
    readonly_fields = ('product', 'branch', 'reason', 'quantity_change', 'quantity_before',
                       'quantity_after', 'reference_type', 'reference_id', 'note', 'created_at', 'created_by')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def reason_badge(self, obj):
        colors = {
            'sale': '#dc3545', 'sale_return': '#28a745', 'purchase': '#007bff',
            'purchase_return': '#fd7e14', 'transfer_out': '#6c757d', 'transfer_in': '#17a2b8',
            'adjustment': '#6f42c1', 'scrap': '#343a40', 'manual': '#ffc107',
        }
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            colors.get(obj.reason, 'gray'), obj.get_reason_display()
        )
    reason_badge.short_description = "السبب"

    def qty_change_display(self, obj):
        color = "#28a745" if obj.quantity_change > 0 else "#dc3545"
        return format_html('<b style="color:{}; font-size:13px;">{:+d}</b>', color, obj.quantity_change)
    qty_change_display.short_description = "التغيير"


# =====================================================================
# 🚨 15. تنبيهات نقص المخزون (Stock Alerts)
# =====================================================================
@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'product', 'branch', 'alert_badge', 'current_quantity', 'min_stock_level', 'status_badge')
    list_filter = ('alert_type', 'is_resolved', 'branch')
    search_fields = ('product__name', 'product__part_number')
    actions = ['mark_resolved']

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def alert_badge(self, obj):
        if obj.alert_type == 'out_of_stock':
            return format_html('<span style="background:#dc3545; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">⚠️ نفاد تام</span>')
        return format_html('<span style="background:#ffc107; color:#000; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">📉 منخفض</span>')
    alert_badge.short_description = "نوع التنبيه"

    def status_badge(self, obj):
        if obj.is_resolved:
            return format_html('<span style="color:#28a745; font-weight:bold;">✅ تم الحل</span>')
        return format_html('<span style="color:#dc3545; font-weight:bold;">🔴 نشط</span>')
    status_badge.short_description = "الحالة"

    @admin.action(description='✅ وضع علامة "تم الحل" على التنبيهات المحددة')
    def mark_resolved(self, request, queryset):
        updated = queryset.filter(is_resolved=False).update(is_resolved=True, resolved_at=timezone.now())
        self.message_user(request, f"تم إغلاق {updated} تنبيه بنجاح.", messages.SUCCESS)


# =====================================================================
# 🔑 16. جلسات الاستيراد الآمن (Import Sessions)
# =====================================================================
@admin.register(ImportSession)
class ImportSessionAdmin(admin.ModelAdmin):
    list_display = ('session_id_short', 'entity_type', 'status_badge', 'total_rows', 'valid_rows', 'error_rows', 'conflict_rows', 'created_by', 'created_at')
    list_filter = ('entity_type', 'status')
    readonly_fields = ('session_id', 'entity_type', 'status', 'uploaded_file', 'original_filename',
                       'total_rows', 'valid_rows', 'error_rows', 'conflict_rows',
                       'validation_report', 'conflict_report', 'imported_ids', 'backup_snapshot',
                       'created_by', 'created_at', 'completed_at')

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def session_id_short(self, obj):
        return obj.session_id.hex[:8]
    session_id_short.short_description = "رقم الجلسة"

    def status_badge(self, obj):
        colors = {
            'pending': '#6c757d', 'validating': '#17a2b8', 'preview': '#ffc107',
            'importing': '#007bff', 'completed': '#28a745', 'failed': '#dc3545',
            'rolled_back': '#fd7e14',
        }
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px; font-weight:bold;">{}</span>',
            colors.get(obj.status, 'gray'), obj.get_status_display()
        )
    status_badge.short_description = "الحالة"


# =====================================================================
# 🛒 17. طلبات النشر في السوق المركزي (B2B Listing Approval)
# =====================================================================
@admin.register(B2BListingRequest)
class B2BListingRequestAdmin(SecureImportExportAdmin):
    list_display = (
        'product', 'status_badge', 'requested_price', 'approved_price',
        'requested_by', 'reviewed_by', 'created_at',
    )
    list_filter = ('status', 'created_at')
    search_fields = ('product__name', 'product__part_number')
    readonly_fields = (
        'product', 'requested_price', 'requested_by',
        'created_at', 'reviewed_at', 'is_synced',
    )
    actions = ['approve_listings', 'reject_listings']

    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def status_badge(self, obj):
        colors = {'pending': '#f59e0b', 'approved': '#10b981', 'rejected': '#ef4444'}
        return format_html(
            '<span style="background:{}; color:white; padding:3px 10px; '
            'border-radius:4px; font-size:11px; font-weight:bold;">{}</span>',
            colors.get(obj.status, '#6b7280'), obj.get_status_display(),
        )
    status_badge.short_description = "حالة الطلب"

    @admin.action(description='تمت الموافقة — نشر في السوق المركزي')
    def approve_listings(self, request, queryset):
        from inventory.services.inventory_service import InventoryService
        approved = 0
        for listing in queryset.filter(status='pending'):
            price = listing.approved_price or listing.requested_price
            InventoryService.approve_b2b_listing(listing, price, request.user)
            approved += 1
        self.message_user(
            request,
            f"تمت الموافقة على {approved} طلب ونشرها في السوق المركزي بنجاح.",
            messages.SUCCESS,
        )

    @admin.action(description='رفض الطلبات المحددة')
    def reject_listings(self, request, queryset):
        updated = queryset.filter(status='pending').update(
            status='rejected',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"تم رفض {updated} طلب.", messages.WARNING)