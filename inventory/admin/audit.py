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
# AuditLog + ImportSession admins — read-only operations trails.

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


