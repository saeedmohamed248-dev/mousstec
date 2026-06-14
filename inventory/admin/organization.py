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
# Branches, employees, users, maintenance contracts (org skeleton).

# =====================================================================
# 🏢 1. رادارات التحكم في باقات الـ SaaS (Quotas Enforcement)
# =====================================================================
@admin.register(Branch)
class BranchAdmin(SecureImportExportAdmin):
    list_display = ('name', 'location', 'phone', 'is_active_badge')
    # 🐛 admin.E040 fix — RFQAdmin.autocomplete_fields includes 'branch',
    # which requires the target admin to declare search_fields.
    search_fields = ('name', 'location', 'phone')
    
    def is_active_badge(self, obj):
        return format_html('<span style="color:#28a745; font-weight:bold;">✅ نشط وبث لايف</span>')
    is_active_badge.short_description = "حالة الفرع"

    # 🧹 Branch-count guard was moved to clients.signals_quota.pre_save so
    # it fires on every path (admin / DRF / shell / loaddata), not just
    # the admin form. The signal raises ValidationError, which the admin
    # surfaces the same way save_model() did. No behavior change for
    # admin users; closes the leak for non-admin entry points.
    pass

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

    def save_model(self, request, obj, form, change):
        # 👤 الموظف الجديد لازم يكون staff علشان يقدر يدخل /secure-portal/ (Django admin).
        # من غير الفلاج ده Django بيعرض «You are authenticated… but not authorized»
        # بعد ما الـ login ينجح. السوبر يوزر يفضل manual.
        if not change and connection.schema_name != 'public' and not obj.is_staff:
            obj.is_staff = True
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        # The User post_save signal auto-creates an EmployeeProfile via
        # get_or_create. When the admin add-view also submits the inline,
        # the formset tries to INSERT a second profile for the same user
        # and trips the user_id UNIQUE constraint. Redirect the inline's
        # new instance onto the existing row so it becomes an UPDATE.
        if formset.model is EmployeeProfile:
            user = form.instance
            existing = EmployeeProfile.objects.filter(user=user).first()
            if existing is not None:
                for f in formset.forms:
                    if f.cleaned_data.get('DELETE'):
                        continue
                    if f.instance.pk is None:
                        f.instance.pk = existing.pk
        super().save_formset(request, form, formset, change)

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

    @admin.action(description='💸 صرف العمولات المستحقة للفنيين المحددين (Admin shortcut — UI الكامل في /system/commissions/)')
    def pay_tech_commissions(self, request, queryset):
        from inventory.services.treasury_service import TreasuryService
        from inventory.models import Treasury, EmployeeProfile

        # Translate User queryset → EmployeeProfile queryset (new service signature)
        profile_ids = [
            u.employee_profile.pk for u in queryset
            if hasattr(u, 'employee_profile')
        ]
        profiles = EmployeeProfile.objects.filter(pk__in=profile_ids)

        # Admin fallback: pick first active treasury (the proper UI does branch-scoped picker).
        treasury = Treasury.objects.filter(is_active=True).order_by('pk').first()
        if not treasury:
            self.message_user(request, "❌ لا توجد خزنة نشطة. أنشئ واحدة أولاً.", messages.ERROR)
            return

        try:
            result = TreasuryService.pay_commissions(
                profiles, treasury=treasury, paid_by_user=request.user,
                allowed_roles={'tech'},  # preserve original action's tech-only scope
            )
            self.message_user(
                request,
                f"✅ صُرفت {result['paid_count']} عمولات بإجمالي "
                f"{result['total_paid']:,.2f} ج.م من خزنة «{result['treasury_name']}».",
                messages.SUCCESS,
            )
        except ValidationError as e:
            self.message_user(request, f"❌ {e.messages[0]}", messages.ERROR)


