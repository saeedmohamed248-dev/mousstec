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


# Foundational admin mixins — inherited by every admin class in this package.
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


