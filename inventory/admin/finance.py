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
# Regional treasury + FinTech audit ledger admins.

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


