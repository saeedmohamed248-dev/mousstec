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
# Chart of accounts, accounting ledger, inventory movements, stock alerts.

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
        return format_html('<b style="color:{}; font-size:13px;">{}</b>', color, f"{obj.quantity_change:+d}")
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


